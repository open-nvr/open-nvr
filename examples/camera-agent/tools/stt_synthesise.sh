#!/usr/bin/env bash
# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
# Make the STT eval set on a Mac: the agent's eval-case phrasings plus a
# few more of the same kind, spoken by four macOS voices into 16 kHz
# mono WAV. Usage: tools/stt_synthesise.sh <out-dir>
set -euo pipefail
out="${1:?out dir}"; mkdir -p "$out/audio"; cd "$(dirname "$0")"
python3 - "$out" <<'PY'
import json, pathlib, re, subprocess, sys, yaml
out = pathlib.Path(sys.argv[1])
texts = []
for c in yaml.safe_load(open("eval_cases.yml")):
    for turn in c.get("turns", []) or []:
        t = turn if isinstance(turn, str) else (turn.get("ask") or "")
        if t: texts.append(t)
    if c.get("ask"): texts.append(c["ask"])
texts += ["Is anyone at the front door right now?", "How many cars are in the driveway?", "Did a delivery van come by this morning?",
 "Alarm if a person is detected after ten p.m. on the yard camera.", "What is the man near the gate wearing?", "Show me the loading bay.",
 "Was Priya here today?", "Notify me when more than three people gather at the entrance.", "Stop the crowd monitor.", "What happened overnight?",
 "Is the gate open?", "Did anyone come in the last twenty minutes?", "Count the people on camera two.", "Describe the back door camera.",
 "Watch the parking lot for trucks until six.", "Thanks, that's all.", "Hi, what can you do?", "Is there a dog in the garden?",
 "Turn off the alarm on the side entrance.", "Search the footage for a red truck yesterday afternoon."]
texts = [re.sub(r"\{camera\}", "the yard camera", t).strip() for t in texts]
seen = set(); uniq = [t for t in texts if not (t.lower() in seen or seen.add(t.lower()))]
json.dump(uniq, open(out / "sentences.json", "w"), indent=1)
for i, t in enumerate(uniq):
    voice = ["Samantha", "Daniel", "Karen", "Rishi"][i % 4]
    subprocess.run(["say", "-v", voice, "-o", str(out / f"audio/{i:03d}.aiff"), t], check=True)
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(out / f"audio/{i:03d}.aiff"), str(out / f"audio/{i:03d}.wav")], check=True)
    (out / f"audio/{i:03d}.aiff").unlink()
print(len(uniq), "utterances in", out)
PY
