# Copyright (c) 2026 OpenNVR
# Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0)
"""Update results and new versions as host-level alerts (services/update_notifier.py).

Each finished run is told once (updated / rolled back / failed, with fitting
severity), a newly found version once, nothing from before the notifier
first ran, and nothing twice across the restart every update causes.
"""

from __future__ import annotations

import json

from services.update_notifier import new_notices

H1 = "2026-10-20T01:00:00Z 0.1.7 0.1.8 succeeded"
H2 = "2026-10-29T01:00:00Z 0.1.8 0.1.9 rolled_back"
H3 = "2026-10-30T01:00:00Z 0.1.8 0.1.9 failed"


def test_first_run_marks_history_without_telling():
    notices, seen = new_notices([H1, H2], "0.2.0", None)
    assert notices == [] and seen == {"last_run": "2026-10-29T01:00:00Z", "available": "0.2.0"}


def test_new_runs_are_told_once_with_their_severity():
    _, seen = new_notices([H1], None, None)
    notices, seen = new_notices([H1, H2, H3], None, seen)
    assert [(n.state, n.severity) for n in notices] == [("rolled_back", "warning"), ("failed", "critical")]
    assert "0.1.8 runs again" in notices[0].description and notices[1].data["to"] == "0.1.9"
    again, _ = new_notices([H1, H2, H3], None, seen)
    assert again == []


def test_a_found_version_is_told_once():
    _, seen = new_notices([], None, None)
    notices, seen = new_notices([], "0.2.0", seen)
    assert [(n.state, n.description) for n in notices] == [("available", "OpenNVR 0.2.0 is available.")]
    assert new_notices([], "0.2.0", seen)[0] == []
    assert [n.data["version"] for n in new_notices([], "0.2.1", seen)[0]] == ["0.2.1"]


def test_a_tick_records_system_events(tmp_path, monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import models
    from services import update_notifier, update_status

    eng = create_engine(f"sqlite:///{tmp_path / 'n.db'}")
    models.Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    monkeypatch.setattr(update_status, "STATE_DIR", tmp_path)
    monkeypatch.setattr(update_status, "REQUEST_DIR", tmp_path / "req")
    (tmp_path / "history.log").write_text(H1 + "\n")
    assert update_notifier.tick(db, "0.1.8", None) == []           # first pass: marks only
    (tmp_path / "history.log").write_text(H1 + "\n" + H2 + "\n")
    rows = update_notifier.tick(db, "0.1.8", None)
    assert [(r["event_type"], r["event_state"], r["severity"]) for r in rows] == [
        ("opennvr_update", "rolled_back", "warning")]
    stored = db.query(models.SystemEvent).filter_by(event_type="opennvr_update").all()
    assert len(stored) == 1 and json.loads(stored[0].data)["to"] == "0.1.9"
    assert update_notifier.tick(db, "0.1.8", None) == []           # told once
    db.close()
