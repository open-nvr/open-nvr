"""Staff attendance from a department's camera.

A department is a camera plus a working window. Inside the window the
worker looks at the camera every ``staff_poll_seconds`` and records every
staff member it recognises: the first sighting of the day opens the row
(check-in), every later one moves ``last_seen`` (check-out is the last
look that saw them). Nobody is marked ABSENT in storage — the report
synthesises ABSENT for staff of the department with no row that day.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

from ..config import get_settings
from ..storage.staff_repo import department_list, get_staff, staff_mark_seen
from ..utils.time import now_local
from .recognize import recognize_faces_in_frame

logger = logging.getLogger(__name__)


def in_window(start: str, end: str, now: datetime) -> bool:
    hhmm = now.strftime("%H:%M")
    if start <= end:
        return start <= hhmm < end
    return hhmm >= start or hhmm < end  # a window over midnight


@dataclass
class StaffWorker:
    department: Dict[str, Any]
    recognizer: Any = None
    frames: Any = None
    running: bool = False
    thread: Optional[threading.Thread] = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    polls: int = 0
    last_codes: List[str] = field(default_factory=list)
    last_poll_at: Optional[str] = None
    last_error: Optional[str] = None
    clock: Callable[[], datetime] = now_local
    poll_seconds: Optional[float] = None
    tick_seconds: float = 1.0

    @property
    def department_id(self) -> int:
        return int(self.department["id"])

    def start(self) -> None:
        if self.running:
            return
        self.running = True
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, daemon=True, name=f"attendly-staff-{self.department_id}")
        self.thread.start()
        logger.info("Staff worker %s started for %s", self.department_id, self.department.get("name"))

    def stop(self) -> None:
        if not self.running:
            return
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5.0)
        self.running = False

    def active_now(self, now: Optional[datetime] = None) -> bool:
        now = now or self.clock()
        return in_window(str(self.department.get("work_start") or "00:00"),
                         str(self.department.get("work_end") or "23:59"), now)

    def tick(self) -> List[str]:
        """One look; returns the staff codes marked seen."""
        now = self.clock()
        if not self.active_now(now):
            return []
        self.polls += 1
        self.last_poll_at = now.isoformat()
        cam = self.department.get("camera_id")
        if self.frames is None or cam is None:
            return []
        try:
            frame = self.frames.snapshot(int(cam))
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            return []
        if frame is None:
            return []
        seen: List[str] = []
        for face in recognize_faces_in_frame(self.recognizer, frame, tag=f"dept{self.department_id}"):
            code = face.get("code")
            if not code or code in seen:
                continue
            if get_staff(code) is None:
                continue  # a student walking past the staff room
            staff_mark_seen(code, date=now.strftime("%Y-%m-%d"), ts_iso=now.isoformat(), department_id=self.department_id)
            seen.append(code)
        self.last_codes = seen
        if seen:
            logger.info("Department %s saw %s", self.department.get("name"), ",".join(seen))
        return seen

    def _run(self) -> None:
        settings = get_settings()
        poll = self.poll_seconds if self.poll_seconds is not None else settings.staff_poll_seconds
        next_at = 0.0
        try:
            while not self.stop_event.is_set():
                if time.monotonic() >= next_at:
                    try:
                        self.tick()
                    except Exception as exc:  # noqa: BLE001
                        self.last_error = str(exc)
                        logger.exception("Staff worker %s tick error", self.department_id)
                    next_at = time.monotonic() + poll
                self.stop_event.wait(self.tick_seconds)
        finally:
            self.running = False

    def status(self) -> Dict[str, Any]:
        return {
            "department_id": self.department_id, "name": self.department.get("name"),
            "camera_id": self.department.get("camera_id"), "running": self.running, "active": self.active_now(),
            "window": f"{self.department.get('work_start')}-{self.department.get('work_end')}",
            "polls": self.polls, "last_poll_at": self.last_poll_at, "last_codes": list(self.last_codes),
            "last_error": self.last_error,
        }


class StaffEngine:
    _instance: Optional["StaffEngine"] = None

    def __init__(self, *, recognizer: Any = None, frames: Any = None, refresh_seconds: float = 15.0) -> None:
        self._lock = threading.RLock()
        self._workers: Dict[int, StaffWorker] = {}
        self._recognizer = recognizer
        self._frames = frames
        self._refresh = refresh_seconds
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @classmethod
    def get_shared(cls) -> "StaffEngine":
        if cls._instance is None:
            cls._instance = StaffEngine()
        return cls._instance

    @classmethod
    def set_shared(cls, engine: Optional["StaffEngine"]) -> None:
        cls._instance = engine

    def _deps(self):
        if self._recognizer is None:
            try:
                from ..face.engine import FaceEngine

                self._recognizer = FaceEngine.get_shared()
            except Exception as exc:  # noqa: BLE001
                logger.warning("face engine unavailable for staff attendance: %s", exc)
        if self._frames is None:
            try:
                from ..cameras import CameraSource

                self._frames = CameraSource.get_shared()
            except Exception as exc:  # noqa: BLE001
                logger.warning("camera source unavailable for staff attendance: %s", exc)
        return self._recognizer, self._frames

    def reload(self) -> None:
        wanted = {int(d["id"]): d for d in department_list() if d.get("camera_id") is not None}
        recognizer, frames = self._deps()
        with self._lock:
            for did in [d for d in self._workers if d not in wanted]:
                self._workers.pop(did).stop()
            for did, dep in wanted.items():
                w = self._workers.get(did)
                if w is not None and w.running and w.department.get("camera_id") == dep.get("camera_id"):
                    w.department = dep
                    continue
                if w is not None:
                    w.stop()
                w = StaffWorker(department=dep, recognizer=recognizer, frames=frames)
                self._workers[did] = w
                w.start()

    def start(self) -> None:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
        try:
            self.reload()
        except Exception:
            logger.exception("initial department load failed; the refresh loop will retry")
        with self._lock:
            self._thread = threading.Thread(target=self._loop, daemon=True, name="attendly-staff")
            self._thread.start()
        logger.info("StaffEngine started")

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self._refresh)
            if self._stop.is_set():
                break
            try:
                self.reload()
            except Exception:
                logger.exception("staff refresh error")

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for w in workers:
            try:
                w.stop()
            except Exception:
                pass
        logger.info("StaffEngine stopped")

    def status(self) -> List[Dict[str, Any]]:
        with self._lock:
            return [w.status() for w in self._workers.values()]
