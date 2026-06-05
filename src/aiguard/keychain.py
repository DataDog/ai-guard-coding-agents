# Unless explicitly stated otherwise all files in this repository are licensed
# under the Apache 2.0 License.
#
# This product includes software developed at Datadog (https://www.datadoghq.com/).
# Copyright 2026-Present Datadog, Inc.

"""OS-keychain storage for sensitive config (``DD_API_KEY`` / ``DD_APP_KEY``).

The Datadog API and application keys are credentials, so rather than persisting
them in the plaintext ``config.env`` (even at mode 0600) we store them in the
machine keychain through the cross-platform ``keyring`` library:

* macOS — the login Keychain
* Linux — the Secret Service (libsecret / gnome-keyring, KWallet)

On a host with no usable keychain backend (headless Linux without
gnome-keyring, say), ``keyring`` has nowhere to write. There we fall back to
``config.env`` so installs still work: :func:`store` returns ``False`` and the
installer keeps the value in the file.

At service start ddtrace reads ``DD_API_KEY`` / ``DD_APP_KEY`` from the
environment. The service wrapper sources ``config.env`` but no longer finds the
keys there once the keychain has taken them, so :func:`load_into_env` pulls them
out of the keychain and into ``os.environ`` *before* ddtrace is imported.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("ai_guard")

# keyring "service" namespace; each secret is stored under its env-var name.
SERVICE = "ai-guard"

# Config keys treated as secrets and routed to the keychain.
SECRET_KEYS = ("DD_API_KEY", "DD_APP_KEY")


def _keyring():
    """Return a usable ``keyring`` module, or ``None`` if no backend works.

    Imported lazily and defensively: ``keyring`` is a runtime convenience, so a
    missing dependency, a stripped PyInstaller bundle, or a host with no Secret
    Service must all degrade to the ``config.env`` fallback rather than crash
    the CLI. keyring's ``fail`` backend is its "nothing available" sentinel, so
    we treat it as unavailable.
    """
    try:
        import keyring
        from keyring.backends.fail import Keyring as FailKeyring
    except Exception:
        logger.debug("keyring unavailable; falling back to config.env", exc_info=True)
        return None
    try:
        if isinstance(keyring.get_keyring(), FailKeyring):
            return None
    except Exception:
        logger.debug("keyring backend probe failed; falling back to config.env", exc_info=True)
        return None
    return keyring


def available() -> bool:
    """``True`` when a real keychain backend is reachable on this host."""
    return _keyring() is not None


def store(key: str, value: str) -> bool:
    """Persist ``key`` in the keychain. Return ``True`` on success.

    ``False`` means no backend was reachable, or the write failed — the caller
    should keep the value in ``config.env`` instead.
    """
    kr = _keyring()
    if kr is None:
        return False
    try:
        kr.set_password(SERVICE, key, value)
        return True
    except Exception:
        logger.warning("failed to store %s in keychain", key, exc_info=True)
        return False


def load(key: str) -> str | None:
    """Return the keychain value for ``key``, or ``None`` if absent/unavailable."""
    kr = _keyring()
    if kr is None:
        return None
    try:
        return kr.get_password(SERVICE, key)
    except Exception:
        logger.debug("failed to read %s from keychain", key, exc_info=True)
        return None


def delete(key: str) -> None:
    """Remove ``key`` from the keychain if present. Never raises.

    A missing entry is expected on a host that kept its secrets in
    ``config.env`` (the fallback path), so failures are logged at debug.
    """
    kr = _keyring()
    if kr is None:
        return
    try:
        kr.delete_password(SERVICE, key)
    except Exception:
        logger.debug("failed to delete %s from keychain", key, exc_info=True)


def load_secrets() -> dict[str, str]:
    """Return every :data:`SECRET_KEYS` value currently in the keychain."""
    out: dict[str, str] = {}
    for key in SECRET_KEYS:
        value = load(key)
        if value:
            out[key] = value
    return out


def load_into_env() -> None:
    """Fill gaps in ``os.environ`` with keychain-stored secrets.

    Called before ddtrace imports so the tracer / AI Guard client authenticate.
    A value already in the environment wins — a real export, or one sourced
    from ``config.env`` by the service wrapper on a host that uses the file
    fallback — so this is a no-op in those cases.
    """
    for key, value in load_secrets().items():
        if not os.environ.get(key):
            os.environ[key] = value
