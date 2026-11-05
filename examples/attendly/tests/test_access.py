"""Hostel and mess gates: the policy table, the camera worker, the direct
check route, and the meal bookings the mess gate consumes."""
import io
from datetime import datetime
from zoneinfo import ZoneInfo

import numpy as np

from attendly.services.access import (
    AccessPublisher,
    Decision,
    GateWorker,
    current_meal,
    decide,
    decide_and_record,
    parse_meal_windows,
)
from attendly.storage import gates_repo
from attendly.storage.repo import put_student
from attendly.storage.staff_repo import put_staff
from tests.conftest import jpeg_bytes

TZ = ZoneInfo("Asia/Kolkata")


def at(hhmm: str, day: str = "2026-11-05") -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}:00").replace(tzinfo=TZ)


def _people():
    put_student("AT00001", "Asha Rao", None, "asha@example.com", course_code="MBA-1", section="A",
                residence="hostler", hostel_block="B")
    put_student("AT00002", "Dev Nair", None, "dev@example.com", course_code="MBA-1", section="A")
    put_staff("ST00001", "Warden Iyer", designation="Warden")


def test_meal_windows_parse_and_lookup():
    w = parse_meal_windows("breakfast=07:00-09:30, lunch=12:00-14:30,dinner=19:00-21:30,tea=16:00-17:00,bad")
    assert set(w) == {"breakfast", "lunch", "dinner"}
    assert current_meal(w, at("08:15")) == "breakfast"
    assert current_meal(w, at("09:30")) is None  # end is exclusive
    assert current_meal(w, at("13:00")) == "lunch"
    assert current_meal(w, at("23:00")) is None
    assert parse_meal_windows("") == {}


def test_hostel_gate_policy(env):
    _people()
    gate_b = gates_repo.gate_put("Block B", "hostel", camera_id=7, hostel_block="B")
    gate_any = gates_repo.gate_put("Main hostel", "hostel", camera_id=9)
    gate_staff = gates_repo.gate_put("Block B (warden)", "hostel", camera_id=7, hostel_block="B", allow_staff=True)

    assert decide(gate_b, "AT00001", 0.8).reason == "hostler"
    assert decide(gate_b, "AT00001", 0.8).allow
    assert decide(gate_b, "AT00002", 0.8).reason == "day_scholar"
    assert decide(gate_any, "AT00001", 0.8).allow
    blocked = decide(gates_repo.gate_put("Block C", "hostel", camera_id=7, hostel_block="C"), "AT00001", 0.8)
    assert blocked.reason == "wrong_block" and blocked.unit == "B"
    assert decide(gate_b, None, None).reason == "unknown"
    assert decide(gate_b, "AT09999", 0.9).reason == "unknown"
    assert decide(gate_b, "ST00001", 0.9).reason == "staff_not_allowed"
    d = decide(gate_staff, "ST00001", 0.9)
    assert d.allow and d.reason == "staff" and d.person_kind == "staff"


def test_mess_gate_policy_and_consumption(env):
    _people()
    mess = gates_repo.gate_put("Mess", "mess", camera_id=9)
    lunch = at("13:00")
    assert decide(mess, "AT00001", 0.8, now=at("10:30")).reason == "no_meal_window"
    assert decide(mess, "AT00001", 0.8, now=lunch).reason == "not_booked"
    row, created = gates_repo.meal_booking_put("AT00001", "2026-11-05", "lunch")
    assert created
    _, created_again = gates_repo.meal_booking_put("AT00001", "2026-11-05", "lunch")
    assert not created_again
    d = decide(mess, "AT00001", 0.8, now=lunch)
    assert d.allow and d.reason == "booked" and d.unit == "lunch" and d.booking_id == row["id"]
    # entry consumes the booking: the second pass is refused
    first = decide_and_record(mess, "AT00001", 0.8, now=lunch, publisher=env["publisher"])
    assert first["decision"] == "allow" and first["actuated"] is True
    second = decide_and_record(mess, "AT00001", 0.8, now=lunch, publisher=env["publisher"])
    assert second["decision"] == "deny" and second["reason"] == "already_used"
    assert gates_repo.meal_booking_get("AT00001", "2026-11-05", "lunch")["consumed_gate_id"] == mess["id"]
    assert gates_repo.meal_summary("2026-11-05")["lunch"] == {"booked": 1, "served": 1}
    # dinner is a separate booking; a gate with its own windows uses them
    own = gates_repo.gate_put("Mess (late)", "mess", camera_id=9, meal_windows="dinner=20:00-23:00")
    gates_repo.meal_booking_put("AT00001", "2026-11-05", "dinner")
    assert decide(own, "AT00001", 0.8, now=at("22:00")).allow
    assert decide(mess, "AT00001", 0.8, now=at("22:00")).reason == "no_meal_window"


def test_publisher_payload_is_the_contract():
    d = Decision("allow", "hostler", person_code="AT00001", person_name="Asha Rao", person_kind="student", unit="B",
                 similarity=0.81)
    p = AccessPublisher.payload(d)
    assert p == {"plate_text": "AT00001", "decision": "allow", "reason": "hostler", "owner": "Asha Rao", "unit": "B",
                 "confidence": 0.81}


def test_publisher_without_bus_still_hits_open_url(env):
    calls = []
    pub = AccessPublisher(nats_url="", http_get=lambda url, t: calls.append(url) or True)
    gate = {"id": 1, "camera_id": 7, "open_url": "http://relay.local/open"}
    assert pub.publish(gate, Decision("allow", "hostler", person_code="AT00001")) is True
    assert pub.publish(gate, Decision("deny", "unknown")) is False  # a deny never pulls the trigger
    assert calls == ["http://relay.local/open"]
    assert pub.publish({"id": 2, "camera_id": 7}, Decision("allow", "hostler")) is False  # nowhere to send it


def test_gate_worker_largest_face_decides_and_cooldown(env):
    _people()
    eng, cams, pub = env["engine"], env["cameras"], env["publisher"]
    gate = gates_repo.gate_put("Block B", "hostel", camera_id=7, hostel_block="B")
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    frame[100:300, 100:300] = 200  # big face (Asha)
    frame[20:60, 500:540] = 90     # small face (Dev, further away)
    cams.frames[7] = frame
    eng.default_faces = [[100, 100, 300, 300], [500, 20, 540, 60]]

    import cv2

    from attendly.services.recognize import crop_boxes

    faces = eng.detect_faces(cv2.imencode(".jpg", frame)[1].tobytes())
    crops = {i: cv2.imencode(".jpg", c)[1].tobytes() for i, c in crop_boxes(frame, faces)}
    eng.match[crops[0]] = ("AT00001", 0.82)
    eng.match[crops[1]] = ("AT00002", 0.70)

    w = GateWorker(gate=gate, recognizer=eng, frames=cams, publisher=pub, clock=lambda: at("22:10"), cooldown_seconds=60)
    row = w.tick()
    assert row and row["decision"] == "allow" and row["person_code"] == "AT00001" and row["actuated"]
    assert pub.events[-1][2]["plate_text"] == "AT00001" and pub.events[-1][0]["camera_id"] == 7
    # same person straight away: cooldown, no second row
    assert w.tick() is None
    assert len(gates_repo.access_events_list(gate["id"])) == 1
    # no camera frame: nothing happens
    cams.frames[7] = None
    assert w.tick() is None
    assert w.status()["decisions"] == 1 and w.status()["polls"] == 3


def test_gate_worker_unknown_face_is_logged_once_per_cooldown(env):
    eng, cams, pub = env["engine"], env["cameras"], env["publisher"]
    gate = gates_repo.gate_put("Block B", "hostel", camera_id=7, hostel_block="B")
    frame = np.zeros((240, 320, 3), dtype=np.uint8)
    cams.frames[7] = frame
    eng.default_faces = [[50, 50, 150, 150]]
    w = GateWorker(gate=gate, recognizer=eng, frames=cams, publisher=pub, cooldown_seconds=60)
    row = w.tick()
    assert row["decision"] == "deny" and row["reason"] == "unknown" and row["person_code"] is None
    assert w.tick() is None
    assert pub.events[-1][2]["decision"] == "deny"


def test_gate_engine_follows_the_table(env):
    from attendly.services.access import GateEngine

    engine = GateEngine.get_shared()
    g = gates_repo.gate_put("Block B", "hostel", camera_id=7, hostel_block="B")
    engine.reload()
    assert [s["gate_id"] for s in engine.status()] == [g["id"]]
    gates_repo.gate_update(g["id"], enabled=False)
    engine.reload()
    assert engine.status() == []
    gates_repo.gate_update(g["id"], enabled=True)
    engine.reload()
    assert engine.status()[0]["running"] is True
    engine.shutdown()
    assert engine.status() == []


# ── routes ──────────────────────────────────────────────────────────


def test_gates_api(client, env):
    r = client.post("/gates/", json={"name": "Block B", "kind": "hostel", "camera_id": 7, "hostel_block": "B"})
    assert r.status_code == 200, r.text
    g = r.json()
    assert g["camera_name"] == "Lab 1 camera" and g["kind"] == "hostel" and g["enabled"] is True
    assert client.post("/gates/", json={"name": "x", "kind": "mess", "camera_id": 9, "meal_windows": "nonsense"}).status_code == 400
    r = client.put(f"/gates/{g['id']}", json={"enabled": False, "open_url": "http://relay/open"})
    assert r.json()["enabled"] is False and r.json()["open_url"] == "http://relay/open"
    st = client.get("/gates/status").json()
    assert st["today"] == {"allowed": 0, "denied": 0} and "lunch" in st["meal_windows"] and st["bus"]["configured"] is True
    assert client.get("/gates/").json()[0]["id"] == g["id"]
    assert client.delete(f"/gates/{g['id']}").json() == {"ok": True}
    assert client.get("/gates/").json() == []


def test_gate_check_route(client, env):
    _people()
    eng = env["engine"]
    g = client.post("/gates/", json={"name": "Block B", "kind": "hostel", "camera_id": 7, "hostel_block": "B"}).json()
    photo = jpeg_bytes(seed=5)
    # no face → deny no_face, nothing logged
    r = client.post(f"/gates/{g['id']}/check", files={"image": ("f.jpg", io.BytesIO(photo), "image/jpeg")})
    assert r.json()["reason"] == "no_face"
    assert client.get("/gates/events").json() == []
    # a face the model knows
    eng.default_faces = [[5, 5, 50, 40]]
    import cv2
    import numpy as np

    frame = cv2.imdecode(np.frombuffer(photo, np.uint8), cv2.IMREAD_COLOR)
    from attendly.services.recognize import crop_boxes

    crop = crop_boxes(frame, eng.detect_faces(photo))[0][1]
    eng.match[cv2.imencode(".jpg", crop)[1].tobytes()] = ("AT00001", 0.9)
    r = client.post(f"/gates/{g['id']}/check", files={"image": ("f.jpg", io.BytesIO(photo), "image/jpeg")})
    assert r.status_code == 200, r.text
    assert r.json()["decision"] == "allow" and r.json()["person_name"] == "Asha Rao" and r.json()["source"] == "check"
    ev = client.get("/gates/events", params={"gate_id": g["id"]}).json()
    assert len(ev) == 1 and ev[0]["actuated"] is True
    assert client.get("/gates/status").json()["today"] == {"allowed": 1, "denied": 0}


def test_meals_api(client, env):
    _people()
    r = client.post("/meals/bookings", json={"email": "asha@example.com", "date": "2026-11-06", "meals": ["lunch", "dinner", "lunch"]})
    assert r.status_code == 200, r.text
    assert r.json()["created"] == 2 and r.json()["bookings"][0]["student"]["full_name"] == "Asha Rao"
    r = client.post("/meals/bookings", json={"student_code": "AT00001", "date": "2026-11-06", "meals": ["lunch"]})
    assert r.json()["created"] == 0
    assert client.post("/meals/bookings", json={"student_code": "AT00001", "date": "2026-11-06", "meals": ["brunch"]}).status_code == 400
    assert client.post("/meals/bookings", json={"student_code": "NOPE", "date": "2026-11-06", "meals": ["lunch"]}).status_code == 404
    day = client.get("/meals/bookings", params={"date": "2026-11-06"}).json()
    assert [b["meal"] for b in day["bookings"]] == ["dinner", "lunch"]
    assert client.get("/meals/bookings", params={"date": "2026-11-06", "meal": "lunch"}).json()["bookings"][0]["meal"] == "lunch"
    csv_text = "student_code,email,meals\nAT00001,,breakfast|lunch\n,dev@example.com,dinner\nAT00009,,lunch\nAT00002,,tea\n"
    r = client.post("/meals/bookings/import", params={"date": "2026-11-07"},
                    files={"file": ("b.csv", io.BytesIO(csv_text.encode()), "text/csv")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["created"] == 3 and body["already_booked"] == 0 and len(body["errors"]) == 2
    assert client.get("/meals/summary", params={"date": "2026-11-07"}).json()["meals"]["lunch"] == {"booked": 1, "served": 0}
    bid = day["bookings"][0]["id"]
    assert client.delete(f"/meals/bookings/{bid}").json() == {"ok": True}
    assert client.delete(f"/meals/bookings/{bid}").status_code == 404
    assert client.get("/meals/kinds").json() == ["breakfast", "lunch", "dinner"]


def test_students_carry_residence(client, env):
    r = client.post("/students/", json={"full_name": "Asha Rao", "course_code": "MBA-1", "section": "A",
                                        "email": "asha@example.com", "residence": "Hostler", "hostel_block": "B"})
    assert r.status_code == 200, r.text
    assert r.json()["residence"] == "hostler" and r.json()["hostel_block"] == "B"
    code = r.json()["student_code"]
    r = client.put(f"/students/{code}", json={"residence": "day_scholar"})
    assert r.json()["residence"] == "day_scholar" and r.json()["hostel_block"] is None
    r = client.post("/students/register_new", data={"full_name": "H", "course_code": "MBA-1", "section": "A",
                                                     "email": "h@example.com", "residence": "hostler"},
                    files=[("images", ("a.jpg", io.BytesIO(jpeg_bytes(seed=2)), "image/jpeg"))])
    assert r.status_code == 400 and "hostel block" in r.json()["detail"]
    assert client.get("/gates/status").json()["hostel_blocks"] == []
