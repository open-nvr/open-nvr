"""The class scheduler and the per-class snapshot worker.

Unchanged rules from the AWS version: a class window is cut into base slots;
within a slot the worker attempts recognition every ``attempt_interval`` and
aggregates every code seen; at the slot boundary one snapshot row set is
written; when the class ends the attendance is finalised (presence ratio ≥
threshold → PRESENT). What changed: the worker holds no RTSP capture — at
each attempt it asks core for the camera's current frame and runs detect →
crop → recognise through KAI-C.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

try:
    from apscheduler.schedulers.background import BackgroundScheduler  # type: ignore
except Exception:  # pragma: no cover
    BackgroundScheduler = None  # type: ignore

from ..config import get_settings
from ..storage.repo import (
    ensure_finalization_marker,
    finalize_attendance,
    list_attendance_classes,
    save_snapshot,
    streams_list,
    streams_mark_schedule_finalized,
    streams_update,
)
from ..utils.image_io import save_frame_snapshot
from .recognize import RecognitionUnavailable, recognize_codes_in_frame

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


@dataclass
class StreamWorker:
    stream_id: int
    name: str
    camera_id: Optional[int]
    class_id: Optional[str] = None
    course_code: Optional[str] = None
    section: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    running: bool = False
    thread: Optional[threading.Thread] = None
    recognizer: Any = None
    frames: Any = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    on_exit: Optional[Callable[[], None]] = None
    attempts: int = 0
    failed_attempts: int = 0
    unwatched_slots: int = 0
    last_codes: List[str] = field(default_factory=list)
    # Test hook: a clock the loop reads instead of the wall clock.
    clock: Callable[[], datetime] = _utcnow
    tick_seconds: float = 1.0

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"attendly-worker-{self.stream_id}")
        self.thread.start()
        try:
            streams_update(self.stream_id, status="running")
        except Exception:
            pass
        logger.info("Worker %s started for %s class=%s", self.stream_id, self.name, self.class_id)

    def stop(self) -> None:
        if not self.running:
            return
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5.0)
        self.running = False
        try:
            streams_update(self.stream_id, status="stopped")
        except Exception:
            pass
        logger.info("Worker %s stopped for %s", self.stream_id, self.name)

    def _fetch_frame(self):
        if self.frames is None or self.camera_id is None:
            return None
        try:
            return self.frames.snapshot(self.camera_id)
        except Exception as exc:
            logger.warning("Worker %s snapshot failed: %s", self.stream_id, exc)
            return None

    def _keep_evidence(self, frame) -> Optional[str]:
        """The frame somebody was recognised in, into core's evidence store
        (one per slot; retention applies). Best effort."""
        fn = getattr(self.frames, "save_evidence", None)
        if fn is None or not get_settings().keep_evidence_frames:
            return None
        try:
            import cv2

            ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            return fn(buf.tobytes()) if ok else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("Worker %s evidence not kept: %s", self.stream_id, exc)
            return None

    def _recognize_codes(self, frame) -> List[str]:
        if frame is None or self.recognizer is None:
            return []
        save_frame_snapshot(frame, class_id=self.class_id, stream_id=self.stream_id, tag="attempt")
        return recognize_codes_in_frame(
            self.recognizer, frame, course_code=self.course_code, section=self.section,
            tag=f"s{self.stream_id}",
        )

    def _run(self) -> None:
        settings = get_settings()
        try:
            delay_s = int(settings.snapshot_start_delay_seconds)
            base_slot_s = int(settings.base_slot_minutes) * 60
            attempt_interval_s = max(int(settings.attempt_interval_minutes) * 60,
                                     int(settings.min_attempt_interval_seconds))
            now = self.clock()
            if self.start_time and self.end_time:
                first_slot_start = self.start_time + timedelta(seconds=delay_s)
                current_slot_end = min(first_slot_start + timedelta(seconds=base_slot_s), self.end_time)
            else:
                first_slot_start = now + timedelta(seconds=delay_s)
                current_slot_end = first_slot_start + timedelta(seconds=int(settings.snapshot_interval_minutes) * 60)
            current_slot_start = first_slot_start
            next_attempt_at = first_slot_start
            aggregated: Dict[str, str] = {}  # code → first seen in this slot
            saved_current_slot = False
            slot_watched = False  # did any attempt in this slot actually look?
            slot_evidence: Optional[str] = None  # one frame per slot in which somebody was seen

            def save_slot(slot_end: datetime) -> None:
                nonlocal saved_current_slot
                if not self.class_id:
                    return
                try:
                    save_snapshot(self.class_id, dict(aggregated), slot_end, watched=slot_watched,
                                  evidence_path=slot_evidence)
                    saved_current_slot = True
                    if not slot_watched:
                        self.unwatched_slots += 1
                    logger.info("Worker %s class=%s slot ending %s saved: %s, %d codes", self.stream_id, self.class_id,
                                slot_end.isoformat(), "watched" if slot_watched else "UNWATCHED", len(aggregated))
                except Exception:
                    logger.exception("Snapshot aggregation save error")
            logger.info(
                "Worker %s class=%s timing: delay=%ss base_slot=%ss attempt_interval=%ss first_slot=%s..%s",
                self.stream_id, self.class_id, delay_s, base_slot_s, attempt_interval_s,
                current_slot_start.isoformat(), current_slot_end.isoformat(),
            )
            while not self.stop_event.is_set():
                now = self.clock()
                if self.end_time and now >= self.end_time:
                    # The last slot is flushed before the worker leaves —
                    # even an empty one, so it counts like every other.
                    if not saved_current_slot:
                        save_slot(current_slot_end)
                    break
                if now >= next_attempt_at and now < current_slot_end:
                    self.attempts += 1
                    try:
                        frame = self._fetch_frame()
                        if frame is None:
                            raise RecognitionUnavailable("no frame from the camera")
                        codes = self._recognize_codes(frame)
                        slot_watched = True
                        for c in codes:
                            aggregated.setdefault(c, now.isoformat())
                        if codes and slot_evidence is None:
                            slot_evidence = self._keep_evidence(frame)
                        self.last_codes = codes
                        logger.info("Worker %s class=%s attempt #%d found %d codes: %s",
                                    self.stream_id, self.class_id, self.attempts, len(codes), ",".join(codes) or "none")
                    except RecognitionUnavailable as exc:
                        self.failed_attempts += 1
                        logger.warning("Worker %s class=%s attempt #%d could not look: %s", self.stream_id,
                                       self.class_id, self.attempts, exc)
                    except Exception:
                        self.failed_attempts += 1
                        logger.exception("Recognition attempt error")
                    next_attempt_at = min(current_slot_end, now + timedelta(seconds=attempt_interval_s))
                if now >= current_slot_end:
                    save_slot(current_slot_end)
                    aggregated = {}
                    slot_watched = False
                    slot_evidence = None
                    slot_len = base_slot_s if (self.start_time and self.end_time) else int(settings.snapshot_interval_minutes) * 60
                    next_start = current_slot_end
                    if self.end_time:
                        current_slot_end = min(next_start + timedelta(seconds=slot_len), self.end_time)
                    else:
                        current_slot_end = next_start + timedelta(seconds=slot_len)
                    current_slot_start = next_start
                    next_attempt_at = next_start
                    saved_current_slot = False
                    if self.end_time and current_slot_end <= current_slot_start:
                        break
                self.stop_event.wait(self.tick_seconds)
        finally:
            self.running = False
            try:
                streams_update(self.stream_id, status="stopped")
            except Exception:
                pass
            logger.info("Worker %s finished for %s", self.stream_id, self.name)
            if self.on_exit is not None:
                try:
                    self.on_exit()
                except Exception:
                    pass


class StreamEngine:
    """Singleton: turns the rooms' schedules into workers and finalisations."""

    _instance: Optional["StreamEngine"] = None

    def __init__(self, *, recognizer: Any = None, frames: Any = None) -> None:
        # Reentrant: start() holds it while reload_streams() takes it again.
        # (The AWS version had a plain Lock here and only survived because
        # apscheduler was never installed, so this path never ran.)
        self._lock = threading.RLock()
        self._workers: Dict[int, StreamWorker] = {}
        self._schedules: Dict[int, List[Dict[str, Any]]] = {}
        self._settings = get_settings()
        self._recognizer = recognizer
        self._frames = frames
        self._finalized_classes: set[str] = set()
        self._worker_slots = threading.Semaphore(max(1, int(self._settings.max_workers)))
        self._scheduler_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._aps: Optional[BackgroundScheduler] = None
        try:
            for cid in list_attendance_classes():
                if cid:
                    self._finalized_classes.add(str(cid))
        except Exception:
            pass

    @classmethod
    def get_shared(cls) -> "StreamEngine":
        if cls._instance is None:
            cls._instance = StreamEngine()
        return cls._instance

    @classmethod
    def set_shared(cls, engine: Optional["StreamEngine"]) -> None:
        cls._instance = engine

    def _recognizer_or_none(self):
        if self._recognizer is None:
            try:
                from ..face.engine import FaceEngine

                self._recognizer = FaceEngine.get_shared()
            except Exception as exc:
                logger.warning("face engine unavailable: %s", exc)
        return self._recognizer

    def _frames_or_none(self):
        if self._frames is None:
            try:
                from ..cameras import CameraSource

                self._frames = CameraSource.get_shared()
            except Exception as exc:
                logger.warning("camera source unavailable: %s", exc)
        return self._frames

    def start(self) -> None:
        with self._lock:
            if self._scheduler_thread and self._scheduler_thread.is_alive():
                return
            self._stop.clear()
            if BackgroundScheduler is not None and self._aps is None:
                try:
                    self._aps = BackgroundScheduler()
                    self._aps.start()
                except Exception:
                    self._aps = None
            use_aps = self._aps is not None
        # The first schedule load talks to the database and may finalise
        # missed classes: not under the lock, so startup never waits on it.
        if use_aps:
            try:
                self.reload_streams()
                self._reschedule_aps()
            except Exception:
                logger.exception("initial schedule load failed; the refresh loop will retry")
        with self._lock:
            target = self._refresh_loop if use_aps else self._scheduler_loop
            self._scheduler_thread = threading.Thread(target=target, daemon=True, name="attendly-engine")
            self._scheduler_thread.start()
        logger.info("StreamEngine started")

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            workers = list(self._workers.values())
        for w in workers:
            try:
                w.stop()
            except Exception:
                pass
        try:
            if self._aps is not None:
                self._aps.shutdown(wait=False)
        except Exception:
            pass
        logger.info("StreamEngine stopped")

    def reload_streams(self) -> None:
        schedules_by_id: Dict[int, List[Dict[str, Any]]] = {}
        for it in streams_list(include_deleted=False):
            sid = int(it.get("id", 0))
            if not sid:
                continue
            schedule = it.get("schedule") or []
            for ent in schedule:
                ent.setdefault("is_finalized", False)
            schedules_by_id[sid] = schedule
        with self._lock:
            self._schedules = schedules_by_id
            running = [(sid, w) for sid, w in self._workers.items() if w.running and w.class_id]
        # A class called off while its worker runs: the worker stops, and
        # the slot it was on is not written (nothing happened in the room).
        for sid, w in running:
            for ent in schedules_by_id.get(sid, []):
                if str(ent.get("class_id") or "") == w.class_id and ent.get("called_off"):
                    logger.info("Class %s called off; stopping worker %s", w.class_id, sid)
                    w.stop()

    def schedules(self) -> Dict[int, List[Dict[str, Any]]]:
        with self._lock:
            return {k: list(v) for k, v in self._schedules.items()}

    def _finalize(self, class_id: str, stream_id: Optional[int], *, force: bool = False) -> None:
        """Write the class's attendance from its snapshots. Runs once per
        class from the schedule, and again (``force``) when the class's
        worker exits, so the slot it flushed last is counted."""
        if class_id in self._finalized_classes and not force:
            return
        try:
            res = finalize_attendance(class_id)
            self._finalized_classes.add(class_id)
            logger.info("Finalized class %s: written=%s total_slots=%s", class_id, res.get("written"), res.get("total_slots"))
            if stream_id is not None:
                try:
                    streams_mark_schedule_finalized(stream_id, class_id)
                except Exception:
                    pass
        except Exception:
            logger.exception("Finalize error for %s", class_id)

    def _scheduler_loop(self) -> None:
        self.reload_streams()
        last_reload = time.time()
        while not self._stop.is_set():
            try:
                now = _utcnow()
                for stream_id, entries in self.schedules().items():
                    for sch in entries:
                        class_id = str(sch.get("class_id") or "")
                        start_iso, end_iso = sch.get("start_iso"), sch.get("end_iso")
                        if not class_id or not start_iso or not end_iso:
                            continue
                        try:
                            start_dt = _to_utc(datetime.fromisoformat(start_iso))
                            end_dt = _to_utc(datetime.fromisoformat(end_iso))
                        except Exception:
                            continue
                        if sch.get("is_finalized") or sch.get("called_off") or class_id in self._finalized_classes:
                            continue
                        if start_dt <= now <= end_dt:
                            self._ensure_running(stream_id, class_id, start_dt, end_dt)
                        if now > end_dt + timedelta(seconds=self._settings.finalize_grace_seconds):
                            self._finalize(class_id, stream_id)
                if time.time() - last_reload >= 30:
                    self.reload_streams()
                    last_reload = time.time()
            except Exception:
                logger.exception("Engine scheduler loop error")
            self._stop.wait(1.0)

    def _refresh_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.reload_streams()
                self._reschedule_aps()
            except Exception:
                logger.exception("Engine refresh loop error")
            self._stop.wait(15.0)

    def _reschedule_aps(self) -> None:
        if self._aps is None:
            return
        try:
            for job in list(self._aps.get_jobs()):
                self._aps.remove_job(job.id)
        except Exception:
            pass
        now = _utcnow()
        for stream_id, entries in self.schedules().items():
            for sch in entries:
                class_id = str(sch.get("class_id") or "")
                start_iso, end_iso = sch.get("start_iso"), sch.get("end_iso")
                if not class_id or not start_iso or not end_iso:
                    continue
                try:
                    start_dt = _to_utc(datetime.fromisoformat(start_iso))
                    end_dt = _to_utc(datetime.fromisoformat(end_iso))
                except Exception:
                    continue
                if sch.get("is_finalized") or sch.get("called_off"):
                    continue
                grace = timedelta(seconds=self._settings.finalize_grace_seconds)
                if end_dt + grace <= now:
                    if class_id not in self._finalized_classes:
                        self._finalize(class_id, stream_id)
                    else:
                        try:
                            ensure_finalization_marker(class_id)
                        except Exception:
                            pass
                    continue
                try:
                    self._aps.add_job(self._ensure_running, "date", run_date=max(start_dt, now),
                                      args=[stream_id, class_id, start_dt, end_dt],
                                      id=f"start-{stream_id}-{class_id}", replace_existing=True)
                    self._aps.add_job(self._finalize, "date", run_date=end_dt + grace, args=[class_id, stream_id],
                                      id=f"end-{stream_id}-{class_id}", replace_existing=True)
                except Exception:
                    pass

    def _ensure_running(self, stream_id: int, class_id: str, start_dt: datetime, end_dt: datetime) -> None:
        with self._lock:
            w = self._workers.get(stream_id)
            if w and w.running and w.class_id == class_id:
                return
        match = next((it for it in streams_list(include_deleted=False) if int(it.get("id", 0)) == stream_id), None)
        if not match:
            return
        if not self._worker_slots.acquire(blocking=False):
            logger.info("Worker slot unavailable; deferring stream %s class %s", stream_id, class_id)
            if self._aps is not None:
                try:
                    self._aps.add_job(self._ensure_running, "date", run_date=_utcnow() + timedelta(seconds=5),
                                      args=[stream_id, class_id, start_dt, end_dt],
                                      id=f"retry-{stream_id}-{class_id}-{int(time.time())}", replace_existing=False)
                except Exception:
                    pass
            return
        course_code = section = None
        for ent in match.get("schedule") or []:
            if str(ent.get("class_id") or "") == class_id:
                course_code = str(ent.get("course") or "") or None
                section = str(ent.get("section") or "") or None
                break
        camera_id = match.get("camera_id")
        worker = StreamWorker(
            stream_id=stream_id, name=str(match.get("name") or f"Room {stream_id}"),
            camera_id=int(camera_id) if camera_id is not None else None, class_id=class_id,
            course_code=course_code, section=section, start_time=start_dt, end_time=end_dt,
            recognizer=self._recognizer_or_none(), frames=self._frames_or_none(),
            on_exit=lambda: self._worker_exited(stream_id, class_id),
        )
        with self._lock:
            old = self._workers.get(stream_id)
            self._workers[stream_id] = worker
        if old is not None and old.running:
            try:
                old.stop()
            except Exception:
                pass
        try:
            streams_update(stream_id, status="starting")
        except Exception:
            pass
        worker.start()

    def _worker_exited(self, stream_id: int, class_id: Optional[str]) -> None:
        self._worker_slots.release()
        # The worker has flushed its last slot; count it now rather than
        # whenever the scheduled finalisation happens to run.
        if class_id and self.clock_now() >= self._class_end(stream_id, class_id):
            self._finalize(class_id, stream_id, force=True)

    def clock_now(self) -> datetime:
        return _utcnow()

    def _class_end(self, stream_id: int, class_id: str) -> datetime:
        for sch in self.schedules().get(stream_id, []):
            if str(sch.get("class_id") or "") == class_id and sch.get("end_iso"):
                try:
                    return _to_utc(datetime.fromisoformat(sch["end_iso"]))
                except Exception:
                    break
        return datetime.min.replace(tzinfo=timezone.utc)

    def status(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [{
                "stream_id": sid, "name": w.name, "camera_id": w.camera_id, "class_id": w.class_id,
                "running": w.running, "attempts": w.attempts, "failed_attempts": w.failed_attempts,
                "unwatched_slots": w.unwatched_slots, "last_codes": list(w.last_codes),
                "start_time": w.start_time.isoformat() if w.start_time else None,
                "end_time": w.end_time.isoformat() if w.end_time else None,
            } for sid, w in self._workers.items()]
