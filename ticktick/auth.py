"""OAuth2 authorization-code flow against TickTick, driven from a terminal.

TickTick only issues tokens to a registered redirect URI, so the flow is: spin a
loopback HTTP server on exactly that host/port, send the user to the consent page,
catch the `?code=` callback, and trade it for a bearer token over HTTP Basic.

Two things this module deliberately does not do: it never writes to stdout (the CLI
owns that stream — pass `emit` to surface progress), and it never keeps the server
alive one request longer than it must.

The token TickTick returns is long-lived and there is **no refresh grant** in the
Open API, so `expires_at` is stored purely so the CLI can say "run auth again"
before a request fails.
"""

from __future__ import annotations

import base64
import html
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable

from . import config as _config
from .api import USER_AGENT, Client, resolve_base
from .errors import ApiError, AuthError, NetworkError, UsageError

AUTHORIZE_URL = "https://ticktick.com/oauth/authorize"
TOKEN_URL = "https://ticktick.com/oauth/token"
SCOPE = "tasks:read tasks:write"
DEFAULT_REDIRECT = "http://localhost:8080/callback"

#: How long we wait for the browser round-trip before giving up, in seconds.
CALLBACK_TIMEOUT = 300.0


def authorize(
    *,
    client_id: str,
    client_secret: str,
    redirect_uri: str = DEFAULT_REDIRECT,
    no_browser: bool = False,
    timeout: float = CALLBACK_TIMEOUT,
    emit: Callable[[str], None] | None = None,
) -> dict:
    """Run the whole flow and persist the result to the credential store.

    Returns a summary: ``{'redirect_uri', 'scope', 'expires_at', 'inbox_id',
    'inbox_error'}``. `inbox_id` is None when nothing named it — not fatal, and no
    longer a read-path dependency: it is only used to label the Inbox.

    Raises:
        UsageError: missing credentials, or a redirect URI we cannot bind.
        AuthError: TickTick refused the code or the credentials.
        NetworkError / ApiError: the token endpoint was unreachable or unparseable.
    """
    say = emit or (lambda _message: None)
    client_id = (client_id or "").strip()
    client_secret = (client_secret or "").strip()
    if not client_id or not client_secret:
        raise UsageError(
            "client id and secret are required — create an app at "
            "https://developer.ticktick.com/manage and pass --client-id/--client-secret"
        )

    host, port, path = _split_redirect(redirect_uri)
    state = secrets.token_urlsafe(24)
    url = f"{AUTHORIZE_URL}?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "scope": SCOPE,
            "state": state,
            "redirect_uri": redirect_uri,
            "response_type": "code",
        }
    )

    result: dict[str, str] = {}
    httpd = _serve(host, port, result, path=path, state=state)
    try:
        say(f"Listening on http://{host}:{port}{path}")
        if no_browser or not _open_browser(url):
            say("Open this URL in a browser to authorize:")
        else:
            say("Opened your browser. If nothing happened, use this URL:")
        say("")
        say(f"  {url}")
        say("")
        say("Waiting for the callback… (Ctrl-C to abort)")
        _wait(httpd, result, timeout)
    finally:
        httpd.server_close()

    if "error" in result:
        raise AuthError(result["error"])
    code = result.get("code", "")
    if not code:
        raise AuthError(f"no authorization code arrived within {int(timeout)}s")

    say("Exchanging the code for a token…")
    payload = _exchange(client_id, client_secret, code, redirect_uri)
    access_token = str(payload.get("access_token") or "")
    if not access_token:
        raise AuthError(f"token endpoint returned no access_token: {payload!r}")

    cfg = _config.load()
    cfg.update(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "access_token": access_token,
            "scope": str(payload.get("scope") or SCOPE),
        }
    )
    expires_at = _expiry(payload.get("expires_in"))
    if expires_at is not None:
        cfg["expires_at"] = expires_at
    else:
        cfg.pop("expires_at", None)
    _config.save(cfg)
    say(f"Token stored in {_config.CONFIG_PATH}")

    say("Looking up your Inbox id…")
    inbox_id, inbox_error = probe_inbox(Client(access_token, base=resolve_base(cfg)))
    if inbox_id:
        cfg["inbox_id"] = inbox_id
        _config.save(cfg)
        say(f"Inbox id: {inbox_id}")
    else:
        # Non-fatal by design: everything except "show Inbox tasks" works without it.
        say(f"Could not determine the Inbox id ({inbox_error}); continuing without it.")

    return {
        "redirect_uri": redirect_uri,
        "scope": cfg["scope"],
        "expires_at": expires_at,
        "inbox_id": inbox_id,
        "inbox_error": inbox_error,
    }


def login_with_token(token: str, *, emit: Callable[[str], None] | None = None) -> dict:
    """Store a personal API token after proving it actually works.

    TickTick issues these from the web app under **Settings → Account → API Token**,
    and they are ordinary bearer tokens — the same thing the OAuth dance produces,
    minus the dance. That makes them the shortest path to a working widget: no app
    registration, no client secret, no redirect URI to match character-for-character,
    no free port on localhost. `authorize` stays for people who prefer the browser
    flow, but nobody should have to visit a developer console to see their own todos.

    The token is validated with one cheap read before anything is written. Storing an
    unverified token would move the failure from this function — where it can be
    explained — to the next refresh, where it surfaces as an empty widget.

    Returns ``{'inbox_id', 'inbox_error', 'projects'}``.

    Raises:
        UsageError: the token is empty or obviously not a token.
        AuthError: TickTick rejected it.
        NetworkError / ApiError: it could not be checked at all.
    """
    say = emit or (lambda _message: None)
    token = (token or "").strip()
    if not token:
        raise UsageError(
            "no token given — copy one from TickTick: Settings → Account → API Token"
        )
    if len(token) < 10 or any(ch.isspace() for ch in token):
        # Catches the two usual pastes: a truncated selection, or the whole
        # "Bearer xxx" line. Both fail identically at the API with a bare 401.
        raise UsageError(
            "that does not look like an API token — paste just the token itself, "
            "with no 'Bearer ' prefix and no surrounding quotes"
        )

    say("Checking the token…")
    client = Client(token, base=resolve_base(_config.load()))
    try:
        projects = client.projects()
    except AuthError:
        raise AuthError(
            "TickTick rejected that token. Generate a fresh one at "
            "Settings → Account → API Token in the web app, and copy all of it."
        ) from None

    cfg = _config.load()
    cfg["access_token"] = token
    cfg["auth_method"] = "token"
    cfg.pop("adopt_external", None)  # an explicit sign-in clears an earlier sign-out
    # A personal token announces no lifetime, and a stale `expires_at` left over from
    # an earlier OAuth sign-in would make `config.is_expired` refuse a working token.
    cfg.pop("expires_at", None)
    _config.save(cfg)
    say(f"Token stored in {_config.CONFIG_PATH}")

    inbox_id, inbox_error = probe_inbox(client)
    if inbox_id:
        cfg["inbox_id"] = inbox_id
        _config.save(cfg)
    return {
        "inbox_id": inbox_id or "",
        "inbox_error": inbox_error,
        "projects": len(projects),
    }


def logout() -> bool:
    """Forget the stored token. Returns whether there was one to forget.

    Only the credentials are dropped; `inbox_id` and the project cache are harmless
    hints that cost a round-trip to relearn, and keeping them makes signing back in
    instant. Nothing here can leave a half-erased token behind: `config.save` is atomic.
    """
    cfg = _config.load()
    had = bool(cfg.get("access_token"))
    for key in ("access_token", "expires_at", "client_secret", "auth_method",
                "auth_source", "scope"):
        cfg.pop(key, None)
    # Otherwise `load()` would borrow the other tool's token straight back and the
    # user would appear to still be signed in immediately after signing out.
    cfg["adopt_external"] = False
    _config.save(cfg)
    return had


def probe_inbox(client: Client) -> tuple[str | None, str]:
    """Discover the Inbox project id by reading, never by writing.

    `GET /project` still omits the Inbox, but every Inbox task carries a real
    `projectId` of the form ``inbox<userId>``, so `/project/inbox/data` names it — and
    failing that, the one-call undone list does. The old version created a throwaway
    task in the user's real account and deleted it again; nothing is worth that, least
    of all a value reading no longer depends on.

    Returns ``(inbox_id, error_message)``. An empty Inbox has no id to find, which is a
    normal outcome, not a failure: store nothing and carry on.
    """
    try:
        pid = _inbox_id((client.inbox_data() or {}).get("tasks"))
        if not pid:
            pid = _inbox_id(client.all_undone())
    except Exception as exc:  # cosmetic: a lookup failure must never block sign-in
        return None, str(exc)
    if not pid:
        return None, "no Inbox task to read the id from"
    return pid, ""


def _inbox_id(tasks: Any) -> str:
    """The ``inbox<userId>`` project id carried by the first Inbox task, or ''."""
    for task in tasks if isinstance(tasks, list) else []:
        pid = str(task.get("projectId") or "") if isinstance(task, dict) else ""
        if pid.startswith("inbox"):
            return pid
    return ""


# --- callback server ---------------------------------------------------------


def _split_redirect(redirect_uri: str) -> tuple[str, int, str]:
    """Host, port and path to bind for `redirect_uri`."""
    parts = urllib.parse.urlsplit(redirect_uri or "")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise UsageError(
            f"redirect uri {redirect_uri!r} must look like http://localhost:8080/callback"
        )
    if parts.hostname not in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        raise UsageError(
            f"redirect uri {redirect_uri!r} points at {parts.hostname}, which this "
            "machine cannot answer for — use a loopback address"
        )
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        raise UsageError(f"redirect uri {redirect_uri!r} has an invalid port") from None
    return parts.hostname, port, parts.path or "/"


def _serve(host: str, port: int, result: dict, *, path: str, state: str) -> HTTPServer:
    """Bind the one-shot callback server, or explain exactly why we could not."""
    try:
        return HTTPServer((host, port), _handler(result, path=path, state=state))
    except OSError as exc:
        raise UsageError(
            f"cannot listen on {host}:{port} ({exc}). Something else is using that "
            f"port — stop it (`ss -ltnp 'sport = :{port}'`), or pass --redirect with a "
            "free port. The URI must match the one registered on your TickTick app."
        ) from None


def _wait(httpd: HTTPServer, result: dict, timeout: float) -> None:
    """Serve requests until the callback lands or `timeout` elapses.

    Not literally one `handle_request()`: browsers cheerfully ask for /favicon.ico
    first, and answering that with the callback's 404 would end the flow early.
    """
    httpd.timeout = 1.0  # so the deadline is checked even while nothing connects
    deadline = time.monotonic() + timeout
    while not result and time.monotonic() < deadline:
        httpd.handle_request()


def _handler(result: dict, *, path: str, state: str) -> type[BaseHTTPRequestHandler]:
    class CallbackHandler(BaseHTTPRequestHandler):
        server_version = "ticktick-auth"
        protocol_version = "HTTP/1.0"  # no keep-alive: we want the socket closed

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's spelling
            parts = urllib.parse.urlsplit(self.path)
            if parts.path != path:
                self._reply(404, _page("Not here", "This is not the callback URL.", False))
                return
            query = urllib.parse.parse_qs(parts.query)
            first = lambda key: (query.get(key) or [""])[0]  # noqa: E731

            if first("error"):
                detail = first("error_description") or first("error")
                result["error"] = f"TickTick refused authorization: {detail}"
                self._reply(400, _page("Authorization failed", detail, False))
                return
            if first("state") != state:
                # Either a stale tab or a forged callback; both mean "do not trust it".
                result["error"] = "state mismatch — the callback did not come from this run"
                self._reply(
                    400,
                    _page(
                        "State mismatch",
                        "The callback did not come from this sign-in attempt. "
                        "Close other TickTick sign-in tabs and run the command again.",
                        False,
                    ),
                )
                return
            code = first("code")
            if not code:
                result["error"] = "callback carried no authorization code"
                self._reply(400, _page("No code", "The callback carried no code.", False))
                return
            result["code"] = code
            self._reply(
                200,
                _page(
                    "Connected",
                    "TickTick is linked. You can close this tab and go back to the terminal.",
                    True,
                ),
            )

        def _reply(self, status: int, body: str) -> None:
            raw = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")  # the URL carries the code
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *args: Any) -> None:
            """Silence the default stderr access log; the CLI owns the console."""

    return CallbackHandler


def _page(heading: str, message: str, ok: bool) -> str:
    accent = "#4ade80" if ok else "#f87171"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(heading)} — TickTick</title>
<style>
  :root {{ color-scheme: dark light; }}
  body {{ margin:0; min-height:100vh; display:grid; place-items:center;
         background:#111318; color:#e6e8ee;
         font:16px/1.55 ui-sans-serif,system-ui,'Segoe UI',sans-serif; }}
  .card {{ max-width:34rem; padding:2.5rem; border-radius:14px; background:#181b22;
          border:1px solid #262b36; box-shadow:0 12px 40px rgba(0,0,0,.45); text-align:center; }}
  .dot {{ width:.7rem; height:.7rem; border-radius:50%; background:{accent};
         display:inline-block; margin-right:.55rem; vertical-align:middle; }}
  h1 {{ margin:0 0 .6rem; font-size:1.35rem; font-weight:600; }}
  p  {{ margin:0; color:#a9b0c0; }}
  @media (prefers-color-scheme: light) {{
    body {{ background:#f4f5f8; color:#1b1e26; }}
    .card {{ background:#fff; border-color:#e2e5ec; box-shadow:0 12px 40px rgba(0,0,0,.08); }}
    p {{ color:#5a6274; }}
  }}
</style></head>
<body><div class="card"><h1><span class="dot"></span>{html.escape(heading)}</h1>
<p>{html.escape(message)}</p></div></body></html>"""


# --- token exchange ----------------------------------------------------------


def _exchange(client_id: str, client_secret: str, code: str, redirect_uri: str) -> dict:
    """Trade the authorization code for a bearer token (HTTP Basic, form-encoded)."""
    body = urllib.parse.urlencode(
        {
            "code": code,
            "grant_type": "authorization_code",
            "scope": SCOPE,
            "redirect_uri": redirect_uri,
        }
    ).encode("utf-8")
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(
        TOKEN_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Basic {basic}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30.0) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace").strip()[:500]
        if exc.code in (400, 401, 403):
            raise AuthError(
                f"token exchange rejected (HTTP {exc.code}): {detail or 'no detail'}. "
                "Check the client id/secret and that the redirect uri matches exactly."
            ) from None
        raise ApiError(f"token exchange failed (HTTP {exc.code}): {detail}") from None
    except OSError as exc:
        raise NetworkError(f"token exchange could not reach {TOKEN_URL}: {exc}") from None

    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ApiError(f"token endpoint returned non-JSON ({exc})") from None
    if not isinstance(payload, dict):
        raise ApiError(f"token endpoint returned {type(payload).__name__}, expected an object")
    return payload


def _expiry(expires_in: Any) -> float | None:
    """Absolute epoch expiry for a relative `expires_in`, or None when absent."""
    try:
        seconds = float(expires_in)
    except (TypeError, ValueError):
        return None
    return round(time.time() + seconds, 3) if seconds > 0 else None


def _open_browser(url: str) -> bool:
    """Best-effort browser launch; False means the user has to click it themselves."""
    try:
        return webbrowser.open(url)
    except Exception:
        return False


__all__ = [
    "AUTHORIZE_URL",
    "TOKEN_URL",
    "SCOPE",
    "DEFAULT_REDIRECT",
    "CALLBACK_TIMEOUT",
    "authorize",
    "login_with_token",
    "logout",
    "probe_inbox",
]
