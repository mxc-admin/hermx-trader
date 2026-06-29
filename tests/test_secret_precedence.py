"""Unified secret: HERMX_SECRET is the SOLE source for webhook and dashboard auth.

``webhook_receiver.SECRET`` and ``dashboard.DASH_AUTH_TOKEN`` are resolved at *import*
time directly from ``HERMX_SECRET`` -- there is no legacy fallback:

    SECRET           = HERMX_SECRET
    DASH_AUTH_TOKEN  = HERMX_SECRET

The legacy ``SHADOW_WEBHOOK_SECRET`` / ``HERMX_DASH_AUTH_TOKEN`` variables were
removed entirely; setting them must have NO effect. An empty/missing HERMX_SECRET
resolves to "" so auth fails closed. These tests reload each module under controlled
env so the binding is exercised faithfully through the real module-load path rather
than monkeypatching the global.
"""
from __future__ import annotations

import importlib
import os



def _reload_with(module_name: str, env: dict, shadow_root) -> tuple:
    """Reload ``module_name`` with ``env`` applied (None => unset). Returns
    ``(module, restore_callable)``; ``restore`` puts every touched env var back and
    reloads the module so later tests see the conftest defaults again."""
    keys = list(env) + ["SHADOW_ROOT"]
    saved = {k: os.environ.get(k) for k in keys}

    os.environ["SHADOW_ROOT"] = str(shadow_root)
    for key, value in env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    module = importlib.import_module(module_name)
    importlib.reload(module)

    def restore() -> None:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        importlib.reload(module)

    return module, restore


# ---------------------------------------------------------------------------
# webhook_receiver.SECRET = HERMX_SECRET (no legacy fallback)
# ---------------------------------------------------------------------------

def test_webhook_secret_is_hermx_secret(shadow_root):
    module, restore = _reload_with(
        "webhook_receiver",
        {"HERMX_SECRET": "unified-secret"},
        shadow_root,
    )
    try:
        assert module.SECRET == "unified-secret"
    finally:
        restore()


def test_legacy_shadow_webhook_secret_is_ignored(shadow_root):
    # The legacy var must have NO effect: with HERMX_SECRET unset the secret is blank.
    module, restore = _reload_with(
        "webhook_receiver",
        {"HERMX_SECRET": None, "SHADOW_WEBHOOK_SECRET": "legacy-webhook-secret"},
        shadow_root,
    )
    try:
        assert module.SECRET == ""
    finally:
        restore()


def test_blank_hermx_secret_fails_closed_webhook(shadow_root):
    # A blank HERMX_SECRET resolves to "" (fail closed) -- no fallthrough to legacy.
    module, restore = _reload_with(
        "webhook_receiver",
        {"HERMX_SECRET": "", "SHADOW_WEBHOOK_SECRET": "legacy-webhook-secret"},
        shadow_root,
    )
    try:
        assert module.SECRET == ""
    finally:
        restore()


def test_missing_hermx_secret_fails_closed_webhook(shadow_root):
    module, restore = _reload_with(
        "webhook_receiver",
        {"HERMX_SECRET": None},
        shadow_root,
    )
    try:
        assert module.SECRET == ""
    finally:
        restore()


# ---------------------------------------------------------------------------
# dashboard.DASH_AUTH_TOKEN = HERMX_SECRET (no legacy fallback)
# ---------------------------------------------------------------------------

def test_dash_token_is_hermx_secret(shadow_root):
    module, restore = _reload_with(
        "dashboard",
        {"HERMX_SECRET": "unified-secret"},
        shadow_root,
    )
    try:
        assert module.DASH_AUTH_TOKEN == "unified-secret"
    finally:
        restore()


def test_legacy_dash_auth_token_is_ignored(shadow_root):
    # The legacy var must have NO effect: with HERMX_SECRET unset the token is blank.
    module, restore = _reload_with(
        "dashboard",
        {"HERMX_SECRET": None, "HERMX_DASH_AUTH_TOKEN": "legacy-dash-token"},
        shadow_root,
    )
    try:
        assert module.DASH_AUTH_TOKEN == ""
    finally:
        restore()


def test_blank_hermx_secret_fails_closed_dashboard(shadow_root):
    module, restore = _reload_with(
        "dashboard",
        {"HERMX_SECRET": "", "HERMX_DASH_AUTH_TOKEN": "legacy-dash-token"},
        shadow_root,
    )
    try:
        assert module.DASH_AUTH_TOKEN == ""
    finally:
        restore()
