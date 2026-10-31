# Copyright (c) 2026 OpenNVR
# SPDX-License-Identifier: Apache-2.0
"""``opennvr-app ai try``: a Look step on pictures, with a scripted model."""
from __future__ import annotations

import base64
import io
import json

import pytest

from opennvr_app_sdk import aitry
from opennvr_app_sdk.agent import ChatResult, LLMUnreachableError
from opennvr_app_sdk.scaffold import main

GATE = """
mode: look
prompt: Who is at the gate?
decision: [delivery, cab, visitor]
fields: { company: [swiggy, zomato, other, none] }
rules:
  - { when: { company: [swiggy, zomato] }, then: delivery }
  - { when: { company: none }, then: visitor }
"""


class Script:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def chat(self, *, messages, tools=None, format=None, temperature=0.2, max_tokens=256):
        self.calls.append({"messages": messages, "max_tokens": max_tokens})
        if max_tokens == 1:
            return ChatResult(content="ok")
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return ChatResult(content=a if isinstance(a, str) else json.dumps(a))


def jpeg(size=(1920, 1080)) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, "grey").save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture()
def files(tmp_path):
    (tmp_path / "gate.yml").write_text(GATE)
    (tmp_path / "a.jpg").write_bytes(jpeg())
    (tmp_path / "b.jpg").write_bytes(jpeg((640, 360)))
    return tmp_path


def args(files, *extra):
    import argparse
    p = argparse.ArgumentParser()
    aitry.add_parser(p.add_subparsers(dest="command"))
    return p.parse_args(["ai", "try", str(files / "gate.yml"), str(files / "a.jpg"),
                         str(files / "b.jpg"), "--model", "m", *extra])


def answer(company):
    return {"reason": f"{company} bag", "company": company, "confidence": 0.9}


def test_a_look_step_runs_on_each_picture_and_says_why(files, capsys):
    llm = Script(answer("swiggy"), answer("none"))
    assert aitry.run_try(args(files, "--input", "plate=KA01"), client=llm) == 0
    out = capsys.readouterr().out
    assert "a.jpg: delivery  company=swiggy  (rule 1)" in out
    assert "b.jpg: visitor  company=none  (rule 2)" in out
    assert "reason: swiggy bag" in out
    sent = llm.calls[1]["messages"][1]
    assert "KA01" in sent["content"]                      # the input, as data
    from PIL import Image
    assert Image.open(io.BytesIO(base64.b64decode(sent["images"][0]))).size == (768, 432)


def test_json_output_and_no_shrinking(files, capsys):
    llm = Script(answer("other"), answer("swiggy"))
    assert aitry.run_try(args(files, "--json", "--max-side", "0"), client=llm) == 0
    a, b = json.loads(capsys.readouterr().out)
    assert (a["outcome"], a["unsure_reason"], a["shrunk"]) == ("unsure", "no_rule", False)
    assert b["outcome"] == "delivery" and b["model_calls"] == 1


def test_an_answer_that_is_not_one_shows_what_the_model_said(files, capsys):
    llm = Script('{"reason": "a very long reason that never clo', answer("swiggy"))
    assert aitry.run_try(args(files), client=llm) == 0
    out = capsys.readouterr().out
    assert "unsure: the model's answer was not one the step allows" in out
    assert "the model said: {\"reason\": \"a very long" in out


def test_a_model_that_is_not_there_is_said_plainly(files, capsys):
    class Down(Script):
        async def chat(self, **kw):
            raise LLMUnreachableError("connection refused")
    assert aitry.run_try(args(files), client=Down()) == 1
    assert "did not answer" in capsys.readouterr().err


@pytest.mark.parametrize("doc, where", [
    (GATE, "ai"),
    ("ai:\n" + "".join(f"  {ln}\n" for ln in GATE.strip().splitlines()), "ai"),
    ("name: Gate\nthen:\n  - log: hi\n  - ai:\n"
     + "".join(f"      {ln}\n" for ln in GATE.strip().splitlines()), "then[1].ai"),
])
def test_the_step_is_found_in_a_step_an_ai_key_or_a_workflow(tmp_path, doc, where):
    (tmp_path / "s.yml").write_text(doc)
    step = aitry.load_step(tmp_path / "s.yml")
    assert step.mode == "look" and step.spec.options == ("delivery", "cab", "visitor")


@pytest.mark.parametrize("doc, says", [
    (("mode: investigate\ngoal: g\ntools: [describe_camera]\ndecision: [a]\nfields: {x: [y]}\n"
      "rules: [{then: a}]\n"), "runs Look steps"),
    ("mode: look\nprompt: Is {plate} known?\ndecision: [known, unknown]\n", "{plate} is not filled in"),
    ("name: nothing here\n", "no AI step found"),
])
def test_a_step_that_cannot_be_tried_says_why(tmp_path, capsys, doc, says):
    (tmp_path / "s.yml").write_text(doc)
    (tmp_path / "a.jpg").write_bytes(b"x")
    assert main(["ai", "try", str(tmp_path / "s.yml"), str(tmp_path / "a.jpg"),
                 "--model", "m"]) == 2
    assert says in capsys.readouterr().err


def test_a_missing_picture_or_a_bad_input_is_refused(files, capsys):
    assert main(["ai", "try", str(files / "gate.yml"), str(files / "nope.jpg"),
                 "--model", "m"]) == 2
    assert "no picture at" in capsys.readouterr().err
    assert main(["ai", "try", str(files / "gate.yml"), str(files / "a.jpg"), "--model", "m",
                 "--input", "plate"]) == 2
    assert "expected name=value" in capsys.readouterr().err


def test_without_pillow_pictures_go_as_they_are(monkeypatch):
    import builtins
    real = builtins.__import__

    def no_pil(name, *a, **kw):
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("no PIL")
        return real(name, *a, **kw)
    monkeypatch.setattr(builtins, "__import__", no_pil)
    big = b"\xff\xd8not really a jpeg"
    assert aitry.shrink(big, 768) == (big, False)
    assert aitry._pillow() is False
