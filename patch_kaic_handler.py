#!/usr/bin/env python3
"""
Persistent fix for the stale KAI-C /infer/local handler on the box.

Surgically replaces ONLY `process_local_inference` in kai-c/main.py with the
version that: resolves the frame URI -> frame_b64, attaches the bearer token,
and translates the adapter's DetectionResult to the flat shape the backend
stores. Leaves every other line of the file untouched.

Usage:
    python3 patch_kaic_handler.py [/usr/src/open-nvr/kai-c/main.py]

Safe to run more than once (idempotent): if the handler is already patched it
exits without changing anything. Writes a .bak the first time it edits.
"""
import ast
import sys

PATH = sys.argv[1] if len(sys.argv) > 1 else "/usr/src/open-nvr/kai-c/main.py"

START = "async def process_local_inference(request: dict):"
END = '@app.post("/infer/cloud"'

NEW_FUNC = '''async def process_local_inference(request: dict):
    """
    Resolve the backend frame URI -> bytes, send to the SDK adapter as
    frame_b64 WITH the bearer token, and translate the adapter response
    to the flat shape the backend stores.
    Flow: OpenNVR Backend -> KAI-C -> AI Adapter -> KAI-C -> OpenNVR Backend
    handler-rev: 2 (task-class filter)
    """
    # The SDK adapter (YOLOv8n) is a generic 80-class COCO detector, so a
    # "person_detection" model otherwise stores whatever box scores highest
    # in the frame (chair/dog/cat false positives). Keep only the classes the
    # task actually asks for; unknown tasks are left unfiltered.
    TASK_CLASSES = {
        "person_detection": {"person"},
        "vehicle_detection": {"car", "truck", "bus", "motorcycle", "bicycle"},
        "animal_detection": {"dog", "cat", "bird", "horse", "cow", "sheep", "bear"},
    }
    try:
        import base64
        adapter_url = get_adapter_url()

        inp = (request or {}).get("input") or {}
        frame = inp.get("frame") or {}
        uri = frame.get("uri") or ""
        params = dict(inp.get("params") or {})

        frames_dir = os.getenv("FRAMES_DIR", "/app/AI-adapters/AIAdapters/frames")
        rel = uri
        if rel.startswith("opennvr://frames/"):
            rel = rel[len("opennvr://frames/"):]
        elif rel.startswith("kavach://frames/"):
            rel = rel[len("kavach://frames/"):]
        else:
            rel = rel.replace("opennvr://", "").replace("kavach://", "")
        frame_path = os.path.join(frames_dir, rel)

        if not os.path.isfile(frame_path):
            raise HTTPException(status_code=400, detail=f"frame not found for inference: {frame_path}")
        with open(frame_path, "rb") as fh:
            frame_b64 = base64.b64encode(fh.read()).decode("ascii")

        if "confidence" in params and "confidence_threshold" not in params:
            params["confidence_threshold"] = params.pop("confidence")
        adapter_body = {"frame_b64": frame_b64, **params}

        headers = {"Content-Type": "application/json"}
        if INTERNAL_API_KEY:
            headers["Authorization"] = f"Bearer {INTERNAL_API_KEY}"

        response = requests.post(f"{adapter_url}/infer", json=adapter_body, headers=headers, timeout=60)
        response.raise_for_status()
        adapter_json = response.json()

        det = adapter_json.get("result") or {}
        detections = det.get("detections") or []

        # Filter to the classes this task wants (person_detection -> person).
        wanted = TASK_CLASSES.get((request or {}).get("task"))
        if wanted is not None:
            detections = [d for d in detections if d.get("label") in wanted]

        if detections:
            top = max(detections, key=lambda d: d.get("confidence") or 0.0)
            bb = top.get("bbox") or {}
            flat = {
                "label": top.get("label"),
                "confidence": top.get("confidence"),
                "bbox": [bb.get("x"), bb.get("y"), bb.get("w"), bb.get("h")],
                "count": len(detections),
                "latency_ms": adapter_json.get("inference_ms"),
            }
        else:
            flat = {"confidence": 0.0, "latency_ms": adapter_json.get("inference_ms")}

        return {"status": "success", "response": flat}

    except HTTPException:
        raise
    except requests.HTTPError as e:
        raise HTTPException(status_code=e.response.status_code if e.response else 500, detail=f"AI Adapter error: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"KAI-C processing error: {str(e)}")


'''


def main():
    src = open(PATH, encoding="utf-8").read()

    if START not in src:
        print("ERROR: could not find process_local_inference in", PATH)
        sys.exit(1)

    i = src.index(START)
    j = src.index(END, i)
    old_span = src[i:j]

    if "handler-rev: 2" in old_span:
        print("Already at handler-rev 2 -- no change:", PATH)
        sys.exit(0)

    patched = src[:i] + NEW_FUNC + src[j:]
    ast.parse(patched)  # abort if the result is not valid Python

    open(PATH + ".bak", "w", encoding="utf-8").write(src)
    open(PATH, "w", encoding="utf-8").write(patched)
    print("PATCHED OK:", PATH)
    print("backup    :", PATH + ".bak")


if __name__ == "__main__":
    main()
