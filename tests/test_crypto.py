"""Tests for per-user secret encryption (jobflow/crypto.py)."""

import importlib

import pytest


@pytest.fixture
def crypto(monkeypatch):
    monkeypatch.setenv("JOBFLOW_SECRET_KEY", "test-secret-key-for-crypto-tests")
    import jobflow.crypto as c
    return importlib.reload(c)


def test_round_trip(crypto):
    token = crypto.encrypt_secret("sk-ant-abc123")
    assert isinstance(token, bytes)
    assert token != b"sk-ant-abc123"          # actually encrypted
    assert crypto.decrypt_secret(token) == "sk-ant-abc123"


def test_decrypt_empty_is_none(crypto):
    assert crypto.decrypt_secret(None) is None
    assert crypto.decrypt_secret(b"") is None


def test_decrypt_garbage_is_none(crypto):
    assert crypto.decrypt_secret(b"not-a-valid-fernet-token") is None


def test_decrypt_with_wrong_key_is_none(crypto, monkeypatch):
    token = crypto.encrypt_secret("sk-ant-xyz")
    monkeypatch.setenv("JOBFLOW_SECRET_KEY", "a-completely-different-secret")
    assert crypto.decrypt_secret(token) is None   # can't decrypt under new key


def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv("JOBFLOW_SECRET_KEY", raising=False)
    import jobflow.crypto as c
    importlib.reload(c)
    with pytest.raises(RuntimeError):
        c.encrypt_secret("x")
