import subprocess

from app import secrets_env


def test_env_wins(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-env")
    assert secrets_env.get_secret("ANTHROPIC_API_KEY") == "from-env"


def test_keychain_fallback(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(secrets_env.sys, "platform", "darwin")

    def fake_run(cmd, **kw):
        assert cmd[:2] == ["security", "find-generic-password"]
        return subprocess.CompletedProcess(cmd, 0, stdout="from-keychain\n", stderr="")

    monkeypatch.setattr(secrets_env.subprocess, "run", fake_run)
    assert secrets_env.get_secret("ANTHROPIC_API_KEY") == "from-keychain"


def test_missing_everywhere(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(secrets_env.sys, "platform", "linux")
    assert secrets_env.get_secret("ANTHROPIC_API_KEY") is None
