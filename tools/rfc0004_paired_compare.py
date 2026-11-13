#!/usr/bin/env python3
# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""RFC-0004 step 1, the accuracy half: both prompts on the same frames.

Traffic windows say what the single call COSTS; they cannot say what it
gets right, because the visits in the two windows differ. This runs the
old path and the new one on the SAME evidence frames and compares answer
by answer:

  A  the plain caption call             (task=scene_caption)
  B  one question per descriptor kind   (the per-question path)
  C  the one structured call            (structured_enrichment.build_prompt)

and reports, per kind, how often C agrees with B after both go through
``normalise_answer``; how often C's answer parsed as JSON at all, and
whether the reply looks cut by the adapter's output-token cap; the
adapter's own inference time per arm, so "old = A + B" against "new = C"
is the same model on the same frame; and caption length in words.

It is run on the box as a one-off container from the core image, on the
compose network, with the recordings mounted read-only — it changes
nothing in the running stack and writes only to its output directory:

    docker run --rm --network open-nvr_opennvr_internal \\
      -v /path/to/recordings:/app/recordings:ro -v /tmp/paired:/out \\
      -e INTERNAL_API_KEY -e DATABASE_URL=sqlite:////tmp/x.db \\
      -e SECRET_KEY=x -e MEDIAMTX_SECRET=x -e CREDENTIAL_ENCRYPTION_KEY=<fernet> \\
      -w /app/server --entrypoint /app/server-venv/bin/python ghcr.io/open-nvr/core:main \\
      /out/rfc0004_paired_compare.py \\
      --frames /out/frames.tsv --out /out/paired.jsonl

``frames.tsv`` is ``event_id<TAB>label<TAB>camera_id<TAB>evidence_rel``
one visit a line (a psql query over ``events``). Calls are sequential —
one in flight — so the run is one more enricher's worth of load on the
accelerator, not a burst. The summary is printed as JSON at the end and
every frame's raw answers go to ``--out`` for a second look.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, "/app/server")

from services.descriptor_enrichment import (  # noqa: E402
    KIND_QUESTIONS,
    kinds_for,
    normalise_answer,
)
from services.structured_enrichment import build_prompt, parse_answer  # noqa: E402

EVIDENCE_ROOT = os.environ.get("EVIDENCE_ROOT", "/app/recordings/evidence")


def call(adapter: str, body: dict, timeout: float) -> tuple[dict | None, float, str | None]:
    """POST /infer. Returns (response json or None, wall ms, error text)."""
    data = json.dumps(body).encode()
    url = adapter if "/api/v1/infer/" in adapter else adapter.rstrip("/") + "/infer"
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "X-Internal-Api-Key": os.environ.get("INTERNAL_API_KEY", "")})
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            out = json.loads(resp.read().decode())
            return out, (time.monotonic() - t0) * 1000, None
    except urllib.error.HTTPError as exc:
        return None, (time.monotonic() - t0) * 1000, f"http {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return None, (time.monotonic() - t0) * 1000, type(exc).__name__


def text_of(resp: dict | None) -> str:
    result = (resp or {}).get("result") or {}
    for key in ("answer", "caption", "text", "description"):
        v = result.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


def ms_of(resp: dict | None, wall: float) -> float:
    v = (resp or {}).get("inference_ms")
    return float(v) if isinstance(v, (int, float)) else wall


def one_frame(adapter: str, ev: dict, jpeg_b64: str, kinds: list[str], timeout: float,
              people_extended: bool) -> dict:
    base = {"frame_b64": jpeg_b64, "camera_id": f"cam{ev['camera_id']}", "event_id": ev["id"]}
    rec: dict = {"id": ev["id"], "label": ev["label"], "camera_id": ev["camera_id"], "kinds": kinds}

    # A — plain caption
    resp, wall, err = call(adapter, {**base, "task": "scene_caption"}, timeout)
    rec["a"] = {"ms": ms_of(resp, wall), "wall": wall, "caption": text_of(resp), "err": err}

    # B — one question per kind
    rec["b"] = {}
    for kind in kinds:
        q = str(KIND_QUESTIONS[kind]["question"])
        resp, wall, err = call(adapter, {**base, "question": q, "prompt": q}, timeout)
        raw = text_of(resp)
        rec["b"][kind] = {"ms": ms_of(resp, wall), "wall": wall, "raw": raw,
                          "norm": normalise_answer(kind, raw) if raw else None, "err": err}

    # C — the structured call
    prompt = build_prompt(kinds)
    resp, wall, err = call(adapter, {**base, "question": prompt, "prompt": prompt}, timeout)
    text = text_of(resp)
    parsed = parse_answer(text)
    c: dict = {"ms": ms_of(resp, wall), "wall": wall, "err": err, "text_len": len(text),
               "json_ok": parsed is not None,
               # Cut by the output-token cap: no closing brace once a code
               # fence is stripped (Gemma wraps its JSON in one).
               "truncated": bool(text) and not text.rstrip().rstrip("`").rstrip().endswith("}"),
               "caption": "", "answers": {}}
    if parsed is not None:
        cap = parsed.get("caption")
        c["caption"] = cap.strip() if isinstance(cap, str) else ""
        for kind in kinds:
            raw = parsed.get(kind)
            raw = raw.strip() if isinstance(raw, str) else ""
            c["answers"][kind] = {"raw": raw,
                                  "norm": None if (not raw or raw.lower() == "unknown")
                                  else normalise_answer(kind, raw)}
    else:
        c["prose"] = text[:300]
    rec["c"] = c
    return rec


def summarise(rows: list[dict]) -> dict:
    def mean(xs):
        xs = [x for x in xs if x is not None]
        return round(statistics.fmean(xs), 1) if xs else None

    def p50(xs):
        xs = sorted(x for x in xs if x is not None)
        return round(xs[len(xs) // 2], 1) if xs else None

    out: dict = {"frames": len(rows), "by_label": {}}
    for lab in sorted({r["label"] for r in rows}):
        out["by_label"][lab] = sum(1 for r in rows if r["label"] == lab)
    ok = [r for r in rows if not r["a"]["err"] and not r["c"]["err"]
          and not any(v["err"] for v in r["b"].values())]
    out["frames_with_all_calls_ok"] = len(ok)
    old = [r["a"]["ms"] + sum(v["ms"] for v in r["b"].values()) for r in ok]
    new = [r["c"]["ms"] for r in ok]
    out["adapter_ms"] = {
        "A_caption": {"mean": mean([r["a"]["ms"] for r in ok]), "p50": p50([r["a"]["ms"] for r in ok])},
        "B_question": {"mean": mean([v["ms"] for r in ok for v in r["b"].values()]),
                       "p50": p50([v["ms"] for r in ok for v in r["b"].values()])},
        "C_structured": {"mean": mean(new), "p50": p50(new)},
        "old_path_A_plus_B": {"mean": mean(old), "p50": p50(old)},
        "ratio_old_over_new": round(statistics.fmean(old) / statistics.fmean(new), 2) if ok and statistics.fmean(new) else None,
        "calls_old_per_frame": mean([1 + len(r["b"]) for r in ok]),
    }
    out["structured"] = {
        "json_ok": sum(1 for r in ok if r["c"]["json_ok"]),
        "json_failed": sum(1 for r in ok if not r["c"]["json_ok"]),
        "truncated_reply": sum(1 for r in ok if r["c"]["truncated"]),
        "empty_caption_in_json": sum(1 for r in ok if r["c"]["json_ok"] and not r["c"]["caption"]),
        "text_len_mean": mean([r["c"]["text_len"] for r in ok]),
    }
    out["caption_words"] = {
        "A_mean": mean([len(r["a"]["caption"].split()) for r in ok if r["a"]["caption"]]),
        "C_mean": mean([len(r["c"]["caption"].split()) for r in ok if r["c"]["caption"]]),
    }
    kinds = sorted({k for r in ok for k in r["kinds"]})
    out["per_kind"] = {}
    for kind in kinds:
        rs = [r for r in ok if kind in r["kinds"]]
        b = [r["b"][kind]["norm"] for r in rs]
        c = [(r["c"]["answers"].get(kind) or {}).get("norm") if r["c"]["json_ok"] else None for r in rs]
        js = [r for r in rs if r["c"]["json_ok"]]
        bj = [r["b"][kind]["norm"] for r in js]
        cj = [(r["c"]["answers"].get(kind) or {}).get("norm") for r in js]
        out["per_kind"][kind] = {
            "frames": len(rs),
            "B_answered": sum(1 for x in b if x),
            "C_answered": sum(1 for x in c if x),
            "both_answered_and_agree": sum(1 for x, y in zip(bj, cj) if x and y and x == y),
            "both_answered_and_differ": sum(1 for x, y in zip(bj, cj) if x and y and x != y),
            "B_only": sum(1 for x, y in zip(bj, cj) if x and not y),
            "C_only": sum(1 for x, y in zip(bj, cj) if y and not x),
            "neither": sum(1 for x, y in zip(bj, cj) if not x and not y),
            "B_raw_not_in_vocabulary": sum(1 for r in rs if r["b"][kind]["raw"] and not r["b"][kind]["norm"]),
            "C_raw_not_in_vocabulary": sum(1 for r in js if (r["c"]["answers"].get(kind) or {}).get("raw")
                                          and (r["c"]["answers"][kind]["raw"].lower() != "unknown")
                                          and not r["c"]["answers"][kind]["norm"]),
        }
    out["errors"] = {
        "A": sum(1 for r in rows if r["a"]["err"]),
        "B": sum(1 for r in rows for v in r["b"].values() if v["err"]),
        "C": sum(1 for r in rows if r["c"]["err"]),
    }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--frames", required=True, help="TSV: event_id, label, camera_id, evidence_rel")
    ap.add_argument("--adapter", default="http://opennvr_core:8100/api/v1/infer/litertlm",
                    help="KAI-C's infer route for the captioner (the path core itself uses, "
                         "authenticated with INTERNAL_API_KEY), or an adapter base URL")
    ap.add_argument("--out", required=True, help="JSONL, one line per frame")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--timeout", type=float, default=90.0)
    ap.add_argument("--people-extended", action="store_true",
                    help="ask the five person kinds (EVENTS_DESCRIPTOR_PEOPLE_EXTENDED)")
    args = ap.parse_args()

    frames = []
    with open(args.frames, encoding="utf-8") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 4 or not parts[3]:
                continue
            frames.append({"id": int(parts[0]), "label": parts[1].lower(),
                           "camera_id": int(parts[2]), "rel": parts[3]})
    if args.limit:
        frames = frames[:args.limit]

    rows: list[dict] = []
    started = time.monotonic()
    with open(args.out, "w", encoding="utf-8") as out:
        for i, ev in enumerate(frames, 1):
            jpeg = b""
            for rel in (ev["rel"], ev["rel"] + ".jpg"):
                try:
                    with open(os.path.join(EVIDENCE_ROOT, rel), "rb") as fh:
                        jpeg = fh.read()
                    break
                except OSError:
                    continue
            if not jpeg:
                print(f"[{i}/{len(frames)}] {ev['id']} evidence missing", file=sys.stderr)
                continue
            kinds = list(kinds_for(ev["label"], extended=args.people_extended))
            if not kinds:
                continue
            rec = one_frame(args.adapter, ev, base64.b64encode(jpeg).decode("ascii"), kinds,
                            args.timeout, args.people_extended)
            rows.append(rec)
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            out.flush()
            c = rec["c"]
            print(f"[{i}/{len(frames)}] {ev['label']:7s} A {rec['a']['ms']:6.0f} ms  "
                  f"B {sum(v['ms'] for v in rec['b'].values()):6.0f} ms  C {c['ms']:6.0f} ms  "
                  f"json={'ok' if c['json_ok'] else 'NO'}{' cut' if c['truncated'] else ''}",
                  file=sys.stderr)
    summary = summarise(rows)
    summary["wall_s"] = round(time.monotonic() - started, 1)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
