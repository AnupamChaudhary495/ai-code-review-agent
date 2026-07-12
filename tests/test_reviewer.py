"""Reviewer tests with a scripted fake Anthropic client — no real LLM calls."""

import json
from types import SimpleNamespace

import pytest

from helpers import load_fixture
from review_agent import reviewer
from review_agent.diffing import parser
from review_agent.reviewer import ReviewOutputError, render_change, review_file

VALID_OUTPUT = json.dumps(
    {
        "findings": [
            {
                "file": "will-be-overwritten.py",
                "line": 12,
                "category": "bug",
                "severity": "high",
                "message": "Off-by-one in the loop bound.",
                "suggestion": "Iterate over the whole list.",
            }
        ]
    }
)


def fake_response(text: str, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=1000, output_tokens=100),
        model="claude-test",
    )


class ScriptedClient:
    """Stands in for anthropic.Anthropic; returns queued responses in order."""

    def __init__(self, responses: list[SimpleNamespace]):
        self._queue = list(responses)
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        return self._queue.pop(0)


@pytest.fixture
def sample_change(settings_env):
    entry = next(f for f in load_fixture("pr_edge_files.json") if f["filename"] == "long_module.py")
    return parser.parse_file(entry)


def scripted(monkeypatch, responses: list[SimpleNamespace]) -> ScriptedClient:
    fake = ScriptedClient(responses)
    monkeypatch.setattr(reviewer.anthropic, "Anthropic", lambda **kwargs: fake)
    return fake


def test_clean_json_parses_without_repair(monkeypatch, sample_change):
    fake = scripted(monkeypatch, [fake_response(VALID_OUTPUT)])
    result = review_file(sample_change)

    assert not result.repair_used
    (finding,) = result.findings
    assert finding.file == sample_change.path  # path is ours, not the model's
    assert finding.category == "bug"
    assert result.input_tokens == 1000
    # system/user boundary: instructions in system, diff (untrusted) in user turn
    call = fake.calls[0]
    assert "untrusted" in call["system"].lower()
    assert call["messages"][0]["content"].startswith("<diff>")


def test_fenced_json_is_tolerated(monkeypatch, sample_change):
    fenced = f"```json\n{VALID_OUTPUT}\n```"
    scripted(monkeypatch, [fake_response(fenced)])
    result = review_file(sample_change)
    assert not result.repair_used
    assert len(result.findings) == 1


def test_malformed_output_gets_one_repair_retry(monkeypatch, sample_change):
    fake = scripted(
        monkeypatch,
        [fake_response('{"findings": [{"file": "x", "cat'), fake_response(VALID_OUTPUT)],
    )
    result = review_file(sample_change)

    assert result.repair_used
    assert len(result.findings) == 1
    assert len(fake.calls) == 2
    # The repair turn carries the bad output and the parse error back to the model.
    repair_messages = fake.calls[1]["messages"]
    assert repair_messages[1]["role"] == "assistant"
    assert "could not be parsed" in repair_messages[2]["content"]
    # Token accounting spans both calls.
    assert result.input_tokens == 2000


def test_gives_up_after_failed_repair(monkeypatch, sample_change):
    scripted(monkeypatch, [fake_response("not json"), fake_response("still not json")])
    with pytest.raises(ReviewOutputError):
        review_file(sample_change)


def test_schema_violation_triggers_repair(monkeypatch, sample_change):
    bad_enum = json.dumps(
        {"findings": [{"file": "x", "category": "vibes", "severity": "high", "message": "m"}]}
    )
    scripted(monkeypatch, [fake_response(bad_enum), fake_response(VALID_OUTPUT)])
    result = review_file(sample_change)
    assert result.repair_used


def test_empty_findings_is_valid(monkeypatch, sample_change):
    scripted(monkeypatch, [fake_response('{"findings": []}')])
    result = review_file(sample_change)
    assert result.findings == []
    assert not result.repair_used


def test_binary_change_is_rejected_before_any_llm_call(monkeypatch, settings_env):
    entry = next(f for f in load_fixture("pr_edge_files.json") if f["filename"] == "data.bin")
    change = parser.parse_file(entry)
    with pytest.raises(ValueError, match="no reviewable hunks"):
        review_file(change)


def test_render_change_numbers_new_file_lines(sample_change):
    rendered = render_change(sample_change)
    assert rendered.startswith("File: long_module.py")
    first_hunk = sample_change.hunks[0]
    # every numbered line matches the hunk's declared new-file coverage
    numbered = {int(line[:6]) for line in rendered.splitlines() if line[:6].strip().isdigit()}
    expected = set()
    for hunk in sample_change.hunks:
        expected |= hunk.new_lines()
    assert numbered == expected
    assert str(first_hunk.new_start) in rendered
