# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: AGPL-3.0-or-later
"""tools/look_eval.py: the labels, the scoring and the loop, with a
scripted model and generated pictures (a site's own stay out of the repo)."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import pathlib
import sys

import pytest
from opennvr_app_sdk.agent import ChatResult, LLMUnreachableError

_TOOLS = pathlib.Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(_TOOLS))
_spec = importlib.util.spec_from_file_location("look_eval", _TOOLS / "look_eval.py")
le = importlib.util.module_from_spec(_spec)
sys.modules["look_eval"] = le
_spec.loader.exec_module(le)

TEMPLATES = le.load_templates(_TOOLS / "look_templates.yml")
# The smallest valid PNG: the model is scripted, so the pixels never matter.
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c6360000002000100e221bc330000000049454e44ae426082")


def write_cases(tmp_path, rows: str, files=("a.png", "b.png", "c.png")) -> pathlib.Path:
    for f in files:
        (tmp_path / f).write_bytes(PNG)
    p = tmp_path / "cases.csv"
    p.write_text("file,template,expect,inputs,note\n" + rows)
    return p


class Script:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    async def chat(self, *, messages, tools=None, format=None, temperature=0.2, max_tokens=256):
        self.calls.append({"messages": messages, "format": format})
        if max_tokens == 1:                       # the warm-up
            return ChatResult(content="ok")
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return ChatResult(content=json.dumps(a))


def gate(decision, company="none"):
    return {"reason": "seen", "company": company, "decision": decision, "confidence": 0.9}


def yard(who):
    return {"reason": "seen", "who": who, "confidence": 0.9}


# ── the templates and the labels ───────────────────────────────────────


def test_the_shipped_templates_are_valid_look_steps():
    assert set(TEMPLATES) == {"gate_who", "yard_threat"}
    assert all(t.mode == "look" and t.input == "picture" for t in TEMPLATES.values())
    assert TEMPLATES["yard_threat"].spec.outcome({"who": "stranger"}) == "alert"


def test_the_example_csv_parses_against_the_templates(tmp_path):
    rows = (_TOOLS / "look_cases.example.csv").read_text().splitlines()[1:]
    for line in rows:
        f = tmp_path / line.split(",")[0]
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(PNG)
    (tmp_path / "c.csv").write_text((_TOOLS / "look_cases.example.csv").read_text())
    cases = le.load_cases(tmp_path / "c.csv", TEMPLATES)
    assert len(cases) == len(rows)
    swiggy = cases[0]
    assert (swiggy["decision"], swiggy["fields"]) == ("delivery", {"company": "swiggy"})
    assert cases[1]["inputs"] == {"plate": "KA01AB1234"}


def test_a_fact_label_gets_its_decision_from_the_step_rules(tmp_path):
    p = write_cases(tmp_path, "a.png,yard_threat,who=guard,,\nb.png,yard_threat,who=Stranger,,\n")
    guard, stranger = le.load_cases(p, TEMPLATES)
    assert (guard["decision"], guard["fields"]) == ("ignore", {"who": "guard"})
    assert (stranger["decision"], stranger["fields"]) == ("alert", {"who": "stranger"})


@pytest.mark.parametrize("row, says", [
    ("a.png,gate_who,parcel,,", "decision 'parcel' is not one of"),
    ("a.png,gate_who,delivery; colour=red,,", "'colour' is not a field of gate_who"),
    ("a.png,gate_who,company=fedex,,", "company='fedex' is not one of"),
    ("a.png,porch,delivery,,", "template 'porch' is not one of"),
    ("missing.png,gate_who,cab,,", "no image at"),
    ("a.png,gate_who,,,", "expect names nothing to check"),
    ("a.png,gate_who,cab; cab,,", "'decision' given twice"),
])
def test_a_bad_label_is_refused_with_its_line(tmp_path, row, says):
    p = write_cases(tmp_path, "b.png,gate_who,cab,,\n" + row + "\n")
    with pytest.raises(le.CaseError) as err:
        le.load_cases(p, TEMPLATES)
    assert "cases.csv:3: " in str(err.value) and says in str(err.value)


def test_every_bad_row_is_reported_at_once(tmp_path):
    p = write_cases(tmp_path, "a.png,gate_who,parcel,,\nb.png,porch,cab,,\na.png,gate_who,cab,,\n"
                              "a.png,gate_who,cab,,\n")
    with pytest.raises(le.CaseError) as err:
        le.load_cases(p, TEMPLATES)
    assert [ln.split(": ")[0] for ln in str(err.value).splitlines()] == [
        "cases.csv:2", "cases.csv:3", "cases.csv:5"]
    assert "listed twice" in str(err.value)


def test_a_csv_without_the_columns_is_refused(tmp_path):
    p = tmp_path / "c.csv"
    p.write_text("image,answer\na.png,cab\n")
    with pytest.raises(le.CaseError, match="missing column"):
        le.load_cases(p, TEMPLATES)


def test_images_can_live_apart_from_the_labels(tmp_path):
    (tmp_path / "pics").mkdir()
    (tmp_path / "pics" / "a.png").write_bytes(PNG)
    p = tmp_path / "cases.csv"
    p.write_text("file,template,expect\na.png,gate_who,cab\n")
    (case,) = le.load_cases(p, TEMPLATES, images=tmp_path / "pics")
    assert case["image"] == str(tmp_path / "pics" / "a.png")


# ── running and scoring ────────────────────────────────────────────────


def test_a_run_through_run_ai_step_scores_decisions_and_fields(tmp_path):
    p = write_cases(tmp_path, "a.png,gate_who,delivery; company=swiggy,plate=KA01,\n"
                              "b.png,gate_who,cab,,\nc.png,yard_threat,who=stranger,,\n")
    cases = le.load_cases(p, TEMPLATES)
    llm = Script(gate("delivery", "zomato"),       # right decision, wrong company
                 gate("cab"), yard("stranger"))
    runs = asyncio.run(le.run(llm, TEMPLATES, cases, repeat=1, hardware="cpu"))
    assert [r["pass"] for r in runs] == [False, True, True]
    assert runs[0]["wrong_fields"] == ["company"]
    assert (runs[2]["decision"], runs[2]["rule"]) == ("alert", 1)
    # The picture and the plate reached the model; the plate as data.
    user = llm.calls[1]["messages"][1]
    assert len(user["images"]) == 1 and "KA01" in user["content"]


def test_unsure_and_a_model_that_is_down_fail_and_are_counted(tmp_path):
    p = write_cases(tmp_path, "a.png,gate_who,cab,,\nb.png,gate_who,unsure,,\n")
    cases = le.load_cases(p, TEMPLATES)
    llm = Script(LLMUnreachableError("down"), gate("unsure"))
    runs = asyncio.run(le.run(llm, TEMPLATES, cases, repeat=1, hardware="cpu"))
    assert [(r["decision"], r["unsure_reason"], r["pass"]) for r in runs] == [
        ("unsure", "llm_down", False), ("unsure", None, True)]
    s = le.summarise(runs, hardware="cpu", gate=0.8)
    assert (s["n"], s["pass"], s["unsure"]) == (2, 1, 2)


def test_output_that_is_no_answer_is_kept_for_reading(tmp_path):
    p = write_cases(tmp_path, "a.png,gate_who,cab,,\n")
    llm = Script({"reason": "a taxi", "decision": "taxi", "company": "none", "confidence": 1})
    (r,) = asyncio.run(le.run(llm, TEMPLATES, le.load_cases(p, TEMPLATES), repeat=1,
                              hardware="cpu"))
    assert (r["decision"], r["unsure_reason"]) == ("unsure", "invalid_output")
    assert '"taxi"' in r["raw"]


def test_the_gpu_draws_three_times_as_a_workflow_does(tmp_path):
    p = write_cases(tmp_path, "a.png,gate_who,cab,,\n")
    llm = Script(gate("cab"), gate("cab"), gate("visitor"))
    (r,) = asyncio.run(le.run(llm, TEMPLATES, le.load_cases(p, TEMPLATES), repeat=1,
                              hardware="gpu"))
    assert len(llm.calls) == 4 and r["decision"] == "unsure"     # 2/3 agree, under 0.75


def test_the_summary_gates_on_the_lower_bound_per_template():
    def run(t, ok, s=5.0):
        return {"template": t, "pass": ok, "decision": "cab" if ok else "visitor",
                "expected": "cab", "seconds": s, "error": None}
    runs = [run("gate_who", True)] * 40 + [run("yard_threat", True)] * 16 + \
        [run("yard_threat", False)] * 4
    s = le.summarise(runs, hardware="cpu", gate=0.8)
    assert s["templates"]["gate_who"]["qualifies"] is True          # 40/40, lower bound 0.91
    assert s["templates"]["yard_threat"]["qualifies"] is False      # 16/20, lower bound 0.58
    assert s["templates"]["yard_threat"]["confusion"] == {"cab": {"cab": 16, "visitor": 4}}

    slow = le.summarise([run("gate_who", True, 130.0)] * 40, hardware="cpu", gate=0.8)
    assert slow["qualifies"] is False                                # over the CPU's 120 s
    report = le.format_report(s, {"model": "m", "hardware": "cpu", "repeat": 1})
    assert "gate_who" in report and "QUALIFIES" in report and "16/20" in report


def test_the_confusion_table_shows_a_wrong_fact_behind_a_right_outcome(tmp_path):
    p = write_cases(tmp_path, "a.png,yard_threat,who=animal,,\n")
    runs = asyncio.run(le.run(Script(yard("nobody")), TEMPLATES, le.load_cases(p, TEMPLATES),
                              repeat=1, hardware="cpu"))
    assert runs[0]["decision"] == "ignore" and runs[0]["pass"] is False
    confusion = le.summarise(runs, hardware="cpu", gate=0.8)["confusion"]
    assert confusion == {"ignore who=animal": {"ignore who=nobody": 1}}


def test_rescore_uses_the_current_labels(tmp_path):
    p = write_cases(tmp_path, "a.png,gate_who,cab,,\n")
    cases = le.load_cases(p, TEMPLATES)
    runs = asyncio.run(le.run(Script(gate("visitor")), TEMPLATES, cases, repeat=1,
                              hardware="cpu"))
    assert runs[0]["pass"] is False
    p.write_text("file,template,expect\na.png,gate_who,visitor\n")   # the label was wrong
    (again,) = le.rescore({"runs": runs}, le.load_cases(p, TEMPLATES))
    assert again["pass"] is True


def test_check_and_bad_cases_need_no_model(tmp_path, capsys):
    p = write_cases(tmp_path, "a.png,gate_who,cab,,\nb.png,yard_threat,who=animal,,\n")
    assert le.main(["--cases", str(p), "--check"]) == 0
    assert "2 cases OK: gate_who 1, yard_threat 1" in capsys.readouterr().out
    p.write_text("file,template,expect\na.png,gate_who,parcel\n")
    assert le.main(["--cases", str(p), "--check"]) == 2
    assert "parcel" in capsys.readouterr().err


def test_max_side_shrinks_what_the_model_sees_and_leaves_small_pictures(tmp_path):
    from io import BytesIO

    from PIL import Image
    buf = BytesIO()
    Image.new("RGB", (1920, 1080), "grey").save(buf, format="JPEG")
    big = buf.getvalue()
    small = le.shrink(big, 768)
    assert Image.open(BytesIO(small)).size == (768, 432)
    assert le.shrink(PNG, 768) == PNG and le.shrink(big, None) == big
    (tmp_path / "big.jpg").write_bytes(big)
    p = tmp_path / "cases.csv"
    p.write_text("file,template,expect\nbig.jpg,gate_who,cab\n")
    llm = Script(gate("cab"))
    asyncio.run(le.run(llm, TEMPLATES, le.load_cases(p, TEMPLATES), repeat=1, hardware="cpu",
                       max_side=768))
    import base64
    sent = base64.b64decode(llm.calls[1]["messages"][1]["images"][0])
    assert Image.open(BytesIO(sent)).size == (768, 432)
