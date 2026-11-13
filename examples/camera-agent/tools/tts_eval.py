#!/usr/bin/env python3
# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""TTS eval: the agent's replies through Piper (shipped) and Kokoro-82M on CPU.

Real-time factor (generation seconds / audio seconds; under 1 means the
reply can play as it is made), seconds to make a one-sentence reply, and
intelligibility: the audio back through whisper small.en, WER against
the text. Twenty replies the agent actually gives. Run both engines on
the same machine; espeak-ng must be installed (both engines phonemise
with it — on macOS the pip wheels' bundled data is broken, run this in a
Linux container).

    python tools/tts_eval.py --engine piper --out piper.json
    python tools/tts_eval.py --engine kokoro --out kokoro.json

Results are in MODELS_AND_LATENCY.md, "Measured: voice".
"""
import json, os, re, sys, time, statistics, wave
os.environ.setdefault("OMP_NUM_THREADS", "4")
import argparse
ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--engine", choices=["piper", "kokoro"], required=True)
ap.add_argument("--piper-voice", default="en_US-lessac-medium.onnx", help="path to the Piper voice .onnx (its .json beside it)")
ap.add_argument("--kokoro-model", default="kokoro-v1.0.onnx")
ap.add_argument("--kokoro-voices", default="voices-v1.0.bin")
ap.add_argument("--out", required=True)
args = ap.parse_args()
ENGINE, OUT = args.engine, args.out
import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402
import jiwer  # noqa: E402
os.makedirs("out", exist_ok=True)
sents = ["I see two people at the front door; one is carrying a package.", "No one is in the yard right now.",
 "Armed the after-hours alarm on the loading bay camera until six a.m.", "A white van came in at ten past three and left at half past.",
 "The gate is open. There is a car in the driveway.", "I can't see right now; the camera is offline.", "Happy to help. Say the word when you want me to look at a camera.",
 "Three cars are parked in the lot, and one truck is at the gate.", "Nobody came by in the last twenty minutes.", "The man near the gate is wearing a yellow jacket and a hard hat.",
 "Watching the parking lot for trucks until six.", "I found a red truck at four fifteen yesterday afternoon on the street camera.",
 "The crowd monitor is off.", "Overnight was quiet: a fox at two, and the guard at one and three.", "There is a dog in the garden, near the fence.",
 "The back door camera shows an empty hallway.", "The side entrance alarm is off.", "Priya was here at nine this morning.", "It is dark outside and the porch light is on.", "Thanks. Anything else?"]
t_setup = time.perf_counter()
if ENGINE == "piper":
    from piper import PiperVoice
    v = PiperVoice.load(args.piper_voice)
    sr = v.config.sample_rate
    def synth(t):
        chunks = [c.audio_int16_array if hasattr(c, "audio_int16_array") else np.frombuffer(c.audio_int16_bytes, dtype=np.int16) for c in v.synthesize(t)]
        return np.concatenate(chunks).astype(np.float32) / 32768.0, sr
else:
    from kokoro_onnx import Kokoro
    k = Kokoro(args.kokoro_model, args.kokoro_voices)
    def synth(t):
        a, sr = k.create(t, voice="af_heart", speed=1.0, lang="en-us"); return a.astype(np.float32), sr
load_s = time.perf_counter() - t_setup
synth("Hello.")  # warm
rtf, gen, dur = [], [], []
for i, t in enumerate(sents):
    t0 = time.perf_counter(); a, sr = synth(t); g = time.perf_counter() - t0
    d = len(a) / sr; rtf.append(g / d); gen.append(g); dur.append(d)
    sf.write(f"out/{ENGINE}-{i:02d}.wav", a, sr)
from faster_whisper import WhisperModel
m = WhisperModel("small.en", device="cpu", compute_type="int8", cpu_threads=4)
norm = jiwer.Compose([jiwer.ToLowerCase(), jiwer.RemovePunctuation(), jiwer.RemoveMultipleSpaces(), jiwer.Strip()])
hyps = []
for i in range(len(sents)):
    segs, _ = m.transcribe(f"out/{ENGINE}-{i:02d}.wav", beam_size=1, language="en"); hyps.append(" ".join(s.text for s in segs))
wer = jiwer.wer([norm(s) for s in sents], [norm(h) for h in hyps])
out = {"engine": ENGINE, "load_s": round(load_s, 2), "rtf_p50": round(statistics.median(rtf), 3), "rtf_p95": round(sorted(rtf)[int(len(rtf)*0.95)], 3),
       "gen_s_p50": round(statistics.median(gen), 2), "audio_s_p50": round(statistics.median(dur), 2), "whisper_wer": round(wer, 4), "sample_rate": sr}
json.dump(out, open(OUT, "w")); print(json.dumps(out))
