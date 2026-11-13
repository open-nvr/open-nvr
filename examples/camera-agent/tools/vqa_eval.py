#!/usr/bin/env python3
# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""VQA eval: can a vision model answer the questions the camera agent asks?

The questions are the agent's (``describe_camera`` with a question, the
"is there a / how many" family), the frames are coco128's and the truths
are its labels, so the score is automatic and the same for every model:

  present   "Is there a <class that IS in the frame>?"  → yes
  absent    "Is there a <class that is NOT>?"           → no   (a yes is a
            hallucination, the failure that matters most on a camera)
  count     "How many people?"                          → the label count,
            scored exact and within one

Two ways to ask: an Ollama model (what ``CAPTION_ADAPTER=ollamavlm``
serves; ``/api/generate`` with ``prompt`` + ``images``, as the adapter
sends it) or ``adapter:<url>``, a shipped adapter's ``/infer`` (multipart
frame + ``params.question``), which is how the moondream container the
stack ships is measured rather than Ollama's moondream.

    python tools/vqa_eval.py --model gemma3:4b --data datasets/coco128 --out gemma.json
    python tools/vqa_eval.py --model adapter:http://127.0.0.1:9008 --token $TOKEN ...
    python tools/vqa_eval.py --model qwen3-vl:2b --cpu --limit 12 ...   # CPU latency

coco128: https://github.com/ultralytics/assets/releases/download/v0.0.0/coco128.zip
Results are in MODELS_AND_LATENCY.md, "Measured: the vision models".
"""
import base64, glob, json, os, random, re, statistics, sys, time, urllib.request

COCO = ["person","bicycle","car","motorcycle","airplane","bus","train","truck","boat","traffic light","fire hydrant","stop sign","parking meter","bench","bird","cat","dog","horse","sheep","cow","elephant","bear","zebra","giraffe","backpack","umbrella","handbag","tie","suitcase","frisbee","skis","snowboard","sports ball","kite","baseball bat","baseball glove","skateboard","surfboard","tennis racket","bottle","wine glass","cup","fork","knife","spoon","bowl","banana","apple","sandwich","orange","broccoli","carrot","hot dog","pizza","donut","cake","chair","couch","potted plant","bed","dining table","toilet","tv","laptop","mouse","remote","keyboard","cell phone","microwave","oven","toaster","sink","refrigerator","book","clock","vase","scissors","teddy bear","hair drier","toothbrush"]
import argparse
ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--model", required=True, help="an Ollama model name, or adapter:<url> for a shipped adapter's /infer")
ap.add_argument("--data", required=True, help="a coco128 directory (images/train2017, labels/train2017)")
ap.add_argument("--url", default="http://localhost:11434", help="Ollama")
ap.add_argument("--cpu", action="store_true", help="Ollama only: num_gpu 0, 4 threads — the CPU latency column")
ap.add_argument("--limit", type=int, default=128)
ap.add_argument("--token", default=os.environ.get("OPENNVR_ADAPTER_TOKEN", ""), help="adapter bearer token")
ap.add_argument("--out", required=True)
args = ap.parse_args()
MODEL, GPU, LIMIT, OUT, ROOT = args.model, (0 if args.cpu else 99), args.limit, args.out, args.data
random.seed(7)
imgs = sorted(glob.glob(f"{ROOT}/images/train2017/*.jpg"))[:LIMIT]

def truth(p):
    lab = p.replace("/images/", "/labels/").rsplit(".", 1)[0] + ".txt"
    ids = [int(l.split()[0]) for l in open(lab)] if os.path.exists(lab) else []
    return ids

def ask_adapter(q, img_bytes):
    """MODEL = adapter:<url>: the shipped OpenNVR adapter's /infer (multipart
    frame + params.question), result.answer. The baseline is the moondream
    adapter the stack actually ships (0.5b int8), not Ollama's moondream."""
    import uuid
    url = MODEL.split(":", 1)[1].rstrip("/") + "/infer"
    boundary = "----vqa" + uuid.uuid4().hex
    params = json.dumps({"question": q})
    crlf = "\r\n"
    head = (f"--{boundary}{crlf}Content-Disposition: form-data; name=\"frame\"; filename=\"f.jpg\"{crlf}"
            f"Content-Type: image/jpeg{crlf}{crlf}").encode()
    tail = (f"{crlf}--{boundary}{crlf}Content-Disposition: form-data; name=\"params\"{crlf}{crlf}"
            f"{params}{crlf}--{boundary}--{crlf}").encode()
    req = urllib.request.Request(url, head + img_bytes + tail,
                                 {"Content-Type": f"multipart/form-data; boundary={boundary}",
                                  "Authorization": f"Bearer {args.token}"})
    t = time.time()
    try:
        r = json.load(urllib.request.urlopen(req, timeout=600)); dt = time.time() - t
        res = r.get("result") or {}
        err = None if r.get("status") != "error" else json.dumps(r.get("error"))[:120]
        return str(res.get("answer") or res.get("caption") or ""), dt, err
    except Exception as e:
        return "", time.time() - t, str(e)[:120]


def ask(q, img_b64):
    if MODEL.startswith("adapter:"):
        return ask_adapter(q, base64.b64decode(img_b64))
    # /api/generate with prompt + images: what the ollamavlm adapter sends
    # (adapters/ollamavlm/service.py). moondream answers /api/chat with "".
    body = {"model": MODEL, "prompt": q, "images": [img_b64], "stream": False, "think": False,
            "keep_alive": -1, "options": {"num_gpu": GPU, "num_thread": 4, "temperature": 0, "num_predict": 256}}
    # 256, not 24: qwen3-vl thinks even with think=false (~60 tokens before
    # "yes"); the time it spends doing so is part of its measured latency.
    req = urllib.request.Request(f"{args.url}/api/generate", json.dumps(body).encode(), {"Content-Type": "application/json"})
    t = time.time()
    try:
        r = json.load(urllib.request.urlopen(req, timeout=600)); dt = time.time() - t
        return r.get("response", ""), dt, None
    except Exception as e:
        return "", time.time() - t, str(e)[:120]

def yn(s):
    s = s.strip().lower()
    if re.match(r"^\W*yes\b", s): return "yes"
    if re.match(r"^\W*no\b", s): return "no"
    return "yes" if "yes" in s[:12] else ("no" if "no" in s[:12] else "?")

def num(s):
    m = re.search(r"\b(\d+)\b", s)
    words = {"zero":0,"none":0,"no":0,"one":1,"two":2,"three":3,"four":4,"five":5,"six":6,"seven":7,"eight":8,"nine":9,"ten":10}
    if m: return int(m.group(1))
    for w,v in words.items():
        if re.search(rf"\b{w}\b", s.lower()): return v
    return None

rows, errors = [], 0
for p in imgs:
    ids = truth(p); present = sorted(set(ids)); 
    if not present: continue
    absent = [c for c in range(80) if c not in present]
    b64 = base64.b64encode(open(p, "rb").read()).decode()
    cls_p = random.choice(present); cls_a = random.choice(absent)
    qs = [("present", f"Is there a {COCO[cls_p]} in this image? Answer yes or no.", "yes"),
          ("absent", f"Is there a {COCO[cls_a]} in this image? Answer yes or no.", "no")]
    n_people = ids.count(0)
    if n_people:
        qs.append(("count", "How many people are in this image? Answer with a number only.", n_people))
    for kind, q, want in qs:
        a, dt, err = ask(q, b64)
        if err: errors += 1
        got = num(a) if kind == "count" else yn(a)
        ok = (got == want) if kind != "count" else (got == want)
        near = (got is not None and abs(got - want) <= 1) if kind == "count" else ok
        rows.append({"img": os.path.basename(p), "kind": kind, "q": q, "want": want, "answer": a[:80], "got": got, "ok": bool(ok), "near": bool(near), "s": round(dt, 2), "err": err})

def summ(kind):
    r = [x for x in rows if x["kind"] == kind]
    return {"n": len(r), "ok": sum(x["ok"] for x in r), "near": sum(x["near"] for x in r), "p50_s": round(statistics.median(x["s"] for x in r), 2) if r else None}
summary = {"model": MODEL, "device": "cpu4" if GPU == 0 else "gpu", "images": len(imgs), "errors": errors, **{k: summ(k) for k in ("present", "absent", "count")}}
json.dump({"summary": summary, "rows": rows}, open(OUT, "w"), indent=1)
print(json.dumps(summary))
