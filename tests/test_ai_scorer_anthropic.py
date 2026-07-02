"""Tests for the server-side Anthropic scorer (mocked client)."""

import sys
import types

from jobflow import ai_scorer_anthropic as scorer


class _FakeBlock:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeMessages:
    def __init__(self, text, sink):
        self._text = text
        self._sink = sink

    def create(self, **kwargs):
        self._sink.update(kwargs)
        return types.SimpleNamespace(content=[_FakeBlock(self._text)])


def _install_fake_anthropic(monkeypatch, response_text, sink):
    fake = types.ModuleType("anthropic")

    class Anthropic:
        def __init__(self, api_key=None):
            sink["api_key"] = api_key
            self.messages = _FakeMessages(response_text, sink)

    fake.Anthropic = Anthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    return sink


BATCH = [
    ("u1", "Acme", "Backend Engineer", "Remote, US", "Python, FastAPI, AWS"),
    ("u2", "Beta", "ML Engineer", "NYC", "PyTorch, LLMs"),
]


def test_score_batch_parses_and_passes_params(monkeypatch):
    sink = {}
    _install_fake_anthropic(
        monkeypatch,
        '[{"id":1,"score":9,"reason":"great fit"},{"id":2,"score":7,"reason":"good"}]',
        sink,
    )
    out = scorer.score_batch(BATCH, "profile text", "sk-ant-test", model="claude-sonnet-5")
    assert out == [
        {"id": 1, "score": 9, "reason": "great fit"},
        {"id": 2, "score": 7, "reason": "good"},
    ]
    assert sink["api_key"] == "sk-ant-test"
    assert sink["model"] == "claude-sonnet-5"
    # no sampling params sent (rejected by current Claude models)
    assert "temperature" not in sink and "top_p" not in sink


def test_unknown_model_falls_back_to_haiku(monkeypatch):
    sink = {}
    _install_fake_anthropic(monkeypatch, "[]", sink)
    scorer.score_batch(BATCH, "p", "sk-ant-test", model="gpt-4")
    assert sink["model"] == scorer.DEFAULT_MODEL == "claude-haiku-4-5"


def test_no_api_key_returns_none(monkeypatch):
    assert scorer.score_batch(BATCH, "p", "", model="claude-haiku-4-5") is None


def test_api_error_returns_none(monkeypatch):
    fake = types.ModuleType("anthropic")

    class Anthropic:
        def __init__(self, api_key=None):
            self.messages = self

        def create(self, **kwargs):
            raise RuntimeError("boom")

    fake.Anthropic = Anthropic
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    assert scorer.score_batch(BATCH, "p", "sk-ant-test") is None
