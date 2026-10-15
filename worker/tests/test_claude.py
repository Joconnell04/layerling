"""Claude backends: MockClaude, Claude Code OAuth subprocess, API (turnkey), no fallback."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import make_photo, make_rt, run
from p2p_contracts import JobStatus, StageId, StageStatus
from p2p_contracts.payloads import IntakeResult, TweakRoute
from p2p_worker.clients import claude as cl
from p2p_worker.clients.claude import (
    HAIKU,
    AnthropicClaude,
    ClaudeCodeClaude,
    IntakeDraft,
    IntakeRequest,
    MockClaude,
    TweakRequest,
    make_claude,
)
from p2p_worker.errors import VendorError
from p2p_worker.stages.prompts import bank_for

INTAKE_OK = {
    "subjects": [{"id": "s1", "name": "Rex", "kind": "dog", "desc": "tan terrier",
                  "photos": [0]}],
    "questions": ["size", "not_in_bank"],
    "colors": ["s1|head and ears|tan|b", "s1|saddle on the back|black|m|#1a1a1a",
               "s9|ghost|blue|b|#0000FF", "garbage line", "s1|eyes|brown|x|#332211"],
    "product_type": "figurine", "target_size_mm": None, "notes": None,
}
SPEC_OK = {"parts": ["s1|body|coat|tan|#C8955A|1|0", "s1|marking|back saddle|black|#1A1A1A|1|0",
                     "s1|eye|left_eye|dark brown|#3A2616|1|0",
                     "s1|eye|left_eye|dark brown|#3A2616|1|0",
                     "s1|whisker|whiskers|white|#FFFFFF|0|0",
                     "s1|nose|nose|black|nothex|1|0"], "notes": None}


def result_line(obj=None, text=None, cost=0.0123, is_error=False):
    r = {"type": "result", "subtype": "success", "is_error": is_error,
         "result": text if text is not None else json.dumps(obj), "session_id": "sess-1",
         "total_cost_usd": cost,
         "usage": {"input_tokens": 900, "output_tokens": 200, "cache_read_input_tokens": 50,
                   "cache_creation_input_tokens": 0}}
    if obj is not None and text is None:
        r["structured_output"] = obj
    return r


class FakeRunner:
    def __init__(self, replies, rc=0):
        self.replies = list(replies)
        self.rc = rc
        self.calls = []

    async def __call__(self, args, stdin, env, timeout):
        self.calls.append({"args": args, "stdin": stdin, "env": env})
        reply = self.replies.pop(0)
        lines = [{"type": "system", "subtype": "init"}, reply]
        return self.rc, "\n".join(json.dumps(x) for x in lines).encode(), b""


def intake_req(tmp_path: Path) -> IntakeRequest:
    return IntakeRequest(prompt="figurine of my dog Rex", product_type="figurine",
                         photos=[make_photo(tmp_path / "p.jpg")],
                         question_bank=bank_for("figurine"))


def test_mock_claude_is_deterministic(tmp_path):
    req = intake_req(tmp_path)
    req.prompt = "keychain of my dog and cat hugging, 50 mm"
    a, regions, call = run(MockClaude().intake(req))
    b, _, _ = run(MockClaude().intake(req))
    assert any(r.kind == "marking" and "saddle" in r.region for r in regions)
    assert a == b
    assert [s.kind for s in a.subjects] == ["dog", "cat"]
    assert 2 <= len(a.questions) <= 4 and a.target_size_mm == 50
    assert call.billing == "free" and call.usd == 0 and call.usd_list > 0
    d, _ = run(MockClaude().route_tweak(TweakRequest(tweak="make the ears bigger please",
                                                     concept_prompt="x")))
    assert d.route == TweakRoute.RERENDER_CONCEPT and d.model == HAIKU


def test_claude_code_subprocess_flags_env_and_accounting(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("ANTHROPIC_API_KEY_TURNKEY", "sk-turnkey")
    runner = FakeRunner([result_line(INTAKE_OK)])
    c = ClaudeCodeClaude(cli="/bin/claude", runner=runner)
    res, regions, call = run(c.intake(intake_req(tmp_path)))
    assert isinstance(res, IntakeResult) and res.subjects[0].name == "Rex"
    # s9 and bad lines dropped; hex normalized
    assert [r.region for r in regions] == ["head and ears", "saddle on the back"]
    assert regions[1].hex == "#1A1A1A" and regions[1].kind == "marking"
    assert res.subjects[0].description.endswith(
        "Colors: tan head and ears; black saddle on the back")
    assert res.questions[0].id == "size" and res.questions[0].options  # from the bank
    assert len(res.questions) >= 2  # topped up from the bank
    args = runner.calls[0]["args"]
    assert args[:2] == ["/bin/claude", "-p"]
    assert args[args.index("--model") + 1] == HAIKU
    assert args[args.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in args and "--system-prompt" in args
    assert args[args.index("--tools") + 1] == ""
    assert "--json-schema" in args and args[args.index("--effort") + 1] == "low"
    env = runner.calls[0]["env"]
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_API_KEY_TURNKEY" not in env
    assert env["MAX_THINKING_TOKENS"] == "0"
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == str(cl.INTAKE_MAX_TOKENS)
    system = args[args.index("--system-prompt") + 1]
    assert "- size:" in system and "options:" not in system  # one line per bank question
    msg = json.loads(runner.calls[0]["stdin"].decode())
    kinds = [b["type"] for b in msg["message"]["content"]]
    assert kinds == ["image", "text"]
    assert call.billing == "subscription" and call.usd == 0.0 and call.usd_list == 0.0123
    assert call.input_tokens == 900 and call.cache_read_tokens == 50
    assert call.request_id == "sess-1" and call.effort == "low"


def test_claude_code_color_spec_uses_sonnet_and_parses_lines(tmp_path):
    runner = FakeRunner([result_line(SPEC_OK)])
    c = ClaudeCodeClaude(runner=runner)
    subj = cl.Subject(id="s1", name="Rex", kind="dog", description="tan terrier",
                      photo_indices=[0])
    req = cl.ColorSpecRequest(photos=[make_photo(tmp_path / "p.jpg")], subjects=[subj],
                              regions=[cl.ColorRegion(subject_id="s1", region="saddle",
                                                      color="black", kind="marking")])
    spec, call = run(c.color_spec(req))
    args = runner.calls[0]["args"]
    assert args[args.index("--model") + 1] == cl.SONNET and call.model == cl.SONNET
    assert runner.calls[0]["env"]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == str(cl.SPEC_MAX_TOKENS)
    assert "s1|saddle|black|m|" in runner.calls[0]["stdin"].decode()  # intake hints go in
    parts = spec.subjects[0].parts
    assert [p.label for p in parts] == ["coat", "back_saddle", "left_eye", "left_eye_2",
                                        "whiskers"]  # bad hex dropped, labels unique
    assert parts[4].part == "other" and parts[4].required is False  # not in PART_VOCAB
    assert parts[0].lab[0] == pytest.approx(66.1, abs=1.0) and parts[0].hex == "#C8955A"


def test_mock_color_spec_follows_intake_regions(tmp_path):
    req = intake_req(tmp_path)
    res, regions, _ = run(MockClaude().intake(req))
    spec, call = run(MockClaude().color_spec(cl.ColorSpecRequest(
        photos=req.photos, subjects=res.subjects, regions=regions)))
    parts = {p.label: p for p in spec.subjects[0].parts}
    assert parts["saddle"].part == "marking" and parts["eyes"].part == "eye"
    assert all(p.hex and p.lab for p in parts.values())
    assert call.billing == "free" and call.model == cl.SONNET


def test_claude_code_repairs_invalid_output_once(tmp_path):
    runner = FakeRunner([result_line(text="Sure! here you go: {not json"),
                         result_line(text="```json\n" + json.dumps(INTAKE_OK) + "\n```")])
    c = ClaudeCodeClaude(runner=runner)
    res, _, call = run(c.intake(intake_req(tmp_path)))
    assert res.subjects[0].kind == "dog" and len(runner.calls) == 2
    assert b"did not validate" in runner.calls[1]["stdin"]
    assert call.usd_list == pytest.approx(0.0246)

    bad = FakeRunner([result_line(text="{}"), result_line(text="{}")])
    with pytest.raises(VendorError) as e:
        run(ClaudeCodeClaude(runner=bad).intake(intake_req(tmp_path)))
    assert e.value.code == "validation"


def test_claude_code_read_vision_mode(tmp_path):
    runner = FakeRunner([result_line({"route": "retexture", "instruction": "make it red",
                                      "followup_question": None})])
    c = ClaudeCodeClaude(runner=runner, vision="read")
    d, _ = run(c.route_tweak(TweakRequest(tweak="red fur", concept_prompt="x",
                                          image=make_photo(tmp_path / "c.png"))))
    assert d.route == TweakRoute.RETEXTURE
    args = runner.calls[0]["args"]
    assert args[args.index("--tools") + 1] == "Read"
    assert args[args.index("--allowedTools") + 1] == "Read" and "--add-dir" in args
    assert b"photo_0." in runner.calls[0]["stdin"]


def test_no_fallback_to_the_api_key(settings, photos, monkeypatch):
    """claude_code failing must fail the stage; the turnkey API key is never touched."""

    def boom(*a, **k):
        raise AssertionError("AnthropicClaude must not be constructed")

    monkeypatch.setattr(AnthropicClaude, "__init__", boom)
    s = settings.with_(mock_claude=False, claude_backend="claude_code",
                       anthropic_turnkey_key="sk-turnkey")
    c = make_claude(s)
    assert isinstance(c, ClaudeCodeClaude)
    runner = FakeRunner([result_line(INTAKE_OK, is_error=True)] * 9, rc=1)
    c.runner = runner
    rt = make_rt(s, claude=c)
    job = rt.orchestrator.create_job("figurine of my dog", photos[:1])
    status = run(rt.orchestrator.advance(job.id))
    assert status == JobStatus.PAUSED
    m = rt.store.load_manifest(job.id, StageId.S01_INTAKE, 3)
    assert m.status == StageStatus.PAUSED and "claude -p failed" in m.pause_reason
    # a failed Claude call replays its attempt twice (lifecycle.RESUME_BUDGET), then a fresh
    # attempt: 3 attempts x 3 tries, never the API key
    assert len(runner.calls) == 9
    assert all("ANTHROPIC_API_KEY_TURNKEY" not in call["env"] for call in runner.calls)


def test_make_claude_backends(settings):
    assert isinstance(make_claude(settings), MockClaude)
    assert isinstance(make_claude(settings.with_(mock_claude=False,
                                                 claude_backend="claude_code")),
                      ClaudeCodeClaude)
    with pytest.raises(VendorError):
        make_claude(settings.with_(mock_claude=False, claude_backend="api",
                                   anthropic_turnkey_key=None))


def test_api_backend_request_shape(tmp_path):
    seen = {}

    class Msgs:
        async def parse(self, **kw):
            seen.update(kw)
            return SimpleNamespace(
                parsed_output=IntakeDraft.model_validate(INTAKE_OK), stop_reason="end_turn",
                usage=SimpleNamespace(input_tokens=1000, output_tokens=100,
                                      cache_creation_input_tokens=0, cache_read_input_tokens=0),
                _request_id="req_1")

    c = AnthropicClaude(client=SimpleNamespace(messages=Msgs()))
    _res, _regions, call = run(c.intake(intake_req(tmp_path)))
    assert seen["model"] == HAIKU and seen["output_format"] is IntakeDraft
    assert seen["max_tokens"] == cl.INTAKE_MAX_TOKENS
    assert seen["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert seen["messages"][0]["content"][0]["type"] == "image"
    assert call.billing == "api" and call.usd == pytest.approx(0.0015) == call.usd_list


@pytest.mark.live
def test_live_claude_code_haiku_smoke(tmp_path):
    """Opt-in (RUN_LIVE_CLAUDE=1): one real Haiku intake through the Claude Code login."""
    from p2p_worker.config import _default_cli

    c = ClaudeCodeClaude(cli=_default_cli(), model=HAIKU, timeout_s=240)
    res, regions, call = run(c.intake(intake_req(tmp_path)))
    assert res.subjects and 2 <= len(res.questions) <= 4 and regions
    assert call.billing == "subscription" and call.usd == 0.0
    print("live intake:", res.model_dump_json()[:300], "usd_list", call.usd_list)


def test_estimates_are_zero_for_subscription(tmp_path):
    c = ClaudeCodeClaude(runner=FakeRunner([]))
    assert c.estimate_intake(intake_req(tmp_path)) == 0.0
    assert cl.estimate_usd(HAIKU, n_images=2, text_chars=3000, max_tokens=2048) > 0


# ------------------------------------------------------------------ concept and view checks
def check_req(tmp_path: Path, text: str | None = "Bronco") -> cl.ConceptCheckRequest:
    return cl.ConceptCheckRequest(
        request="nameplate for my beagle bronco, give him a bow tie",
        photos=[make_photo(tmp_path / "p.jpg")], figure=make_photo(tmp_path / "f.png"),
        product=make_photo(tmp_path / "c.png"),
        items=[cl.CheckSpec("part.nameplate", "part", "A plate", "product"),
               cl.CheckSpec("text", "text", "The name reads exactly Bronco", "product"),
               cl.CheckSpec("ask.1", "ask", "wearing a bow tie", "figure")],
        expected_text=text)


def test_claude_code_concept_check_uses_haiku_and_reads_the_name_exactly(tmp_path):
    ok = {"items": [{"id": "part.nameplate", "ok": True}, {"id": "text", "ok": True},
                    {"id": "ask.1", "ok": False, "why": "no bow tie"}], "text_read": "Bronco"}
    runner = FakeRunner([result_line(ok)])
    c = ClaudeCodeClaude(runner=runner)
    chk, call = run(c.check_concept(check_req(tmp_path)))
    args = runner.calls[0]["args"]
    assert args[args.index("--model") + 1] == HAIKU == cl.CHECK_MODEL
    assert call.billing == "subscription" and call.usd == 0
    stdin = json.loads(runner.calls[0]["stdin"])["message"]["content"]
    assert [b["type"] for b in stdin] == ["image", "image", "image", "text"]  # photo, fig, product
    assert "then FIGURE, then PRODUCT" in stdin[-1]["text"]
    assert "The name must read exactly: Bronco" in stdin[-1]["text"]
    assert not chk.passed
    assert {i.id: i.passed for i in chk.items} == {"part.nameplate": True, "text": True,
                                                  "ask.1": False}
    assert next(i for i in chk.items if i.id == "ask.1").note == "no bow tie"

    # the model says ok but read "BRONCO": case counts, the item fails
    loud = {**ok, "items": [{**x, "ok": True} for x in ok["items"]], "text_read": "BRONCO"}
    chk, _ = run(ClaudeCodeClaude(runner=FakeRunner([result_line(loud)])).check_concept(
        check_req(tmp_path)))
    text = next(i for i in chk.items if i.id == "text")
    assert not chk.passed and not text.passed and "must read 'Bronco'" in text.note
    # an item the model skipped fails
    skip = {"items": [{"id": "text", "ok": True}], "text_read": "Bronco"}
    chk, _ = run(ClaudeCodeClaude(runner=FakeRunner([result_line(skip)])).check_concept(
        check_req(tmp_path)))
    assert next(i for i in chk.items if i.id == "part.nameplate").note == "not answered"


def test_claude_code_view_check_orders_answers_and_fails_missing_views(tmp_path):
    reply = {"views": [{"index": 2, "angle": "back", "ok": True},
                       {"index": 1, "angle": "front", "ok": False, "why": "saddle moved"}]}
    runner = FakeRunner([result_line(reply)])
    req = cl.ViewCheckRequest(concept=make_photo(tmp_path / "c.png"),
                              views=[make_photo(tmp_path / f"v{i}.png") for i in range(3)],
                              regions=["black saddle on the back"], asks=["wearing a sombrero"])
    answers, call = run(ClaudeCodeClaude(runner=runner).check_views(req))
    assert [(a.index, a.angle, a.ok) for a in answers] == [(1, "front", False), (2, "back", True),
                                                          (3, "other", False)]
    assert answers[2].why == "not answered" and call.model == HAIKU
    text = json.loads(runner.calls[0]["stdin"])["message"]["content"][-1]["text"]
    assert "APPROVED, then views 1 to 3" in text and "wearing a sombrero" in text


def test_intake_reads_ask_lines_and_the_request_form(tmp_path):
    draft = {**INTAKE_OK, "asks": ["outfit|s1|wearing a sombrero", "junk", "prop|s9|a ball"],
             "product_type": "keychain", "questions": []}
    req = IntakeRequest(prompt="3d keychain of my dog Rex wearing a sombrero holding a stick",
                        product_type="keychain", photos=[make_photo(tmp_path / "p.jpg")],
                        question_bank=bank_for("keychain"))
    res, _, _ = run(ClaudeCodeClaude(runner=FakeRunner([result_line(draft)])).intake(req))
    asks = [(a.kind, a.text, a.subject_id) for a in res.custom_asks]
    # the model's lines, then the code parser's find the model missed (no duplicate sombrero)
    assert asks == [("outfit", "wearing a sombrero", "s1"), ("prop", "a ball", None),
                    ("prop", "holding a stick", None)]
    assert res.form == "figure"  # "3d" in the request: the form question is not asked
    assert "form" not in [q.id for q in res.questions]


def test_intake_notes_lose_stray_json_closers(tmp_path):
    """live2 run: Haiku ended the notes with the JSON it was writing ('photos."}]'), and the
    concept prompt carried it."""
    from p2p_worker.clients.claude import clean_notes

    draft = {**INTAKE_OK, "notes": 'Whimsical sombrero. Capture his grin from photos."}]'}
    req = IntakeRequest(prompt="make my dog wear a sombrero", product_type="figurine",
                        photos=[make_photo(tmp_path / "p.jpg")], question_bank=bank_for("figurine"))
    res, _, _ = run(ClaudeCodeClaude(runner=FakeRunner([result_line(draft)])).intake(req))
    assert res.notes == "Whimsical sombrero. Capture his grin from photos."
    assert clean_notes("Keep the red collar.") == "Keep the red collar."
    assert clean_notes('"}]') is None and clean_notes(None) is None
