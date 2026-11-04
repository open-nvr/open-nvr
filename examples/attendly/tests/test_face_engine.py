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
