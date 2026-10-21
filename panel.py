"""Web-Panel für den Duality-Bot.

Läuft im selben Prozess und in derselben Event-Loop wie der Bot. main.py startet
es über start_panel() und übergibt in PanelHooks alles, was das Panel braucht;
panel.py importiert main.py nicht.
"""

import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import secrets
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode, urlparse

import aiohttp
import discord
from aiohttp import web


log = logging.getLogger("duality.panel")

STATIC_DIR = Path(__file__).with_name("panel_static")
DISCORD_API = "https://discord.com/api/v10"
DISCORD_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
SESSION_COOKIE = "duality_session"
STATE_COOKIE = "duality_oauth_state"
SESSION_SECONDS = 12 * 60 * 60
STATE_SECONDS = 10 * 60
MIN_SECRET_KEY_LENGTH = 32
LOGIN_ATTEMPTS = 10
LOGIN_WINDOW_SECONDS = 5 * 60
# Rechte-Stufen, wie PanelHooks.staff_level sie liefert (0 = kein Staff).
COACH, ADMIN, OWNER = 1, 2, 3
LEVEL_NAMES = {COACH: "Coach", ADMIN: "Admin", OWNER: "Owner"}
LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")


@dataclass(frozen=True)
class PanelConfig:
    client_id: str
    client_secret: str
    base_url: str
    secret_key: bytes
    host: str = "127.0.0.1"
    port: int = 8080

    @property
    def origin(self) -> str:
        parsed = urlparse(self.base_url)
        return f"{parsed.scheme}://{parsed.netloc}"

    @property
    def secure_cookies(self) -> bool:
        # Ohne HTTPS (lokal oder per SSH-Tunnel) schickt der Browser Secure-Cookies nicht zurück.
        return self.base_url.startswith("https://")

    @property
    def redirect_uri(self) -> str:
        return self.base_url + "/auth/callback"


def load_config(environ=os.environ) -> tuple[PanelConfig | None, str | None]:
    """Liest die Panel-Einstellungen. Bei fehlenden oder ungültigen Werten: (None, Grund)."""
    required = ("DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "PANEL_BASE_URL", "PANEL_SECRET_KEY")
    missing = [name for name in required if not environ.get(name, "").strip()]
    if missing:
        return None, "Es fehlen " + ", ".join(missing) + "."
    base_url = environ["PANEL_BASE_URL"].strip().rstrip("/")
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.path or parsed.query:
        return None, "PANEL_BASE_URL muss eine Adresse ohne Pfad sein, z. B. https://panel.example.de."
    if parsed.scheme == "http" and parsed.hostname not in LOCAL_HOSTS:
        return None, "PANEL_BASE_URL braucht HTTPS, außer für localhost."
    secret_key = environ["PANEL_SECRET_KEY"].strip()
    if len(secret_key) < MIN_SECRET_KEY_LENGTH:
        return None, f"PANEL_SECRET_KEY muss mindestens {MIN_SECRET_KEY_LENGTH} Zeichen lang sein."
    try:
        port = int(environ.get("PANEL_PORT", "8080"))
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        return None, "PANEL_PORT muss eine Zahl zwischen 1 und 65535 sein."
    return PanelConfig(
        client_id=environ["DISCORD_CLIENT_ID"].strip(),
        client_secret=environ["DISCORD_CLIENT_SECRET"].strip(),
        base_url=base_url,
        secret_key=secret_key.encode(),
        host=environ.get("PANEL_HOST", "").strip() or "127.0.0.1",
        port=port,
    ), None


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _b64decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _signature(body: str, key: bytes) -> str:
    return _b64encode(hmac.new(key, body.encode(), hashlib.sha256).digest())


def sign(payload: dict, key: bytes) -> str:
    body = _b64encode(json.dumps(payload, separators=(",", ":")).encode())
    return f"{body}.{_signature(body, key)}"


def unsign(token: str | None, key: bytes, purpose: str, now: float | None = None) -> dict | None:
    """Prüft Signatur, Zweck und Ablauf eines signierten Werts; sonst None."""
    if not token or token.count(".") != 1:
        return None
    body, signature = token.split(".")
    if not hmac.compare_digest(signature.encode(), _signature(body, key).encode()):
        return None
    try:
        payload = json.loads(_b64decode(body))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("use") != purpose:
        return None
    expires = payload.get("exp")
    if not isinstance(expires, (int, float)) or expires <= (time.time() if now is None else now):
        return None
    return payload


def new_session(user_id: int, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    return {
        "use": "session",
        "uid": user_id,
        "sid": secrets.token_urlsafe(16),
        "csrf": secrets.token_urlsafe(32),
        "exp": int(now + SESSION_SECONDS),
    }


def csrf_valid(session: dict, origin: str, request_origin: str | None, token: str | None) -> bool:
    """Gültig nur, wenn die Anfrage von der Panel-Seite selbst kommt und das Token der Session trägt."""
    if request_origin != origin or not token:
        return False
    return hmac.compare_digest(token.encode(), str(session.get("csrf", "")).encode())


class RateLimiter:
    def __init__(self, limit: int, window: float):
        self.limit = limit
        self.window = window
        self.hits: dict[object, deque[float]] = {}

    def allow(self, key, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        if len(self.hits) > 1000:
            self.hits = {name: hits for name, hits in self.hits.items() if hits and hits[-1] > now - self.window}
        hits = self.hits.setdefault(key, deque())
        while hits and hits[0] <= now - self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return False
        hits.append(now)
        return True


def client_ip(request: web.Request) -> str:
    # Hinter dem Reverse-Proxy kommt jede Anfrage von 127.0.0.1; dann zählt die Adresse, die der Proxy einträgt.
    remote = request.remote or ""
    forwarded = request.headers.get("X-Forwarded-For", "")
    if remote in LOCAL_HOSTS and forwarded:
        return forwarded.split(",")[-1].strip()
    return remote


def json_error(status: int, message: str) -> web.HTTPException:
    error_class = {
        400: web.HTTPBadRequest,
        401: web.HTTPUnauthorized,
        403: web.HTTPForbidden,
        429: web.HTTPTooManyRequests,
        503: web.HTTPServiceUnavailable,
    }[status]
    return error_class(text=json.dumps({"error": message}), content_type="application/json")


def page(title: str, body: str, status: int = 200) -> web.Response:
    document = (
        '<!doctype html><html lang="de"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{html.escape(title)} · Duality-Panel</title>"
        '<link rel="stylesheet" href="/static/panel.css"></head>'
        '<body class="center-page"><main class="card narrow">'
        '<div class="brand"><span class="brand-mark" aria-hidden="true"></span>Duality-Panel</div>'
        f"<h1>{html.escape(title)}</h1>{body}</main></body></html>"
    )
    return web.Response(text=document, content_type="text/html", status=status, headers={"Cache-Control": "no-store"})


LOGIN_LINK = '<p><a class="button primary" href="/auth/login">Mit Discord anmelden</a></p>'


@dataclass
class PanelHooks:
    """Was das Panel vom Bot braucht. main.py füllt es aus, damit panel.py main.py nicht importiert."""

    client: discord.Client
    guild_id: int
    # 0 = kein Staff, sonst COACH, ADMIN oder OWNER.
    staff_level: Callable[[discord.Member], int]
    audit: Callable[[discord.Guild, str, discord.Color], Awaitable[None]]


class Panel:
    def __init__(self, config: PanelConfig, hooks: PanelHooks):
        self.config = config
        self.hooks = hooks
        self.revoked: dict[str, float] = {}
        self.login_limiter = RateLimiter(LOGIN_ATTEMPTS, LOGIN_WINDOW_SECONDS)
        self.http: aiohttp.ClientSession | None = None
        ws_origin = config.origin.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
        self.csp = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data: https://cdn.discordapp.com https://media.discordapp.net; "
            f"connect-src 'self' {ws_origin}; frame-ancestors 'none'; base-uri 'none'; "
            "form-action 'self'; object-src 'none'"
        )

    def build_app(self) -> web.Application:
        app = web.Application(middlewares=[self.require_csrf], client_max_size=256 * 1024)
        app.add_routes([
            web.get("/", self.index),
            web.get("/healthz", self.healthz),
            web.get("/auth/login", self.login),
            web.get("/auth/callback", self.callback),
            web.post("/auth/logout", self.logout),
            web.get("/api/me", self.api_me),
        ])
        app.router.add_static("/static/", STATIC_DIR)
        app.on_response_prepare.append(self.add_security_headers)
        app.on_cleanup.append(self.close)
        return app

    async def add_security_headers(self, request: web.Request, response: web.StreamResponse) -> None:
        response.headers["Content-Security-Policy"] = self.csp
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        if self.config.secure_cookies:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"

    @web.middleware
    async def require_csrf(self, request: web.Request, handler):
        # Gilt für jede ändernde Anfrage, auch für künftige Endpunkte.
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            session = self.session(request)
            if session is None:
                raise json_error(401, "Bitte melde dich an.")
            if not csrf_valid(session, self.config.origin, request.headers.get("Origin"), request.headers.get("X-CSRF-Token")):
                log.warning("Panel: %s %s ohne gültiges CSRF-Token abgelehnt", request.method, request.path)
                raise json_error(403, "Ungültige Anfrage. Bitte lade die Seite neu.")
        return await handler(request)

    async def close(self, app: web.Application | None = None) -> None:
        if self.http and not self.http.closed:
            await self.http.close()

    def http_session(self) -> aiohttp.ClientSession:
        if self.http is None or self.http.closed:
            self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self.http

    def guild(self) -> discord.Guild | None:
        return self.hooks.client.get_guild(self.hooks.guild_id)

    def session(self, request: web.Request) -> dict | None:
        session = unsign(request.cookies.get(SESSION_COOKIE), self.config.secret_key, "session")
        if session is None or session.get("sid") in self.revoked:
            return None
        return session

    def staff(self, user_id: int) -> tuple[discord.Member, int] | None:
        """Prüft live im Server, ob die Person Staff ist; ein Rollenentzug wirkt so sofort."""
        guild = self.guild()
        member = guild.get_member(int(user_id)) if guild else None
        level = self.hooks.staff_level(member) if member else 0
        return (member, level) if level else None

    def authenticate(self, request: web.Request, minimum: int = COACH) -> tuple[dict, discord.Member, int]:
        session = self.session(request)
        if session is None:
            raise json_error(401, "Bitte melde dich an.")
        if self.guild() is None:
            raise json_error(503, "Der Bot ist gerade nicht mit Discord verbunden.")
        staff = self.staff(session["uid"])
        if staff is None:
            raise json_error(403, "Du hast keinen Zugriff mehr auf das Panel.")
        member, level = staff
        if level < minimum:
            raise json_error(403, f"Dafür brauchst du mindestens die Rolle {LEVEL_NAMES[minimum]}.")
        return session, member, level

    def revoke(self, session: dict) -> None:
        now = time.time()
        self.revoked = {sid: expires for sid, expires in self.revoked.items() if expires > now}
        self.revoked[session["sid"]] = session["exp"]

    def set_cookie(self, response: web.StreamResponse, name: str, value: str, max_age: int, path: str = "/") -> None:
        response.set_cookie(
            name, value, max_age=max_age, path=path, httponly=True, secure=self.config.secure_cookies, samesite="Lax"
        )

    async def index(self, request: web.Request) -> web.StreamResponse:
        session = self.session(request)
        if session is None:
            return page("Anmelden", "<p>Das Panel ist nur für den Staff von Duality Mannheim.</p>" + LOGIN_LINK)
        if self.guild() is None:
            return page("Bot nicht verbunden", "<p>Der Bot verbindet sich gerade mit Discord. Lade die Seite gleich neu.</p>", 503)
        if self.staff(session["uid"]) is None:
            response = page(
                "Kein Zugriff",
                "<p>Du hast keine Staff-Rolle (Owner, Admin oder Coach) mehr und wurdest abgemeldet.</p>" + LOGIN_LINK,
                403,
            )
            response.del_cookie(SESSION_COOKIE, path="/")
            return response
        return web.FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    async def healthz(self, request: web.Request) -> web.Response:
        ready = self.hooks.client.is_ready() and self.guild() is not None
        return web.json_response({"ok": ready}, status=200 if ready else 503)

    async def login(self, request: web.Request) -> web.Response:
        if not self.login_limiter.allow(client_ip(request)):
            return page("Zu viele Versuche", "<p>Bitte warte ein paar Minuten und versuche es dann erneut.</p>", 429)
        state = secrets.token_urlsafe(24)
        query = urlencode({
            "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri,
            "response_type": "code",
            "scope": "identify",
            "state": state,
            "prompt": "none",
        })
        response = web.Response(status=302, headers={"Location": f"{DISCORD_AUTHORIZE_URL}?{query}"})
        state_token = sign({"use": "state", "state": state, "exp": int(time.time() + STATE_SECONDS)}, self.config.secret_key)
        self.set_cookie(response, STATE_COOKIE, state_token, STATE_SECONDS, path="/auth")
        return response

    async def callback(self, request: web.Request) -> web.Response:
        if not self.login_limiter.allow(client_ip(request)):
            return page("Zu viele Versuche", "<p>Bitte warte ein paar Minuten und versuche es dann erneut.</p>", 429)
        stored = unsign(request.cookies.get(STATE_COOKIE), self.config.secret_key, "state")
        state = request.query.get("state", "")
        if stored is None or not hmac.compare_digest(state.encode(), str(stored.get("state", "")).encode()):
            log.warning("Panel-Login abgelehnt: ungültiger oder abgelaufener state-Parameter")
            return page(
                "Anmeldung abgelaufen",
                "<p>Die Anmeldung ist abgelaufen oder wurde nicht hier gestartet. Bitte starte sie neu.</p>" + LOGIN_LINK,
                400,
            )
        code = request.query.get("code")
        if not code:
            return page("Anmeldung abgebrochen", "<p>Discord hat keine Anmeldung bestätigt.</p>" + LOGIN_LINK, 400)
        try:
            user_id = await self.fetch_user_id(code)
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, TypeError, ValueError) as error:
            log.warning("Panel-Login: Discord-Anmeldung fehlgeschlagen (%s)", type(error).__name__)
            return page("Anmeldung fehlgeschlagen", "<p>Discord hat die Anmeldung nicht bestätigt. Bitte versuche es erneut.</p>" + LOGIN_LINK, 502)
        if self.guild() is None:
            return page("Bot nicht verbunden", "<p>Der Bot verbindet sich gerade mit Discord. Bitte versuche es gleich erneut.</p>", 503)
        staff = self.staff(user_id)
        if staff is None:
            log.warning("Panel-Login abgelehnt: %s hat keine Staff-Rolle", user_id)
            response = page("Kein Zugriff", "<p>Das Panel ist nur für Owner, Admins und Coaches von Duality Mannheim.</p>", 403)
            response.del_cookie(STATE_COOKIE, path="/auth")
            return response
        member, level = staff
        response = web.Response(status=302, headers={"Location": "/"})
        self.set_cookie(response, SESSION_COOKIE, sign(new_session(user_id), self.config.secret_key), SESSION_SECONDS)
        response.del_cookie(STATE_COOKIE, path="/auth")
        log.info("Panel-Login: %s (%s, %s)", member, member.id, LEVEL_NAMES[level])
        return response

    async def fetch_user_id(self, code: str) -> int:
        http = self.http_session()
        credentials = {"client_id": self.config.client_id, "client_secret": self.config.client_secret}
        async with http.post(
            f"{DISCORD_API}/oauth2/token",
            data={**credentials, "grant_type": "authorization_code", "code": code, "redirect_uri": self.config.redirect_uri},
        ) as response:
            response.raise_for_status()
            token = (await response.json())["access_token"]
        try:
            async with http.get(f"{DISCORD_API}/users/@me", headers={"Authorization": f"Bearer {token}"}) as response:
                response.raise_for_status()
                return int((await response.json())["id"])
        finally:
            # Der Discord-Token wird nur für diese eine Abfrage gebraucht und danach widerrufen.
            try:
                async with http.post(
                    f"{DISCORD_API}/oauth2/token/revoke",
                    data={**credentials, "token": token, "token_type_hint": "access_token"},
                ):
                    pass
            except (aiohttp.ClientError, asyncio.TimeoutError):
                log.warning("Panel-Login: Discord-Token konnte nicht widerrufen werden")

    async def logout(self, request: web.Request) -> web.Response:
        session = self.session(request)
        if session:
            self.revoke(session)
        response = web.json_response({"ok": True})
        response.del_cookie(SESSION_COOKIE, path="/")
        return response

    async def api_me(self, request: web.Request) -> web.Response:
        session, member, level = self.authenticate(request)
        return web.json_response({
            "id": str(member.id),
            "name": member.display_name,
            "avatar": member.display_avatar.url,
            "level": level,
            "role": LEVEL_NAMES[level],
            "csrf": session["csrf"],
        })


async def start_panel(hooks: PanelHooks) -> web.AppRunner | None:
    """Startet das Panel. Ohne gültige Konfiguration läuft der Bot ohne Panel weiter."""
    config, problem = load_config()
    if config is None:
        print(f"Web-Panel ist aktiviert, aber nicht vollständig eingerichtet: {problem} Der Bot läuft ohne Panel.", flush=True)
        return None
    panel = Panel(config, hooks)
    runner = web.AppRunner(panel.build_app(), access_log=None, shutdown_timeout=5)
    await runner.setup()
    try:
        await web.TCPSite(runner, config.host, config.port).start()
    except OSError as error:
        await runner.cleanup()
        print(
            f"Web-Panel konnte nicht auf {config.host}:{config.port} starten ({error.strerror or error}). "
            "Der Bot läuft ohne Panel.",
            flush=True,
        )
        return None
    print(f"Web-Panel läuft auf {config.host}:{config.port}, erreichbar unter {config.base_url}.", flush=True)
    return runner
