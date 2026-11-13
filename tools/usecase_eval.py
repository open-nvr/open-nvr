#!/usr/bin/env python3
# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Score a use case end to end: play labelled clips through the fake-camera
rig, read the alerts inbox, compare with what each clip's ``expected.yml``
says should (and should not) have fired.

One folder per use case under ``data/fake-cameras/`` (gitignored). Beside
each clip sits ``<clip>.expected.yml``::

    use_case: intrusion
    apps: [intrusion-detection]          # picked for the eval camera before the clip plays
    expect:
      - alert_type: intrusion             # the app's alert type (apps_index.yml "emits")
        title_contains: ""                # optional substring, e.g. a plate number
        at_seconds: 12                    # when the event happens in the clip (0 = anywhere)
        within_seconds: 10                # tolerance after at_seconds (alert delay budget)
    no_alerts: [loitering]                # alert types that must NOT fire during this clip
    strict: false                         # true: an alert of an expected type whose title matches no
                                          # expected entry is a false positive (misread plates)
    config:                               # optional: app config applied before the clip (PUT /apps/{id}/config)
      intrusion-detection: {arm_mode: always}
    settle_seconds: 15                    # extra wait after the clip's length (default 20)

The harness stages one clip at a time as the camera ``_eval`` (the rig
turns a folder into a looping RTSP stream), waits the clip's length plus
the settle time, then scores the inbox rows that fired meanwhile. Every
expected event found inside its window is a true positive; an expected
event not found is a miss; any alert of a ``no_alerts`` type is a false
positive. Alert delay is fired_at minus (clip start + at_seconds).

Usage::

    python tools/usecase_eval.py --token "$TOKEN" data/fake-cameras/anpr
    python tools/usecase_eval.py --token "$TOKEN" --all data/fake-cameras

The token is a bearer from POST /api/v1/auth/login-json. ``--json`` writes the
raw per-clip rows; ``--report DIR`` writes the evidence pack: an index with
the accuracy table and one Markdown page per use case with every clip, what
was expected, what fired and when, the alert delays, and the alerts' own
snapshots, downloaded beside the page.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml

EVAL_CAMERA = "eval-cam"
FAKECAM_IP = os.getenv("FAKECAM_IP", "172.28.90.10")


# ── OpenNVR API ────────────────────────────────────────────────────────

class Api:
    """Thin client. Access tokens are short-lived; with a refresh token the
    client re-authenticates on the first 401 and retries the call once."""

    def __init__(self, base: str, token: str, refresh_token: str | None = None) -> None:
        self.c = httpx.Client(base_url=base.rstrip("/"), timeout=30,
                              headers={"Authorization": f"Bearer {token}"})
        self.refresh_token = refresh_token

    def _refresh(self) -> bool:
        if not self.refresh_token:
            return False
        r = self.c.post("/api/v1/auth/refresh", json={"refresh_token": self.refresh_token})
        if r.status_code != 200:
            return False
        body = r.json()
        self.c.headers["Authorization"] = f"Bearer {body['access_token']}"
        self.refresh_token = body.get("refresh_token", self.refresh_token)
        print("  (access token refreshed)", flush=True)
        return True

    def _call(self, method: str, p: str, body=None, params=None):
        r = self.c.request(method, p, json=body, params=params)
        if r.status_code == 401 and self._refresh():
            r = self.c.request(method, p, json=body, params=params)
        r.raise_for_status()
        return r.json() if r.content else None

    def get(self, p: str, **params):
        return self._call("GET", p, params=params)

    def post(self, p: str, body=None):
        return self._call("POST", p, body)

    def put(self, p: str, body=None, **params):
        return self._call("PUT", p, body, params)

    def get_bytes(self, p: str) -> bytes | None:
        r = self.c.get(p)
        if r.status_code == 401 and self._refresh():
            r = self.c.get(p)
        return r.content if r.status_code == 200 and r.content else None

    def version(self) -> str:
        try:
            return str((self.get("/health") or {}).get("version") or "?")
        except httpx.HTTPError:
            return "?"

    def ensure_eval_camera(self) -> int:
        listing = self.get("/api/v1/cameras/")
        cams = listing if isinstance(listing, list) else (listing.get("cameras") or listing.get("items") or [])
        url = f"rtsp://{FAKECAM_IP}:8554/{EVAL_CAMERA}"
        for cam in cams:
            if isinstance(cam, dict) and (cam.get("name") == EVAL_CAMERA or cam.get("rtsp_url") == url):
                if cam.get("name") != EVAL_CAMERA or cam.get("rtsp_url") != url:
                    self.put(f"/api/v1/cameras/{cam['id']}", {"name": EVAL_CAMERA, "rtsp_url": url})
                return int(cam["id"])
        try:
            cam = self.post("/api/v1/cameras/", {"name": EVAL_CAMERA, "ip_address": FAKECAM_IP, "port": 8554,
                                                 "rtsp_url": url})
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 409:
                raise
            # The rig's host:port is already a camera under another name
            # (a previous eval renamed it): take it back.
            rig = [c for c in cams if isinstance(c, dict) and str(c.get("ip_address")) == FAKECAM_IP]
            if not rig:
                raise
            cam = self.put(f"/api/v1/cameras/{rig[0]['id']}", {"name": EVAL_CAMERA, "rtsp_url": url})
        return int(cam["id"])

    def installed_apps(self) -> list[str]:
        apps = self.get("/api/v1/apps")
        return [a.get("id") for a in (apps if isinstance(apps, list) else apps.get("apps", [])) if a.get("id")]

    def unpick_camera_for_app(self, app_id: str, camera_id: int) -> None:
        try:
            skill = self.get(f"/api/v1/apps/{app_id}/cameras").get("skill")
            if skill:
                self.c.delete(f"/api/v1/skills/{skill}/cameras/{camera_id}", params={"consumer": f"app:{app_id}"})
        except httpx.HTTPError:
            pass

    def pick_camera_for_app(self, app_id: str, camera_id: int) -> None:
        info = self.get(f"/api/v1/apps/{app_id}/cameras")
        skill = info.get("skill")
        if not skill:
            raise SystemExit(f"{app_id}: no camera-picking skill; is the app installed and running?")
        self.put(f"/api/v1/skills/{skill}/cameras/{camera_id}",
                 {"consumer": f"app:{app_id}"}, consumer=f"app:{app_id}")

    def alerts_since(self, start: datetime, camera_id: int) -> list[dict]:
        rows, skip = [], 0
        while True:
            page = self.get("/api/v1/alerts-inbox", **{"from": start.isoformat(), "limit": 200, "skip": skip})
            items = page.get("alerts", page if isinstance(page, list) else [])
            rows += [a for a in items if str(a.get("camera_id")) in (str(camera_id), f"cam{camera_id}", f"cam-{camera_id}", "", "None")]
            if len(items) < 200:
                return rows
            skip += 200


# ── clips ─────────────────────────────────────────────────────────────

def clip_seconds(path: Path) -> float:
    """ffprobe on the host if present, else the ffmpeg image already pulled for the rig."""
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)]
    if shutil.which("ffprobe") is None:
        cmd = ["docker", "run", "--rm", "-v", f"{path.parent}:/v:ro", "--entrypoint", "ffprobe",
               "bluenviron/mediamtx:1.15.4-ffmpeg", "-v", "error", "-show_entries", "format=duration",
               "-of", "csv=p=0", f"/v/{path.name}"]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=120).stdout.strip()
    return float(out or 0) or 30.0


def stage(clip: Path, rig_dir: Path) -> Path:
    d = rig_dir / EVAL_CAMERA
    if d.exists():
        shutil.rmtree(d)
    d.mkdir(parents=True)
    shutil.copy(clip, d / clip.name)
    return d


def unstage(rig_dir: Path) -> None:
    shutil.rmtree(rig_dir / EVAL_CAMERA, ignore_errors=True)


def fired_at(a: dict) -> datetime | None:
    raw = a.get("fired_at") or a.get("created_at")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ── scoring ────────────────────────────────────────────────────────────

def score_clip(spec: dict, alerts: list[dict], start: datetime) -> dict:
    hits, misses, delays = [], [], []
    for exp in spec.get("expect", []):
        want_type = exp.get("alert_type")
        want_sub = (exp.get("title_contains") or "").upper()
        t0 = start.timestamp() + float(exp.get("at_seconds", 0))
        t1 = t0 + float(exp.get("within_seconds", 30)) if exp.get("at_seconds", 0) else float("inf")
        found = None
        for a in alerts:
            if want_type and a.get("alert_type") not in (want_type, None):
                continue
            if a.get("alert_type") is None and not want_sub:
                continue                        # untyped rows count only when a title is asked for
            if want_sub and want_sub not in str(a.get("title", "")).upper():
                continue
            fa = fired_at(a)
            if fa is None or not (t0 - 2 <= fa.timestamp() <= t1):
                continue
            found = a
            break
        if found:
            hits.append(exp)
            fa = fired_at(found)
            if fa and exp.get("at_seconds", 0):
                delays.append(round(fa.timestamp() - t0, 1))
        else:
            misses.append(exp)
    false_pos = [a for a in alerts if a.get("alert_type") in set(spec.get("no_alerts", []))]
    if spec.get("strict"):
        subs = [(e.get("title_contains") or "").upper() for e in spec.get("expect", [])]
        types = {e.get("alert_type") for e in spec.get("expect", [])}
        for a in alerts:
            if a.get("alert_type") in types or a.get("alert_type") is None:
                title = str(a.get("title", "")).upper()
                if subs and not any(sub and sub in title for sub in subs):
                    false_pos.append(a)
    fp_ids = {id(a) for a in false_pos}
    return {"hits": len(hits), "misses": len(misses), "false_positives": len(false_pos),
            "expected": len(spec.get("expect", [])), "delays_s": delays,
            "fired": [(a.get("alert_type"), a.get("title")) for a in alerts][:20],
            "false_positive_titles": [a.get("title") for a in false_pos][:10],
            "missed": [(m.get("alert_type"), m.get("title_contains")) for m in misses],
            # Everything the evidence page needs about each alert, in clip order
            # (the inbox returns newest first).
            "alerts": sorted([{"id": a.get("id"), "alert_id": a.get("alert_id"), "alert_type": a.get("alert_type"),
                               "title": a.get("title"), "severity": a.get("severity"),
                               "fired_at": (fired_at(a).isoformat() if fired_at(a) else None),
                               "offset_s": (round(fired_at(a).timestamp() - start.timestamp(), 1)
                                            if fired_at(a) else None),
                               "images": list(a.get("images") or []), "false_positive": id(a) in fp_ids}
                              for a in alerts][:40], key=lambda r: (r["offset_s"] is None, r["offset_s"] or 0))}


def run_folder(api: Api, folder: Path, rig_dir: Path, camera_id: int) -> list[dict]:
    rows = []
    specs = sorted(folder.glob("*.expected.yml"))
    if not specs:
        print(f"  {folder.name}: no *.expected.yml files, skipped")
        return rows
    for spec_path in specs:
        spec = yaml.safe_load(spec_path.read_text()) or {}
        clip = folder / spec_path.name.replace(".expected.yml", "")
        if not clip.exists():
            print(f"  {spec_path.name}: clip {clip.name} missing, skipped")
            continue
        wanted = set(spec.get("apps", []))
        for app_id in api.installed_apps():
            if app_id not in wanted:
                api.unpick_camera_for_app(app_id, camera_id)
        for app_id in wanted:
            api.pick_camera_for_app(app_id, camera_id)
        for app_id, cfg in (spec.get("config") or {}).items():
            current = api.get(f"/api/v1/apps/{app_id}/config").get("config") or {}
            api.put(f"/api/v1/apps/{app_id}/config", {**current, **cfg})
        length = clip_seconds(clip)
        settle = float(spec.get("settle_seconds", 20))
        stage(clip, rig_dir)
        # The rig republishes the same path for every clip; core's proxy
        # path and capture can be left holding the previous source. Ask core
        # to provision the camera afresh so the new publish is picked up.
        try:
            api.post(f"/api/v1/cameras/{camera_id}/provision-mediamtx")
        except httpx.HTTPError as exc:
            print(f"    (re-provision failed: {exc}; continuing)", flush=True)
        start = datetime.now(timezone.utc)              # the window opens with the stream: a first-loop alert must count
        pickup = 25                                     # rig scan, stream start, MediaMTX source retry, detector pickup
        print(f"  {clip.name}: playing {length:.0f}s (+{pickup}s pickup, +{settle:.0f}s settle)", flush=True)
        time.sleep(pickup + length + settle)
        alerts = api.alerts_since(start, camera_id)
        unstage(rig_dir)
        time.sleep(8)
        res = score_clip(spec, alerts, start)
        verdict = "PASS" if res["misses"] == 0 and res["false_positives"] == 0 else "FAIL"
        res.update({"use_case": spec.get("use_case", folder.name), "clip": clip.name, "verdict": verdict,
                    "length_s": round(length, 1), "started_at": start.isoformat(), "spec": spec})
        print(f"    {verdict}  hits {res['hits']}/{res['expected']}  false+ {res['false_positives']}"
              f"  delays {res['delays_s']}  fired {res['fired'][:4]}", flush=True)
        rows.append(res)
    return rows


def tally(rows: list[dict]) -> dict[str, dict]:
    """Per use case: clips, passes, expected, hits, false positives, delays."""
    by: dict[str, dict] = {}
    for r in rows:
        s = by.setdefault(r["use_case"], {"clips": 0, "pass": 0, "exp": 0, "hit": 0, "fp": 0, "delays": []})
        s["clips"] += 1
        s["exp"] += r["expected"]
        s["hit"] += r["hits"]
        s["fp"] += r["false_positives"]
        s["pass"] += int(r["misses"] == 0 and r["false_positives"] == 0)
        s["delays"] += r["delays_s"]
    return by


def _median(values: list[float]):
    return sorted(values)[len(values) // 2] if values else None


def summarise(rows: list[dict]) -> None:
    print("\nuse case            clips  pass  recall      false+  median delay")
    for uc, s in tally(rows).items():
        rec = f"{s['hit']}/{s['exp']}" if s["exp"] else "n/a"
        med = _median(s["delays"])
        print(f"{uc:19s} {s['clips']:5d}  {s['pass']:4d}  {rec:10s}  {s['fp']:6d}  {med if med is not None else 'n/a'}")


# ── the evidence pack ──────────────────────────────────────────────────

def _slug(text: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in str(text)).strip("-") or "x"


def _pct(num: int, den: int) -> str:
    return f"{100.0 * num / den:.0f} %" if den else "n/a"


def write_report(rows: list[dict], out_dir: Path, *, fetch_image=None, meta: dict | None = None) -> list[Path]:
    """The evidence pack: ``README.md`` with the accuracy table and one page
    per use case. ``fetch_image(row_id, name) -> bytes | None`` downloads an
    alert's snapshot; the pages embed what it returns. Returns the pages."""
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = meta or {}
    by = tally(rows)
    when = meta.get("generated_at") or datetime.now(timezone.utc).isoformat(timespec="seconds")
    index = ["# Use-case evidence pack\n",
             f"Generated {when} against {meta.get('base', '?')} (OpenNVR {meta.get('version', '?')}). "
             f"Each row is a use case; each page lists every clip played, what the clip's `expected.yml` "
             f"asked for, what fired, when, and the alert's own snapshot.\n",
             "| Use case | Clips | Pass | Recall | Precision | False + | Median delay | Max delay | Page |",
             "|---|---|---|---|---|---|---|---|---|"]
    pages: list[Path] = []
    for uc, s in by.items():
        page = out_dir / f"{_slug(uc)}.md"
        med, mx = _median(s["delays"]), (max(s["delays"]) if s["delays"] else None)
        index.append(f"| {uc} | {s['clips']} | {s['pass']}/{s['clips']} | {s['hit']}/{s['exp']} ({_pct(s['hit'], s['exp'])}) "
                     f"| {_pct(s['hit'], s['hit'] + s['fp'])} | {s['fp']} "
                     f"| {f'{med:.1f} s' if med is not None else 'n/a'} | {f'{mx:.1f} s' if mx is not None else 'n/a'} "
                     f"| [{page.name}]({page.name}) |")
        lines = [f"# {uc}\n",
                 f"{s['pass']} of {s['clips']} clip(s) passed; recall {s['hit']}/{s['exp']} ({_pct(s['hit'], s['exp'])}), "
                 f"precision {_pct(s['hit'], s['hit'] + s['fp'])}, {s['fp']} false positive(s)"
                 + (f", alert delay median {med:.1f} s, max {mx:.1f} s" if med is not None else "") + ".\n",
                 "| Clip | Length | Expected | Hits | Misses | False + | Delays | Verdict |", "|---|---|---|---|---|---|---|---|"]
        for r in [r for r in rows if r["use_case"] == uc]:
            lines.append(f"| {r['clip']} | {r.get('length_s', '?')} s | {r['expected']} | {r['hits']} | {r['misses']} "
                         f"| {r['false_positives']} | {', '.join(f'{d:.1f} s' for d in r['delays_s']) or '—'} "
                         f"| **{r.get('verdict', 'PASS' if r['misses'] == 0 and r['false_positives'] == 0 else 'FAIL')}** |")
        for r in [r for r in rows if r["use_case"] == uc]:
            lines += [f"\n## {r['clip']}\n", f"Played {r.get('started_at', '?')}, {r.get('length_s', '?')} s.\n",
                      "**Expected**\n"]
            spec = r.get("spec") or {}
            for e in spec.get("expect", []):
                where = (f"at {e['at_seconds']} s (+{e.get('within_seconds', 30)} s)" if e.get("at_seconds") else "anywhere")
                lines.append(f"- `{e.get('alert_type') or '*'}`"
                             + (f" titled *{e['title_contains']}*" if e.get("title_contains") else "") + f", {where}")
            if spec.get("no_alerts"):
                lines.append(f"- must not fire: {', '.join(f'`{t}`' for t in spec['no_alerts'])}")
            if r.get("missed"):
                lines.append("\n**Missed**\n")
                lines += [f"- `{t or '*'}`" + (f" titled *{sub}*" if sub else "") for t, sub in r["missed"]]
            lines.append("\n**Fired**\n")
            alerts = r.get("alerts") or []
            if not alerts:
                lines.append("- nothing")
            for i, a in enumerate(alerts):
                mark = " — **false positive**" if a.get("false_positive") else ""
                lines.append(f"- {a.get('offset_s', '?')} s: `{a.get('alert_type') or '—'}` {a.get('title')}"
                             f" ({a.get('severity')}){mark}")
                for name in a.get("images") or []:
                    if fetch_image is None or a.get("id") is None:
                        continue
                    data = fetch_image(a["id"], name)
                    if not data:
                        continue
                    img_dir = out_dir / _slug(uc)
                    img_dir.mkdir(exist_ok=True)
                    img = img_dir / f"{_slug(r['clip'])}-{i + 1}-{_slug(name)}.jpg"
                    img.write_bytes(data)
                    lines.append(f"\n  ![{name}]({img.relative_to(out_dir).as_posix()})\n")
        page.write_text("\n".join(lines) + "\n")
        pages.append(page)
    (out_dir / "README.md").write_text("\n".join(index) + "\n")
    return pages


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folder", type=Path, help="a use-case folder, or the fake-cameras root with --all")
    ap.add_argument("--all", action="store_true", help="run every use-case folder under the root")
    ap.add_argument("--base", default=os.getenv("OPENNVR_URL", "http://localhost:8000"))
    ap.add_argument("--token", default=os.getenv("OPENNVR_TOKEN"), help="bearer token (POST /api/v1/auth/login-json)")
    ap.add_argument("--refresh-token", default=os.getenv("OPENNVR_REFRESH_TOKEN"),
                    help="refresh token from the same login; lets a long run outlive the access token")
    ap.add_argument("--rig-dir", type=Path, default=None, help="data/fake-cameras (default: the folder's parent)")
    ap.add_argument("--json", type=Path, default=None, help="write per-clip rows here")
    ap.add_argument("--report", type=Path, default=None,
                    help="write the evidence pack here: README.md with the accuracy table, one page per use case, "
                         "the alerts' snapshots beside them")
    args = ap.parse_args()
    if not args.token:
        ap.error("--token or OPENNVR_TOKEN is required")
    api = Api(args.base, args.token, args.refresh_token)
    folders = sorted(p for p in args.folder.iterdir() if p.is_dir() and not p.name.startswith(("_", ".")) and p.name != EVAL_CAMERA) if args.all else [args.folder]
    rig_dir = args.rig_dir or (args.folder if args.all else args.folder.parent)
    camera_id = api.ensure_eval_camera()
    print(f"eval camera {EVAL_CAMERA} = id {camera_id}; rig dir {rig_dir}")
    rows: list[dict] = []
    try:
        for f in folders:
            print(f"\n== {f.name}")
            rows += run_folder(api, f, rig_dir, camera_id)
    finally:
        unstage(rig_dir)
    summarise(rows)
    if args.json:
        args.json.write_text(json.dumps(rows, indent=1, default=str))
        print(f"\nrows written to {args.json}")
    if args.report:
        pages = write_report(
            rows, args.report,
            fetch_image=lambda row_id, name: api.get_bytes(f"/api/v1/alerts-inbox/{row_id}/images/{name}"),
            meta={"base": args.base, "version": api.version()})
        print(f"evidence pack written to {args.report} ({len(pages)} page(s) + README.md)")
    return 0 if all(r["misses"] == 0 and r["false_positives"] == 0 for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
