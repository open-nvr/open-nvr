#!/usr/bin/env python3
# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""STT eval: word error rate and seconds per utterance on the agent's own phrasings.

The test set is the agent's eval-case phrasings plus twenty more of the
same kind, spoken by macOS voices into 16 kHz mono WAV (``tools/stt_synthesise.sh``
makes the set on a Mac; any directory of ``sentences.json`` +
``audio/NNN.wav`` works). Scoring uses Whisper's own English text
normaliser, so "10 pm" and "10pm", "okay" and "ok" are not errors.

Two engines, each the way its adapter runs it: ``whisper`` is
faster-whisper (int8, 4 threads), ``onnx`` is onnx-asr (the parakeet
adapter). Run every model on the same machine; the seconds column only
compares within one run.

    python tools/stt_eval.py --engine onnx --model nemo-parakeet-tdt-0.6b-v3 --data stt-set --out parakeet.json
    python tools/stt_eval.py --engine whisper --model base.en --data stt-set --out whisper.json

Results are in MODELS_AND_LATENCY.md, "Measured: speech".
"""
import glob, json, os, re, sys, time, statistics
os.environ.setdefault("OMP_NUM_THREADS", "4")
import argparse
ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--engine", choices=["whisper", "onnx"], required=True, help="faster-whisper (the whisper adapter) or onnx-asr (the parakeet adapter)")
ap.add_argument("--model", required=True, help="whisper: base.en / small.en; onnx: nemo-parakeet-tdt-0.6b-v3, whisper-base, ...")
ap.add_argument("--data", required=True, help="a directory with sentences.json and audio/NNN.wav (16 kHz mono), see --synthesise")
ap.add_argument("--quant", default="int8", help="onnx-asr quantization (int8) or '' for fp32")
ap.add_argument("--out", required=True)
args = ap.parse_args()
KIND, NAME, OUT = args.engine, args.model, args.out
import jiwer  # noqa: E402
os.chdir(args.data)
sents = json.load(open("sentences.json")); wavs = sorted(glob.glob("audio/*.wav"))
from whisper_normalizer.english import EnglishTextNormalizer
_en = EnglishTextNormalizer()
def fix(s):
    # Whisper's own normaliser (numbers, spellings, ok/okay), then the
    # compounds this phrasing set trips on.
    s = _en(s)
    for a, b in (("backdoor", "back door"), ("p m", "pm"), ("a m", "am"), (" pm", "pm"), (" am", "am"), ("ok ", "okay ")):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()
if KIND == "whisper":
    from faster_whisper import WhisperModel
    m = WhisperModel(NAME, device="cpu", compute_type="int8", cpu_threads=4)
    def run(w):
        segs, _ = m.transcribe(w, beam_size=1, language="en", vad_filter=False)
        return " ".join(s.text for s in segs)
else:
    import onnx_asr
    m = onnx_asr.load_model(NAME, quantization=(args.quant or None))
    def run(w): return m.recognize(w)
run(wavs[0])  # warm
hyps, secs = [], []
for w in wavs:
    t0 = time.perf_counter(); h = run(w); secs.append(time.perf_counter() - t0); hyps.append(h)
refs = [fix(s) for s in sents]; hyp = [fix(h) for h in hyps]
wer = jiwer.wer(refs, hyp)
worst = sorted(zip(refs, hyp), key=lambda rh: -jiwer.wer(rh[0], rh[1]))[:3]
out = {"model": f"{KIND}:{NAME}", "n": len(wavs), "wer": round(wer, 4), "hyps": hyps, "sec_p50": round(statistics.median(secs), 2), "sec_p95": round(sorted(secs)[int(len(secs)*0.95)], 2), "worst": worst}
json.dump(out, open(OUT, "w"), indent=1); print(json.dumps({k: v for k, v in out.items() if k != "worst"}))
