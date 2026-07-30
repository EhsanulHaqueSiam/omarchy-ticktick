"""Credential store: ``$XDG_CONFIG_HOME/ticktick/credentials.json``, mode 0600.

Stored keys: ``access_token auth_method auth_source adopt_external api_base client_id
client_secret redirect_uri expires_at inbox_id project_cache``. The file holds a
long-lived bearer token, so writes are atomic and the permissions are set before the
file is ever reachable under its final name.

Reads are deliberately total: a corrupt or half-written file yields ``{}`` and the caller
falls through to "not signed in" rather than crashing the bar widget.
"""

from __future__ import annotations

import json
import os
import pathlib
import tempfile
import time
from datetime import datetime
from typing import Any

from .errors import AuthError

# Resolved once at import: the shell that launches `bin/ticktick` sets XDG_CONFIG_HOME (or
# doesn't) before we start. Tests monkeypatch CONFIG_PATH directly.
_XDG = os.environ.get("XDG_CONFIG_HOME") or ""
CONFIG_DIR: pathlib.Path = (
    pathlib.Path(_XDG) if _XDG else pathlib.Path.home() / ".config"
) / "ticktick"
CONFIG_PATH: pathlib.Path = CONFIG_DIR / "credentials.json"

#: Cap on `project_cache`; entries are evicted oldest-insertion-first.
PROJECT_CACHE_MAX = 500


#: Other tools on this machine that hold a TickTick bearer token for the same user.
#: Only read, never written. `@ticktick/ticktick-cli` is TickTick's own CLI.
EXTERNAL_TOKEN_PATHS = (
    (pathlib.Path(_XDG) if _XDG else pathlib.Path.home() / ".config")
    / "ticktick-cli" / "config.json",
)


def load() -> dict:
    """Return the stored config, or ``{}`` when it is absent, unreadable or malformed.

    When nothing is signed in here but TickTick's own CLI is signed in on this
    machine, its token is borrowed. Both tools authenticate the same user against the
    same API with the same kind of bearer token, so asking again would be busywork —
    someone who already ran `ticktick auth login` should find the widget already
    working. The other tool's file is never modified, and `logout` sets a flag that
    stops the borrowing so signing out stays signed out.
    """
    try:
        raw = CONFIG_PATH.read_text(encoding="utf-8")
        data: Any = json.loads(raw)
    except (OSError, ValueError, UnicodeDecodeError):
        data = {}
    # A file containing `null` or `[]` is as useless as a missing one.
    cfg = data if isinstance(data, dict) else {}

    if not cfg.get("access_token") and cfg.get("adopt_external") is not False:
        token, source = external_token()
        if token:
            cfg["access_token"] = token
            cfg["auth_method"] = "adopted"
            cfg["auth_source"] = source
    return cfg


def external_token() -> tuple[str, str]:
    """A bearer token another TickTick tool has stored, as ``(token, source)``."""
    for path in EXTERNAL_TOKEN_PATHS:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, UnicodeDecodeError):
            continue
        if isinstance(data, dict):
            token = str(data.get("access_token") or "").strip()
            if token:
                return token, str(path)
    return "", ""


def save(cfg: dict) -> None:
    """Write `cfg` atomically with mode 0600, creating parent directories as needed.

    The temp file lives in the destination directory so ``os.replace`` is a same-filesystem
    rename: readers see either the old file or the new one, never a truncated token.
    """
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(
        dir=str(CONFIG_PATH.parent), prefix=".credentials-", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())  # rename is only atomic w.r.t. data that reached the disk
        os.chmod(tmp, 0o600)  # before the rename: the token is never world-readable, ever
        os.replace(tmp, CONFIG_PATH)
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise


def token(cfg: dict | None = None) -> str:
    """Return the stored access token, loading the config when none is supplied.

    Raises:
        AuthError: no token has been stored yet.
    """
    cfg = load() if cfg is None else cfg
    value = cfg.get("access_token")
    if not value:
        raise AuthError("not signed in — run `ticktick login`")
    return str(value)


def is_expired(cfg: dict) -> bool:
    """Report whether the stored token is past ``expires_at``.

    Accepts epoch seconds or an ISO-8601 string. A missing or unparseable value returns
    False on purpose: a wrong local clock must not lock the user out, and the API's 401 is
    the authoritative answer anyway.
    """
    raw = (cfg or {}).get("expires_at")
    if raw is None or raw == "":
        return False
    try:
        if isinstance(raw, bool):  # bool is an int; treat it as garbage, not as epoch 0/1
            return False
        if isinstance(raw, (int, float)):
            expiry = float(raw)
        else:
            expiry = datetime.fromisoformat(str(raw)).timestamp()
    except (TypeError, ValueError, OverflowError, OSError):
        return False
    return expiry <= time.time()


def cached_project(cfg: dict, task_id: str) -> str | None:
    """Return the project id remembered for `task_id`, or None."""
    cache = (cfg or {}).get("project_cache")
    if not isinstance(cache, dict):
        return None
    value = cache.get(task_id)
    return str(value) if value else None


def cache_project(cfg: dict, task_id: str, project_id: str) -> None:
    """Remember ``task_id -> project_id`` in `cfg` (the caller persists it with `save`).

    Capped at `PROJECT_CACHE_MAX`; re-inserting a known key moves it to the young end, so
    eviction is LRU-ish by insertion order rather than strictly oldest-first.
    """
    if not task_id or not project_id:
        return
    cache = cfg.get("project_cache")
    if not isinstance(cache, dict):
        cache = {}
        cfg["project_cache"] = cache
    cache.pop(task_id, None)
    cache[task_id] = project_id
    while len(cache) > PROJECT_CACHE_MAX:
        del cache[next(iter(cache))]


__all__ = [
    "CONFIG_DIR",
    "CONFIG_PATH",
    "EXTERNAL_TOKEN_PATHS",
    "PROJECT_CACHE_MAX",
    "external_token",
    "load",
    "save",
    "token",
    "is_expired",
    "cached_project",
    "cache_project",
]
