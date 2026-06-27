from __future__ import annotations

from security.credentials import redact_secrets, resolve_executor_env


def test_resolve_executor_env_scopes_credentials_to_selected_exchange():
    source = {
        "PATH": "/usr/bin",
        "OKX_DEMO_API_KEY": "okx-demo-key",
        "OKX_DEMO_SECRET_KEY": "okx-demo-secret",
        "OKX_DEMO_PASSPHRASE": "okx-demo-pass",
        "KUCOIN_PAPER_API_KEY": "kucoin-key",
        "UNRELATED_SECRET": "should-not-leak",
    }
    env = resolve_executor_env("okx_demo", source, {"OKX_SUBMIT_ORDERS": "false"})

    assert env["PATH"] == "/usr/bin"
    assert env["OKX_API_KEY"] == "okx-demo-key"
    assert env["OKX_SECRET_KEY"] == "okx-demo-secret"
    assert env["OKX_PASSPHRASE"] == "okx-demo-pass"
    assert env["OKX_SUBMIT_ORDERS"] == "false"
    assert "KUCOIN_PAPER_API_KEY" not in env
    assert "UNRELATED_SECRET" not in env


def test_redact_secrets_scrubs_known_values(monkeypatch):
    monkeypatch.setenv("OKX_API_KEY", "real-okx-key")
    text = 'stderr: {"OKX_API_KEY":"real-okx-key","token":"abc"}'
    redacted = redact_secrets(text)
    assert "real-okx-key" not in redacted
    assert "<redacted>" in redacted
