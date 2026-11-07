"""The FaceEngine against a mocked KAI-C and adapter (respx)."""
import httpx
import pytest
import respx

from attendly.face.engine import FaceEngine, FaceRejected, FaceServiceError, normalised_bbox, split_face_id
from tests.conftest import jpeg_bytes

KAIC = "http://kaic.test"
FACES = "http://faces.test"


@pytest.fixture
def engine():
    return FaceEngine(kaic_url=KAIC, kaic_api_key="site-key", adapter_name="insightface", faces_url=FACES,
                      faces_token="site-key", category="attendly_student", timeout_seconds=2.0)


def test_normalised_bbox_and_face_ids():
    box = normalised_bbox([10, 20, 30, 60], 100, 200)
    assert box == {"Left": 0.1, "Top": 0.1, "Width": 0.2, "Height": 0.2}
    assert split_face_id("AT00001#3") == ("AT00001", 3)
    assert split_face_id("AT00001") == ("AT00001", None)


@respx.mock
def test_detect_faces_normalises_against_the_image_size(engine):
    img = jpeg_bytes(100, 50)
    route = respx.post(f"{KAIC}/api/v1/infer/insightface").mock(return_value=httpx.Response(200, json={
        "result": {"task": "face_detection", "faces": [{"bbox": [10, 5, 60, 45], "confidence": 0.97}], "face_count": 1}}))
    faces = engine.detect_faces(img)
    assert route.called
    body = route.calls[0].request
    assert body.headers["X-Internal-Api-Key"] == "site-key"
    assert faces[0]["bbox_px"] == [10, 5, 60, 45]
    assert faces[0]["BoundingBox"] == {"Left": 0.1, "Top": 0.1, "Width": 0.5, "Height": 0.8}
    assert faces[0]["Confidence"] == pytest.approx(97.0)


@respx.mock
def test_search_faces_recognised_and_not(engine):
    img = jpeg_bytes()
    route = respx.post(f"{KAIC}/api/v1/infer/insightface")
    route.mock(return_value=httpx.Response(200, json={"result": {"recognized": True, "person_id": "AT00001", "name": "Asha",
                                                                 "similarity": 0.81, "threshold": 0.5}}))
    assert engine.search_faces(img, threshold=0.5) == [("AT00001", 0.81)]
    import json

    sent = json.loads(route.calls[0].request.content)
    assert sent["task"] == "face_recognition" and sent["threshold"] == 0.5 and "frame_b64" in sent

    route.mock(return_value=httpx.Response(200, json={"result": {"recognized": False, "similarity": 0.2}}))
    assert engine.search_faces(img) == []

    route.mock(return_value=httpx.Response(503, text="adapter not ready"))
    with pytest.raises(FaceServiceError):
        engine.search_faces(img)


@respx.mock
def test_index_face_appends_with_name_and_category(engine):
    route = respx.post(f"{FACES}/faces/register").mock(return_value=httpx.Response(200, json={
        "ok": True, "face": {"person_id": "AT00001", "name": "Asha", "category": "attendly_student", "samples": 3}}))
    fid = engine.index_face(jpeg_bytes(), "AT00001", name="Asha")
    assert fid == "AT00001#2"
    req = route.calls[0].request
    assert req.headers["Authorization"] == "Bearer site-key"
    body = req.content
    assert b'name="person_id"\r\n\r\nAT00001' in body
    assert b'name="append"\r\n\r\ntrue' in body
    assert b"attendly_student" in body and b'filename="face.jpg"' in body


@respx.mock
def test_index_face_rejections_and_outages(engine):
    respx.post(f"{FACES}/faces/register").mock(return_value=httpx.Response(422, json={"detail": "image contains 2 faces"}))
    with pytest.raises(FaceRejected, match="2 faces"):
        engine.index_face(jpeg_bytes(), "AT00001", name="Asha")
    respx.post(f"{FACES}/faces/register").mock(return_value=httpx.Response(500, text="boom"))
    with pytest.raises(FaceServiceError):
        engine.index_face(jpeg_bytes(), "AT00001", name="Asha")
    respx.post(f"{FACES}/faces/register").mock(side_effect=httpx.ConnectError("down"))
    with pytest.raises(FaceServiceError, match="unreachable"):
        engine.index_face(jpeg_bytes(), "AT00001", name="Asha")


@respx.mock
def test_list_describe_and_get_by_external_id(engine):
    respx.get(f"{FACES}/faces").mock(return_value=httpx.Response(200, json={"faces": [
        {"person_id": "AT00001", "name": "Asha", "category": "attendly_student", "samples": 2},
        {"person_id": "AT00002", "name": "Bilal", "category": "attendly_student", "samples": 1}], "count": 2}))
    faces = engine.list_faces()
    assert [f["FaceId"] for f in faces] == ["AT00001#0", "AT00001#1", "AT00002#0"]
    assert engine.describe_collection()["FaceCount"] == 3
    assert engine.describe_collection()["PersonCount"] == 2
    respx.get(f"{FACES}/faces/AT00001").mock(return_value=httpx.Response(200, json={"face": {
        "person_id": "AT00001", "name": "Asha", "category": "attendly_student", "samples": 2}}))
    assert len(engine.get_faces_by_external_id("AT00001")) == 2
    # a doorbell household member is not one of ours
    respx.get(f"{FACES}/faces/dad").mock(return_value=httpx.Response(200, json={"face": {
        "person_id": "dad", "name": "Dad", "category": "family", "samples": 4}}))
    assert engine.get_faces_by_external_id("dad") == []
    respx.get(f"{FACES}/faces/nobody").mock(return_value=httpx.Response(404, json={"detail": "no"}))
    assert engine.person("nobody") is None


@respx.mock
def test_delete_faces_groups_samples_and_whole_persons(engine):
    respx.get(f"{FACES}/faces/AT00001").mock(return_value=httpx.Response(200, json={"face": {"person_id": "AT00001", "samples": 3}}))
    respx.get(f"{FACES}/faces/AT00002").mock(return_value=httpx.Response(200, json={"face": {"person_id": "AT00002", "samples": 2}}))
    sample_del = respx.delete(url__regex=rf"{FACES}/faces/AT00001/samples/\d+").mock(return_value=httpx.Response(200, json={"ok": True}))
    person_del = respx.delete(f"{FACES}/faces/AT00002").mock(return_value=httpx.Response(200, json={"ok": True}))
    out = engine.delete_faces(["AT00001#0", "AT00001#2", "AT00002#0", "AT00002#1"])
    # samples removed highest index first so indexes do not shift underneath
    assert [c.request.url.path for c in sample_del.calls] == ["/faces/AT00001/samples/2", "/faces/AT00001/samples/0"]
    assert person_del.called
    assert {d["FaceId"] for d in out["DeletedFaces"]} == {"AT00001#0", "AT00001#2", "AT00002#0", "AT00002#1"}


# ── face_identify (ai-adapter#81): one call per frame, with a fallback ──


def _identify_result(*faces):
    return {"task": "face_identify", "faces": list(faces), "recognized_count": sum(1 for f in faces if f.get("recognized"))}


def test_identify_path_recognises_every_face_and_honours_the_margin(env):
    from attendly.services.recognize import recognize_codes_in_frame, recognize_faces_in_frame
    from attendly.storage.repo import put_student
    import numpy as np

    put_student("AT00001", "Asha", None, None, course_code="MBA-1", section="A")
    put_student("AT00002", "Bilal", None, None, course_code="MBA-1", section="A")
    put_student("AT00003", "Chen", None, None, course_code="ECO-2", section="B")
    eng = env["engine"]
    eng.identify = _identify_result(
        {"bbox": [10, 10, 110, 110], "recognized": True, "person_id": "AT00001", "similarity": 0.81, "second_similarity": 0.40},
        {"bbox": [200, 10, 260, 70], "recognized": True, "person_id": "AT00002", "similarity": 0.62, "second_similarity": 0.60},
        {"bbox": [300, 10, 340, 50], "recognized": True, "person_id": "AT00003", "similarity": 0.70, "second_similarity": 0.1},
        {"bbox": [400, 10, 440, 50], "recognized": False, "person_id": None, "similarity": 0.3, "second_similarity": 0.2},
    )
    frame = np.zeros((200, 500, 3), dtype=np.uint8)
    faces = recognize_faces_in_frame(eng, frame)
    assert [f["code"] for f in faces] == ["AT00001", None, "AT00003", None]
    assert faces[1]["ambiguous"] is True and faces[1]["similarity"] == 0.62  # 0.02 lead < margin
    assert "detect" not in eng.calls and eng.calls.count("identify") == 1
    # the class filter still applies, the largest face wins at a gate
    assert recognize_codes_in_frame(eng, frame, course_code="MBA-1", section="A") == ["AT00001"]
    assert recognize_faces_in_frame(eng, frame, largest_only=True)[0]["code"] == "AT00001"


def test_identify_absent_falls_back_to_detect_then_crop(env):
    from attendly.services.recognize import recognize_codes_in_frame
    from attendly.storage.repo import put_student
    import cv2
    import numpy as np

    put_student("AT00001", "Asha", None, None, course_code="MBA-1", section="A")
    eng = env["engine"]
    eng.identify = None  # the adapter has no such task
    frame = np.full((200, 300, 3), 100, dtype=np.uint8)
    eng.faces_in[cv2.imencode(".jpg", frame)[1].tobytes()] = [[10, 10, 60, 60]]
    eng.search_faces = lambda *a, **k: [("AT00001", 0.9)]
    assert recognize_codes_in_frame(eng, frame) == ["AT00001"]
    assert eng.identify_calls and "detect" in eng.calls


@respx.mock
def test_real_engine_marks_identify_unsupported_on_a_4xx_and_retries_later(monkeypatch):
    from attendly.face.engine import FaceEngine, FaceServiceError

    eng = FaceEngine(kaic_url="http://kaic.test", kaic_api_key="k", adapter_name="insightface",
                     faces_url="http://faces.test", faces_token="t", category="attendly_student")
    route = respx.post("http://kaic.test/api/v1/infer/insightface").mock(
        return_value=httpx.Response(400, json={"detail": "unknown task 'face_identify'"}))
    assert eng.identify_faces(b"jpeg") is None and eng.identify_supported is False
    assert eng.identify_faces(b"jpeg") is None and route.call_count == 1  # remembered, not re-asked
    eng._identify_checked_at -= FaceEngine.IDENTIFY_RECHECK_SECONDS + 1
    route.mock(return_value=httpx.Response(200, json={"result": {"task": "face_identify", "faces": [
        {"bbox": [1, 2, 30, 40], "recognized": True, "person_id": "AT00001", "similarity": 0.9, "second_similarity": 0.3}]}}))
    res = eng.identify_faces(b"jpeg", threshold=0.6)
    assert res["faces"][0]["person_id"] == "AT00001" and eng.identify_supported is True
    import json

    body = json.loads(route.calls[-1].request.content)
    assert body["task"] == "face_identify" and body["threshold"] == 0.6 and body["min_face_px"] == 20
    route.mock(return_value=httpx.Response(503, text="adapter busy"))
    with pytest.raises(FaceServiceError):
        eng.identify_faces(b"jpeg")
    assert eng.identify_supported is True  # an outage is not "unsupported"
