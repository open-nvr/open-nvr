#!/usr/bin/env python3
"""Markdown tables from runs.jsonl: compute and quality proxies per combination."""
import json, sys
runs = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
print("| backend | fps | cap | pipeline CPU % | mediamtx CPU % | crops/s | det ms/frame | frames/s achieved (target) | dropped | shed? (budget/configured) | capped frames | visits/min | person det/min | car det/min | spawns dropped |")
print("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
for r in runs:
    s = r["window_s"]; cams = r["cameras"]
    crops = sum(c["crops"] for c in cams.values()) / s
    lat = [c["det_ms_mean"] for c in cams.values() if c["det_ms_mean"]]
    ach = sum((c["processing_fps"] or 0) for c in cams.values())
    tgt = sum((c["target_fps"] or 0) for c in cams.values())
    dropped = sum(c["dropped"] for c in cams.values())
    shed = ", ".join(f"{k}:{int(c['budget_end'] or 0)}/{int(c['configured'] or 0)}" for k, c in cams.items())
    capped = sum(c["capped"] for c in cams.values())
    visits = sum(c["visits"] for c in cams.values()) * 60 / s
    person = sum(c["detections"].get("person", 0) for c in cams.values()) * 60 / s
    car = sum(sum(v for l, v in c["detections"].items() if l in ("car", "truck", "bus", "motorcycle")) for c in cams.values()) * 60 / s
    sd = sum(c["spawns_dropped"] for c in cams.values())
    cpu = r["container_cpu_mean_pct"]
    print(f"| {r.get('backend','auto')} | {r['fps']} | {r['cap']} | {cpu.get('opennvr_detect_pipeline','?')} | {cpu.get('opennvr_mediamtx','?')} | {crops:.1f} | {sum(lat)/len(lat):.0f} | {ach:.1f} ({tgt:.0f}) | {dropped:.0f} | {shed} | {capped:.0f} | {visits:.1f} | {person:.0f} | {car:.0f} | {sd:.0f} |")
print()
print("Per camera (frames/s achieved of target; crops per frame; visits in window):")
for r in runs:
    row = "; ".join(f"{k}: {c['processing_fps'] or 0:.1f}/{c['target_fps'] or 0:.0f} fps, {c['crops']/max(c['det_calls'],1):.1f} crops/frame, {c['visits']:.0f} visits" for k, c in r["cameras"].items())
    print(f"- {r.get('backend','auto')} fps {r['fps']} cap {r['cap']}: {row}")
