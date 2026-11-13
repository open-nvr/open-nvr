#!/usr/bin/env python3
# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Embedding eval: text-to-image retrieval on coco128 for an OpenCLIP checkpoint.

Queries are "a photo of a <class>" for every COCO class in at least three
of the 128 frames; a frame is relevant when its labels hold the class.
mAP and recall@5 over those classes, CPU (4 threads) milliseconds per
image embed and per text embed, the vector width and the parameter
count. What the `clip` adapter serves is an OpenCLIP checkpoint, so any
candidate can be measured before it is baked.

    python tools/retrieval_eval.py --arch ViT-B-32 --pretrained laion2b_s34b_b79k --data datasets/coco128 --out clip.json
    python tools/retrieval_eval.py --arch ViT-B-16-SigLIP2-256 --pretrained webli --data datasets/coco128 --out siglip2.json

Needs open_clip_torch, torch, pillow (and transformers for SigLIP 2's
tokenizer). Results are in MODELS_AND_LATENCY.md, "Measured: embeddings".
"""
import glob, json, os, sys, time, statistics
import argparse
ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
ap.add_argument("--arch", required=True, help="an OpenCLIP architecture, e.g. ViT-B-16-SigLIP2-256")
ap.add_argument("--pretrained", required=True, help="its pretrained tag, e.g. webli")
ap.add_argument("--data", required=True, help="a coco128 directory (images/train2017, labels/train2017)")
ap.add_argument("--out", required=True)
args = ap.parse_args()
ARCH, PRE, OUT, ROOT = args.arch, args.pretrained, args.out, args.data

import numpy as np  # noqa: E402
import torch  # noqa: E402
import open_clip  # noqa: E402
from PIL import Image  # noqa: E402
torch.set_num_threads(4)
COCO = ["person","bicycle","car","motorcycle","airplane","bus","train","truck","boat","traffic light","fire hydrant","stop sign","parking meter","bench","bird","cat","dog","horse","sheep","cow","elephant","bear","zebra","giraffe","backpack","umbrella","handbag","tie","suitcase","frisbee","skis","snowboard","sports ball","kite","baseball bat","baseball glove","skateboard","surfboard","tennis racket","bottle","wine glass","cup","fork","knife","spoon","bowl","banana","apple","sandwich","orange","broccoli","carrot","hot dog","pizza","donut","cake","chair","couch","potted plant","bed","dining table","toilet","tv","laptop","mouse","remote","keyboard","cell phone","microwave","oven","toaster","sink","refrigerator","book","clock","vase","scissors","teddy bear","hair drier","toothbrush"]
imgs = sorted(glob.glob(f"{ROOT}/images/train2017/*.jpg"))
def labels(p):
    lab = p.replace("/images/", "/labels/").rsplit(".", 1)[0] + ".txt"
    return set(int(l.split()[0]) for l in open(lab) if l.strip()) if os.path.exists(lab) else set()
L = [labels(p) for p in imgs]
model, _, preprocess = open_clip.create_model_and_transforms(ARCH, pretrained=PRE, device="cpu")
model.eval(); tok = open_clip.get_tokenizer(ARCH)
t_img = []
feats = []
with torch.no_grad():
    for p in imgs:
        x = preprocess(Image.open(p).convert("RGB")).unsqueeze(0)
        t0 = time.perf_counter(); f = model.encode_image(x); t_img.append(time.perf_counter() - t0)
        feats.append(torch.nn.functional.normalize(f, dim=-1)[0].numpy())
    F = np.stack(feats)
    classes = [c for c in range(80) if sum(c in s for s in L) >= 3]
    t_txt = []; aps = []; r5 = []
    for c in classes:
        t0 = time.perf_counter(); q = model.encode_text(tok([f"a photo of a {COCO[c]}"])); t_txt.append(time.perf_counter() - t0)
        q = torch.nn.functional.normalize(q, dim=-1)[0].numpy()
        order = np.argsort(-(F @ q)); rel = np.array([c in L[i] for i in order])
        hits = np.cumsum(rel); prec = hits / (np.arange(len(rel)) + 1)
        aps.append(float((prec * rel).sum() / rel.sum())); r5.append(float(rel[:5].any()))
n_params = sum(p.numel() for p in model.parameters()) / 1e6
out = {"arch": ARCH, "pretrained": PRE, "dim": int(F.shape[1]), "params_M": round(n_params, 1), "classes": len(classes),
       "mAP": round(float(np.mean(aps)), 3), "R@5": round(float(np.mean(r5)), 3),
       "img_ms_p50_cpu4": round(statistics.median(t_img) * 1000, 1), "txt_ms_p50_cpu4": round(statistics.median(t_txt) * 1000, 1)}
json.dump(out, open(OUT, "w")); print(json.dumps(out))
