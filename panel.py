"""Web-Panel für den Duality-Bot.

Läuft im selben Prozess und in derselben Event-Loop wie der Bot. main.py startet
es über start_panel() und übergibt in PanelHooks alles, was das Panel braucht;
panel.py importiert main.py nicht.
"""

import asyncio
import base64
import csv
import hashlib
import hmac
import html
import io
import itertools
import json
import logging
import math
import os
import platform
import re
import secrets
import sqlite3
import sys
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

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
PANEL_COLOR = discord.Color.from_rgb(110, 70, 255)
BERLIN = ZoneInfo("Europe/Berlin")
LOG_BUFFER_SIZE = 1000
LOG_MESSAGE_LIMIT = 4000
WS_QUEUE_SIZE = 2000
WS_CHECK_SECONDS = 5
COMMANDS_PER_MINUTE = 20
# Schließcode, wenn der Zugriff weg ist: Das Frontend lädt dann neu, statt sich wieder zu verbinden.
WS_CLOSE_NO_ACCESS = 4403
# Was nach Token oder Secret aussieht, kommt nie in die Konsole.
REDACTIONS = (
    (re.compile(r"(discord(?:app)?\.com/api/(?:v\d+/)?webhooks/\d+/)[\w-]+"), r"\1***"),
    (re.compile(r"[\w-]{23,}\.[\w-]{6,7}\.[\w-]{27,}"), "***"),
    (re.compile(r"(Bearer\s+)\S+", re.IGNORECASE), r"\1***"),
    (re.compile(r"HDEV-[\w-]+"), "HDEV-***"),
    (re.compile(r"(Session ID: )[0-9a-f]+"), r"\1***"),
)


def redact(text: str) -> str:
    for pattern, replacement in REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


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


class ConsoleLog:
    """Ringpuffer der letzten Logeinträge; neue Einträge gehen sofort an alle offenen Konsolen."""

    def __init__(self, size: int = LOG_BUFFER_SIZE):
        self.entries: deque[dict] = deque(maxlen=size)
        self.listeners: list[Callable[[dict], None]] = []
        self.loop: asyncio.AbstractEventLoop | None = None
        self.ids = itertools.count(1)
        self.lock = threading.Lock()

    def add(self, level: str, name: str, message: str, created: float | None = None) -> None:
        # discord.py loggt teils aus eigenen Threads, deshalb Lock und call_soon_threadsafe.
        with self.lock:
            entry = {
                "id": next(self.ids),
                "time": created or time.time(),
                "level": level,
                "name": name,
                "message": redact(message)[:LOG_MESSAGE_LIMIT],
            }
            self.entries.append(entry)
        if self.loop and not self.loop.is_closed():
            try:
                self.loop.call_soon_threadsafe(self.notify, entry)
            except RuntimeError:
                pass

    def notify(self, entry: dict) -> None:
        for listener in list(self.listeners):
            listener(entry)

    def snapshot(self) -> list[dict]:
        with self.lock:
            return list(self.entries)


class ConsoleLogHandler(logging.Handler):
    def __init__(self, console: ConsoleLog):
        super().__init__(logging.DEBUG)
        self.console = console

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            if record.exc_info and record.exc_info[1] is not None:
                # Nur Art und Text des Fehlers; der vollständige Stacktrace bleibt im Journal.
                error = record.exc_info[1]
                message += f" ({type(error).__name__}: {error})"
            self.console.add(record.levelname, record.name, message, record.created)
        except Exception:
            self.handleError(record)


class ConsoleStdout:
    """Gibt print()-Ausgaben unverändert ans Terminal weiter und schreibt sie zusätzlich in die Konsole."""

    def __init__(self, stream, console: ConsoleLog):
        self.stream = stream
        self.console = console
        self.pending = ""

    def write(self, text: str) -> int:
        written = self.stream.write(text)
        self.pending += text
        *lines, self.pending = self.pending.split("\n")
        if len(self.pending) > LOG_MESSAGE_LIMIT:
            lines.append(self.pending)
            self.pending = ""
        for line in lines:
            if line.strip():
                self.console.add("INFO", "ausgabe", line)
        return written

    def flush(self) -> None:
        self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


CAPTURED_LOGGERS = ("duality", "discord")


def parse_command_line(line: str) -> tuple[str, str]:
    """„say #kanal Hallo“ → („say“, „#kanal Hallo“)."""
    parts = line.strip().split(maxsplit=1)
    if not parts:
        return "", ""
    return parts[0].casefold(), parts[1].strip() if len(parts) > 1 else ""


def parse_user_id(text: str) -> int | None:
    match = re.fullmatch(r"<@!?(\d{15,20})>|(\d{15,20})", text.strip())
    return int(match.group(1) or match.group(2)) if match else None


def find_channel(guild: discord.Guild, text: str):
    """Textkanal per Erwähnung (<#id>), ID oder Name (mit oder ohne #)."""
    text = text.strip()
    match = re.fullmatch(r"<#(\d{15,20})>|(\d{15,20})", text)
    if match:
        channel = guild.get_channel(int(match.group(1) or match.group(2)))
        return channel if channel in guild.text_channels else None
    name = text.removeprefix("#").casefold()
    return discord.utils.find(lambda channel: channel.name.casefold() == name, guild.text_channels)


def format_time(timestamp: float | None) -> str:
    if not timestamp:
        return "–"
    return datetime.fromtimestamp(timestamp, BERLIN).strftime("%d.%m.%Y %H:%M")


def format_duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days} d {hours} h"
    if hours:
        return f"{hours} h {minutes} min"
    return f"{minutes} min"


def setting_text(value) -> str:
    if isinstance(value, bool):
        return "an" if value else "aus"
    if isinstance(value, (list, tuple)):
        return ", ".join(str(item) for item in value) + " h"
    return str(value)


def spreadsheet_safe(value: str) -> str:
    """Verhindert, dass Tabellenprogramme Namen wie „=HYPERLINK(…)“ als Formel ausführen."""
    return f"'{value}" if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


class CommandError(Exception):
    """Fehler mit einer Meldung, die so in der Konsole angezeigt werden darf."""


@dataclass(frozen=True)
class CommandContext:
    panel: "Panel"
    guild: discord.Guild
    member: discord.Member
    level: int


@dataclass(frozen=True)
class ConsoleCommand:
    name: str
    usage: str
    description: str
    level: int
    run: Callable[[CommandContext, str], Awaitable[str]]
    # Text für den Bestätigungsdialog; ohne Bestätigung lehnt der Server den Befehl ab.
    confirm: str | None = None
    # Steuernde Befehle landen im Audit-Log.
    controlling: bool = True


async def command_help(context: CommandContext, arguments: str) -> str:
    return "\n".join(
        f"{command.usage} – {command.description}"
        for command in COMMANDS.values()
        if context.level >= command.level
    )


async def command_status(context: CommandContext, arguments: str) -> str:
    status = context.panel.status()
    bot, guild, api = status["bot"], status["guild"], status["henrikdev"]
    latency = f"{bot['latency_ms']} ms" if bot["latency_ms"] is not None else "–"
    lines = [
        f"Bot: {'verbunden' if bot['connected'] else 'getrennt'} als {bot['user'] or '–'} · Latenz {latency} · "
        f"läuft seit {format_duration(status['time'] - bot['started_at'])}",
        f"Version: Python {bot['python']} · discord.py {bot['discord_py']} · Commit {bot['commit']}",
    ]
    if guild:
        lines.append(
            f"Server: {guild['name']} · {guild['members']} Mitglieder · {guild['players']} Spieler · "
            f"{guild['applicants']} Bewerber · {guild['staff']} Staff"
        )
    if not api["enabled"]:
        lines.append("HenrikDev: aus (HENRIKDEV_API_KEY fehlt)")
    else:
        error = f"{api['last_error']} ({format_time(api['last_error_at'])})" if api["last_error"] else "keiner"
        limit = f" · Rate-Limit bis {format_time(api['rate_limited_until'])}" if api["rate_limited"] else ""
        lines.append(f"HenrikDev: aktiv · letzter Fehler: {error}{limit}")
    stopped = [task["label"] for task in status["tasks"] if not task["running"]]
    if stopped:
        lines.append("Gestoppte Tasks: " + ", ".join(stopped))
    problems = status["alerts"]["active"]
    lines.append(
        "Aktive Warnungen: " + "; ".join(problem["text"] for problem in problems) if problems else "Aktive Warnungen: keine"
    )
    return "\n".join(lines)


async def command_tasks(context: CommandContext, arguments: str) -> str:
    lines = []
    for task in context.panel.status()["tasks"]:
        error = f"{task['last_error']} ({format_time(task['last_error_at'])})" if task["last_error"] else "–"
        lines.append(
            f"{task['label']}: {'läuft' if task['running'] else 'GESTOPPT'} · letzter Lauf {format_time(task['last_run'])} · "
            f"nächster {format_time(task['next_run'])} · letzter Fehler: {error}"
        )
    return "\n".join(lines) or "Keine Hintergrund-Tasks registriert."


async def command_sync_ranks(context: CommandContext, arguments: str) -> str:
    if not arguments:
        return await context.panel.hooks.sync_ranks(context.guild, None)
    user_id = parse_user_id(arguments)
    if user_id is None:
        raise CommandError("Bitte gib @Person oder eine User-ID an, z. B. „sync-ranks 123456789012345678“.")
    member = context.guild.get_member(user_id)
    if member is None:
        raise CommandError("Diese Person ist nicht auf dem Server.")
    return await context.panel.hooks.sync_ranks(context.guild, member)


async def command_reminders(context: CommandContext, arguments: str) -> str:
    sent = await context.panel.hooks.send_reminders()
    return f"Termin-Erinnerungen geprüft, {sent} gesendet."


async def command_purge(context: CommandContext, arguments: str) -> str:
    events, applications = await context.panel.hooks.purge(context.guild)
    return f"Löschfristen angewendet: {events} vergangene Termine und {applications} Bewerbungen gelöscht."


async def command_cleanup_voice(context: CommandContext, arguments: str) -> str:
    removed = await context.panel.hooks.cleanup_voice(context.guild)
    return f"{removed} leere temporäre Voice-Kanäle gelöscht."


async def command_say(context: CommandContext, arguments: str) -> str:
    parts = arguments.split(maxsplit=1)
    channel = find_channel(context.guild, parts[0]) if parts else None
    if channel is None:
        raise CommandError("Kanal nicht gefunden. Beispiel: say #allgemein Hallo zusammen")
    text = parts[1].strip() if len(parts) > 1 else ""
    if not 1 <= len(text) <= 2000:
        raise CommandError("Der Text muss 1 bis 2000 Zeichen lang sein.")
    permissions = channel.permissions_for(context.guild.me)
    if not (permissions.view_channel and permissions.send_messages):
        raise CommandError(f"Der Bot darf in #{channel.name} nicht schreiben.")
    await channel.send(text, allowed_mentions=discord.AllowedMentions.none())
    return f"Nachricht in #{channel.name} gesendet."


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB".replace(".", ",")
    return f"{size / 1024 / 1024:.1f} MB".replace(".", ",")


async def command_backup(context: CommandContext, arguments: str) -> str:
    try:
        backup = await context.panel.hooks.run_backup()
    except (OSError, sqlite3.Error) as error:
        raise CommandError(f"Das Backup ist fehlgeschlagen ({type(error).__name__}). Details stehen im Log.") from error
    return f"Backup erstellt: {backup['name']} ({format_size(backup['size'])}), {backup['removed']} alte Backups gelöscht."


async def command_restart(context: CommandContext, arguments: str) -> str:
    context.panel.spawn(context.panel.restart_soon())
    return "Der Bot startet neu. Die Konsole verbindet sich danach automatisch wieder."


COMMANDS = {
    command.name: command
    for command in (
        ConsoleCommand("help", "help", "listet die Befehle, die du nutzen darfst", COACH, command_help, controlling=False),
        ConsoleCommand("status", "status", "Kurzstatus wie im Dashboard", COACH, command_status, controlling=False),
        ConsoleCommand("tasks", "tasks", "Status aller Hintergrund-Tasks", COACH, command_tasks, controlling=False),
        ConsoleCommand(
            "sync-ranks", "sync-ranks [@user|user_id]", "Rang-Abgleich sofort, für alle oder eine Person",
            ADMIN, command_sync_ranks,
        ),
        ConsoleCommand("reminders", "reminders", "Termin-Erinnerungen sofort prüfen", ADMIN, command_reminders),
        ConsoleCommand("purge", "purge", "Löschfristen für Termine und Bewerbungen sofort anwenden", ADMIN, command_purge),
        ConsoleCommand("cleanup-voice", "cleanup-voice", "leere temporäre Voice-Kanäle löschen", ADMIN, command_cleanup_voice),
        ConsoleCommand("backup", "backup", "sofort ein Datenbank-Backup erstellen", ADMIN, command_backup),
        ConsoleCommand(
            "say", "say #kanal <text>", "Nachricht als Bot senden (ohne Pings)", ADMIN, command_say,
            confirm="Der Text wird als Bot-Nachricht im angegebenen Kanal veröffentlicht. Erwähnungen pingen niemanden.",
        ),
        ConsoleCommand(
            "restart", "restart", "Bot sauber beenden; systemd startet ihn neu", OWNER, command_restart,
            confirm="Der Bot beendet sich und wird von systemd neu gestartet. Er ist dabei kurz offline, "
            "laufende Befehle brechen ab.",
        ),
    )
}


class Connection:
    """Eine offene Konsole. Nachrichten laufen über eine Warteschlange, damit langsame Browser den Bot nicht bremsen."""

    def __init__(self, ws: web.WebSocketResponse, session: dict):
        self.ws = ws
        self.user_id = int(session["uid"])
        self.sid = session["sid"]
        self.expires = session["exp"]
        self.queue: asyncio.Queue[str] = asyncio.Queue(maxsize=WS_QUEUE_SIZE)

    def send(self, message: dict) -> None:
        try:
            self.queue.put_nowait(json.dumps(message, ensure_ascii=False))
        except asyncio.QueueFull:
            pass

    async def write(self) -> None:
        while True:
            data = await self.queue.get()
            await self.ws.send_str(data)


@dataclass
class PanelHooks:
    """Was das Panel vom Bot braucht. main.py füllt es aus, damit panel.py main.py nicht importiert."""

    client: discord.Client
    guild_id: int
    # 0 = kein Staff, sonst COACH, ADMIN oder OWNER.
    staff_level: Callable[[discord.Member], int]
    audit: Callable[[discord.Guild, str, discord.Color], Awaitable[None]]
    # Konsolenbefehle; Rückgabe ist die Meldung für die Konsole bzw. eine Anzahl.
    sync_ranks: Callable[[discord.Guild, discord.Member | None], Awaitable[str]]
    send_reminders: Callable[[], Awaitable[int]]
    purge: Callable[[discord.Guild], Awaitable[tuple[int, int]]]
    cleanup_voice: Callable[[discord.Guild], Awaitable[int]]
    restart: Callable[[], Awaitable[None]]
    # Dashboard
    started_at: float
    gateway: dict
    # Name → (Anzeigename, tasks.Loop)
    loops: dict[str, tuple[str, Any]]
    loop_status: dict[str, dict]
    rank_client: Callable[[], Any]
    find_role: Callable[[discord.Guild, str], discord.Role | None]
    staff_members: Callable[[discord.Guild], list[discord.Member]]
    # main.Alerts: aktive Probleme und die letzten Meldungen
    alerts: Any
    # Backups: erstellen, auflisten (neueste zuerst, mit name/path/size/created), letzter Fehler
    run_backup: Callable[[], Awaitable[dict]]
    list_backups: Callable[[], list[dict]]
    backup_state: dict
    # Live-Ansicht: main.py ruft jeden Eintrag mit einem Thema auf (z. B. „voice“).
    listeners: list[Callable[[str], None]]
    temp_voice_owners: Callable[[int], dict[int, int]]
    # Bewerbungen: nur Status, IDs und Zeiten; die Inhalte bleiben in der Discord-Nachricht.
    find_text_channel: Callable[[discord.Guild, str], discord.TextChannel | None]
    list_applications: Callable[[int], list]
    application_vote_counts: Callable[[int], dict[int, tuple[int, int]]]
    application_labels: dict[str, str]
    reopen_applications: Callable[[discord.Guild], Awaitable[int | None]]
    # Spieler-Profile (main.Profile)
    list_profiles: Callable[[int], list]
    # Tickets: (Thread, meldende Person, erstellt, geschlossen); Inhalte bleiben im Thread.
    list_tickets: Callable[[int], list[tuple[int, int, int, int | None]]]
    # Termine: dieselben Funktionen wie /termin; Fehler kommen als ValueError mit Meldung für die Person.
    list_events: Callable[[int], list]
    event_rsvps: Callable[[int], dict[int, dict[str, list[int]]]]
    event_reminders: Callable[[int], dict[int, dict[str, int]]]
    validate_event: Callable[[str, datetime, str], tuple[str, str, int]]
    publish_event: Callable[[discord.Guild, str, str, int], Awaitable[discord.Message]]
    edit_event: Callable[..., Awaitable[str]]
    cancel_event: Callable[..., Awaitable[str]]
    # Rangliste wie /rangliste: (Mitglied, Profil, Name); Verlauf je Person; Rang → vergleichbarer Wert
    rank_leaderboard: Callable[[discord.Guild], list[tuple[discord.Member, Any, str]]]
    rank_histories: Callable[[int], dict[int, list[tuple[str, int]]]]
    rank_value: Callable[[str | None], tuple[int, int] | None]
    # Anwesenheit: Termine + Antworten im Zeitraum, Auswertung je Person, Schwellen und Löschfrist
    attendance_events: Callable[[int, int, int], tuple[list, dict[int, dict[int, str]]]]
    summarize_attendance: Callable[[list, dict, dict[int, int]], dict[int, dict]]
    attendance_rules: Callable[[], dict]
    # Einstellungen (D3): Übersicht, speichern (prüft, ValueError bei ungültigem Wert) → (alt, neu), zurücksetzen
    settings_overview: Callable[[], list[dict]]
    save_setting: Callable[[str, Any, int], tuple[Any, Any]]
    reset_setting: Callable[[str], tuple[Any, Any]]


class Panel:
    def __init__(self, config: PanelConfig, hooks: PanelHooks):
        self.config = config
        self.hooks = hooks
        self.revoked: dict[str, float] = {}
        self.login_limiter = RateLimiter(LOGIN_ATTEMPTS, LOGIN_WINDOW_SECONDS)
        self.command_limiter = RateLimiter(COMMANDS_PER_MINUTE, 60)
        self.http: aiohttp.ClientSession | None = None
        self.console = ConsoleLog()
        self.log_handler = ConsoleLogHandler(self.console)
        self.connections: set[Connection] = set()
        self.tasks: set[asyncio.Task] = set()
        self.commit = "unbekannt"
        self.last_voice: list[dict] | None = None
        self.voice_push: asyncio.TimerHandle | None = None
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
            web.get("/api/status", self.api_status),
            web.get("/api/voice", self.api_voice),
            web.get("/api/applications", self.api_applications),
            web.post("/api/applications/new-season", self.api_new_season),
            web.get("/api/players", self.api_players),
            web.get("/api/tickets", self.api_tickets),
            web.get("/api/events", self.api_events),
            web.get("/api/attendance", self.api_attendance),
            web.get("/api/ranking", self.api_ranking),
            web.get("/api/settings", self.api_settings),
            web.post("/api/settings/{key}", self.api_save_setting),
            web.post("/api/settings/{key}/reset", self.api_reset_setting),
            web.get("/api/attendance.csv", self.api_attendance_csv),
            web.post("/api/events", self.api_create_event),
            web.post("/api/events/{message_id}", self.api_edit_event),
            web.post("/api/events/{message_id}/cancel", self.api_cancel_event),
            web.post("/api/players/{user_id}/sync-rank", self.api_sync_player_rank),
            web.get("/api/backups", self.api_backups),
            web.post("/api/backups", self.api_create_backup),
            web.get("/api/backups/{name}", self.api_download_backup),
            web.get("/ws", self.websocket),
        ])
        app.router.add_static("/static/", STATIC_DIR)
        app.on_response_prepare.append(self.add_security_headers)
        app.on_startup.append(self.open)
        app.on_shutdown.append(self.close_connections)
        app.on_cleanup.append(self.close)
        return app

    async def open(self, app: web.Application | None = None) -> None:
        self.commit = await read_git_commit()
        self.console.loop = asyncio.get_running_loop()
        self.console.listeners.append(self.broadcast_log)
        for name in CAPTURED_LOGGERS:
            logging.getLogger(name).addHandler(self.log_handler)
        # print()-Ausgaben bleiben im Terminal unverändert und erscheinen zusätzlich in der Konsole.
        sys.stdout = ConsoleStdout(sys.stdout, self.console)
        self.hooks.listeners.append(self.on_bot_event)
        self.spawn(self.watch_connections())

    async def close_connections(self, app: web.Application | None = None) -> None:
        for task in list(self.tasks):
            task.cancel()
        for connection in list(self.connections):
            await connection.ws.close(code=aiohttp.WSCloseCode.GOING_AWAY, message=b"Bot wird beendet")

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
        for name in CAPTURED_LOGGERS:
            logging.getLogger(name).removeHandler(self.log_handler)
        if isinstance(sys.stdout, ConsoleStdout) and sys.stdout.console is self.console:
            sys.stdout = sys.stdout.stream
        if self.broadcast_log in self.console.listeners:
            self.console.listeners.remove(self.broadcast_log)
        if self.on_bot_event in self.hooks.listeners:
            self.hooks.listeners.remove(self.on_bot_event)
        if self.voice_push:
            self.voice_push.cancel()
        if self.http and not self.http.closed:
            await self.http.close()

    def spawn(self, coroutine) -> asyncio.Task:
        task = asyncio.create_task(coroutine)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

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
        for connection in list(self.connections):
            if connection.sid == session["sid"]:
                self.spawn(connection.ws.close(code=WS_CLOSE_NO_ACCESS, message=b"Abgemeldet"))

    def connection_level(self, connection: Connection) -> int:
        """Aktuelle Stufe einer offenen Konsole; 0, wenn Session oder Staff-Rolle weg sind."""
        if connection.sid in self.revoked or connection.expires <= time.time() or self.guild() is None:
            return 0
        staff = self.staff(connection.user_id)
        return staff[1] if staff else 0

    async def watch_connections(self) -> None:
        # Rollenentzug, Abmelden und Ablauf der Session wirken auch auf offene Konsolen;
        # alle anderen bekommen bei der Gelegenheit den aktuellen Status fürs Dashboard.
        while True:
            await asyncio.sleep(WS_CHECK_SECONDS)
            if not self.connections:
                continue
            status = self.status()
            for connection in list(self.connections):
                if self.connection_level(connection) == 0:
                    await connection.ws.close(code=WS_CLOSE_NO_ACCESS, message=b"Kein Zugriff mehr")
                else:
                    connection.send({"type": "status", "status": status})
            # Fängt auch neue, umbenannte oder gelöschte Kanäle ab, für die es kein Voice-Ereignis gibt.
            self.push_voice()

    def on_bot_event(self, topic: str) -> None:
        if topic == "voice" and self.voice_push is None:
            # Mehrere Änderungen kurz hintereinander gehen als eine Nachricht raus.
            self.voice_push = asyncio.get_running_loop().call_later(0.3, self.push_voice)

    def push_voice(self) -> None:
        self.voice_push = None
        if not self.connections:
            return
        channels = self.voice_snapshot()
        if channels != self.last_voice:
            self.last_voice = channels
            for connection in list(self.connections):
                connection.send({"type": "voice", "channels": channels})

    def voice_snapshot(self) -> list[dict]:
        guild = self.guild()
        if guild is None:
            return []
        owners = self.hooks.temp_voice_owners(guild.id)
        channels = []
        ordered = sorted(
            guild.voice_channels,
            key=lambda channel: (channel.category.position if channel.category else -1, channel.position),
        )
        for channel in ordered:
            owner = guild.get_member(owners[channel.id]) if channel.id in owners else None
            members = []
            for member in channel.members:
                voice = member.voice
                members.append({
                    "id": str(member.id),
                    "name": member.display_name,
                    "avatar": member.display_avatar.url,
                    "muted": bool(voice and (voice.self_mute or voice.mute)),
                    "deafened": bool(voice and (voice.self_deaf or voice.deaf)),
                    "streaming": bool(voice and voice.self_stream),
                    "video": bool(voice and voice.self_video),
                })
            channels.append({
                "id": str(channel.id),
                "name": channel.name,
                "category": channel.category.name if channel.category else None,
                "temporary": channel.id in owners,
                "owner": (owner.display_name if owner else "unbekannt") if channel.id in owners else None,
                "members": members,
            })
        return channels

    def status(self) -> dict:
        hooks = self.hooks
        client = hooks.client
        guild = self.guild()
        now = time.time()
        latency = client.latency
        tasks = []
        for name, (label, loop) in hooks.loops.items():
            info = hooks.loop_status.get(name, {})
            next_run = loop.next_iteration if loop.is_running() else None
            tasks.append({
                "name": name,
                "label": label,
                "running": loop.is_running(),
                "last_run": info.get("last_run"),
                "next_run": int(next_run.timestamp()) if next_run else None,
                "last_error": info.get("last_error"),
                "last_error_at": info.get("last_error_at"),
            })

        def role_count(name: str) -> int:
            role = hooks.find_role(guild, name)
            return len(role.members) if role else 0

        rank_client = hooks.rank_client()
        return {
            "time": int(now),
            "bot": {
                "connected": bool(hooks.gateway.get("connected")) and client.is_ready(),
                "connection_changed_at": hooks.gateway.get("changed_at"),
                "latency_ms": round(latency * 1000) if math.isfinite(latency) else None,
                "user": str(client.user) if client.user else None,
                "started_at": int(hooks.started_at),
                "python": platform.python_version(),
                "discord_py": discord.__version__,
                "commit": self.commit,
            },
            "guild": {
                "name": guild.name,
                "members": guild.member_count,
                "players": role_count("Spieler"),
                "applicants": role_count("Bewerber"),
                "staff": len(hooks.staff_members(guild)),
            } if guild else None,
            "tasks": tasks,
            "henrikdev": {
                "enabled": rank_client is not None,
                "last_error": rank_client.last_error if rank_client else None,
                "last_error_at": rank_client.last_error_at if rank_client else None,
                "failures_in_row": rank_client.failures_in_row if rank_client else 0,
                "rate_limited": bool(rank_client and rank_client.rate_limited_until > now),
                "rate_limited_until": int(rank_client.rate_limited_until) if rank_client and rank_client.rate_limited_until > now else None,
            },
            "backup": self.backup_summary(),
            "alerts": {
                "channel": "Webhook" if hooks.alerts.webhook_url else "#audit-log",
                "active": [
                    {"text": problem["text"], "since": int(problem["since"]), "notified": problem["notified"]}
                    for problem in hooks.alerts.active.values()
                ],
                "recent": [
                    {
                        "kind": message["kind"],
                        "text": message["text"],
                        "time": int(message["time"]),
                        "delivered": message.get("delivered", "–"),
                    }
                    for message in reversed(hooks.alerts.history)
                ][:10],
            },
        }

    def broadcast_log(self, entry: dict) -> None:
        for connection in list(self.connections):
            connection.send({"type": "log", "entry": entry})

    async def restart_soon(self) -> None:
        # Kurz warten, damit Antwort und Audit-Eintrag noch rausgehen.
        await asyncio.sleep(1)
        await self.hooks.restart()

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

    async def api_status(self, request: web.Request) -> web.Response:
        self.authenticate(request)
        return web.json_response(self.status())

    async def api_voice(self, request: web.Request) -> web.Response:
        self.authenticate(request)
        return web.json_response({"channels": self.voice_snapshot()})

    @staticmethod
    def person(guild: discord.Guild, user_id: int | None) -> dict | None:
        """Anzeigename und Avatar aus dem Cache; das Panel speichert dazu nichts."""
        if user_id is None:
            return None
        member = guild.get_member(user_id)
        if member is None:
            return {"id": str(user_id), "name": f"Nicht mehr auf dem Server ({user_id})", "avatar": None, "on_server": False}
        return {"id": str(user_id), "name": member.display_name, "avatar": member.display_avatar.url, "on_server": True}

    async def api_applications(self, request: web.Request) -> web.Response:
        self.authenticate(request)
        guild = self.guild()
        wanted = request.query.get("status", "all")
        if wanted != "all" and wanted not in self.hooks.application_labels:
            raise json_error(400, "Unbekannter Status.")
        channel = self.hooks.find_text_channel(guild, "bewerbungen")
        votes = self.hooks.application_vote_counts(guild.id)
        applications = []
        for application in self.hooks.list_applications(guild.id):
            if wanted != "all" and application.status != wanted:
                continue
            yes, no = votes.get(application.message_id, (0, 0))
            applications.append({
                "message_id": str(application.message_id),
                "applicant": self.person(guild, application.applicant_id),
                "status": application.status,
                "status_label": self.hooks.application_labels.get(application.status, application.status),
                "submitted_at": application.submitted_at,
                "decided_by": self.person(guild, application.decided_by),
                "decided_at": application.decided_at,
                "votes": {"yes": yes, "no": no},
                "url": f"https://discord.com/channels/{guild.id}/{channel.id}/{application.message_id}" if channel else None,
            })
        return web.json_response({"applications": applications, "labels": self.hooks.application_labels})

    def team_role(self, guild: discord.Guild, member: discord.Member | None) -> str:
        if member is None:
            return "Nicht mehr auf dem Server"
        level = self.hooks.staff_level(member)
        if level:
            return LEVEL_NAMES[level]
        for name in ("Spieler", "Bewerber"):
            role = self.hooks.find_role(guild, name)
            if role and role in member.roles:
                return name
        return "Mitglied"

    async def api_players(self, request: web.Request) -> web.Response:
        self.authenticate(request)
        guild = self.guild()
        only_players = request.query.get("filter", "players") == "players"
        players = []
        for profile in self.hooks.list_profiles(guild.id):
            member = guild.get_member(profile.user_id)
            role = self.team_role(guild, member)
            if only_players and role != "Spieler":
                continue
            players.append({
                "user": self.person(guild, profile.user_id),
                "role": role,
                "riot_id": profile.riot_id,
                "region": profile.region,
                "rank_tier": profile.rank_tier,
                "rank_synced_at": profile.rank_synced_at,
                "main_role": profile.main_role,
                "playable_roles": profile.playable_roles,
            })
        players.sort(key=lambda player: player["user"]["name"].casefold())
        return web.json_response({"players": players})

    async def api_sync_player_rank(self, request: web.Request) -> web.Response:
        _, staff_member, _ = self.authenticate(request, ADMIN)
        guild = self.guild()
        user_id = parse_user_id(request.match_info["user_id"])
        member = guild.get_member(user_id) if user_id else None
        if member is None:
            raise web.HTTPNotFound(text=json.dumps({"error": "Diese Person ist nicht auf dem Server."}), content_type="application/json")
        await self.audit_action(staff_member, f"hat den Rang von {member.mention} abgeglichen")
        return web.json_response({"message": await self.hooks.sync_ranks(guild, member)})

    async def api_tickets(self, request: web.Request) -> web.Response:
        self.authenticate(request)
        guild = self.guild()
        wanted = request.query.get("state", "open")
        if wanted not in ("open", "closed", "all"):
            raise json_error(400, "Unbekannter Filter.")
        tickets = []
        for thread_id, reporter_id, created_at, closed_at in self.hooks.list_tickets(guild.id):
            if (wanted == "open" and closed_at) or (wanted == "closed" and not closed_at):
                continue
            tickets.append({
                "thread_id": str(thread_id),
                "reporter": self.person(guild, reporter_id),
                "created_at": created_at,
                "closed_at": closed_at,
                "url": f"https://discord.com/channels/{guild.id}/{thread_id}",
            })
        return web.json_response({"tickets": tickets})

    async def api_events(self, request: web.Request) -> web.Response:
        self.authenticate(request)
        guild = self.guild()
        wanted = request.query.get("range", "upcoming")
        if wanted not in ("upcoming", "past", "all"):
            raise json_error(400, "Unbekannter Zeitraum.")
        now = time.time()
        rsvps = self.hooks.event_rsvps(guild.id)
        reminders = self.hooks.event_reminders(guild.id)
        events = []
        for event in self.hooks.list_events(guild.id):
            upcoming = event.starts_at > now
            if (wanted == "upcoming" and not upcoming) or (wanted == "past" and upcoming):
                continue
            responses = rsvps.get(event.message_id, {})
            events.append({
                "id": str(event.message_id),
                "title": event.title or "Termin ohne gespeicherten Titel",
                "details": event.details or "",
                "starts_at": event.starts_at,
                "cancelled_at": event.cancelled_at,
                "upcoming": upcoming,
                "rsvps": {
                    choice: [self.person(guild, user_id) for user_id in responses.get(choice, [])]
                    for choice in ("going", "maybe", "declined")
                },
                "reminders": reminders.get(event.message_id, {}),
                "url": f"https://discord.com/channels/{guild.id}/{event.channel_id}/{event.message_id}",
            })
        if wanted == "past":
            events.reverse()
        return web.json_response({"events": events})

    def attendance(self, request: web.Request) -> dict:
        guild = self.guild()
        rules = self.hooks.attendance_rules()
        try:
            days = int(request.query.get("days", "30"))
        except ValueError:
            days = 0
        if not 1 <= days <= rules["retention_days"]:
            raise json_error(400, f"Der Zeitraum muss zwischen 1 und {rules['retention_days']} Tagen liegen.")
        until = int(time.time())
        since = until - days * 24 * 60 * 60
        player_role = self.hooks.find_role(guild, "Spieler")
        players = [member for member in (player_role.members if player_role else []) if not member.bot]
        members = {member.id: int(member.joined_at.timestamp()) if member.joined_at else 0 for member in players}
        events, responses = self.hooks.attendance_events(guild.id, since, until)
        summary = self.hooks.summarize_attendance(events, responses, members)
        rows = []
        for member in players:
            entry = summary[member.id]
            rows.append({
                "user": self.person(guild, member.id),
                **{key: entry[key] for key in ("going", "maybe", "declined", "none", "total", "rate", "flagged")},
                "history": [
                    {"id": str(item["message_id"]), "title": item["title"] or "Termin", "starts_at": item["starts_at"], "choice": item["choice"]}
                    for item in entry["history"]
                ],
            })
        rows.sort(key=lambda row: row["user"]["name"].casefold())
        return {"days": days, "since": since, "until": until, "events": len(events), "rules": rules, "players": rows}

    async def api_settings(self, request: web.Request) -> web.Response:
        self.authenticate(request, ADMIN)
        guild = self.guild()
        settings = self.hooks.settings_overview()
        for entry in settings:
            entry["updated_by"] = self.person(guild, entry["updated_by"])
        return web.json_response({"settings": settings})

    def setting_label(self, key: str) -> str:
        return next((entry["label"] for entry in self.hooks.settings_overview() if entry["key"] == key), key)

    async def api_save_setting(self, request: web.Request) -> web.Response:
        _, member, _ = self.authenticate(request, ADMIN)
        key = request.match_info["key"]
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict) or "value" not in body:
            raise json_error(400, "Ungültige Anfrage.")
        try:
            old, new = self.hooks.save_setting(key, body["value"], member.id)
        except ValueError as error:
            raise json_error(400, str(error)) from None
        await self.audit_action(
            member, f"hat die Einstellung „{self.setting_label(key)}“ geändert: {setting_text(old)} → {setting_text(new)}"
        )
        return web.json_response({"value": list(new) if isinstance(new, tuple) else new})

    async def api_reset_setting(self, request: web.Request) -> web.Response:
        _, member, _ = self.authenticate(request, ADMIN)
        key = request.match_info["key"]
        try:
            old, new = self.hooks.reset_setting(key)
        except ValueError as error:
            raise json_error(400, str(error)) from None
        await self.audit_action(
            member, f"hat die Einstellung „{self.setting_label(key)}“ auf den Standard gesetzt: {setting_text(old)} → {setting_text(new)}"
        )
        return web.json_response({"value": list(new) if isinstance(new, tuple) else new})

    async def api_ranking(self, request: web.Request) -> web.Response:
        self.authenticate(request)
        guild = self.guild()
        histories = self.hooks.rank_histories(guild.id)

        def value(tier: str | None) -> list[int] | None:
            rank = self.hooks.rank_value(tier)
            return list(rank) if rank else None

        players = []
        for position, (member, profile, name) in enumerate(self.hooks.rank_leaderboard(guild), 1):
            players.append({
                "position": position,
                "user": self.person(guild, member.id),
                "name": name,
                "riot_id": profile.riot_id,
                "rank_tier": profile.rank_tier,
                "rank_value": value(profile.rank_tier),
                "rank_synced_at": profile.rank_synced_at,
                "history": [
                    {"rank_tier": tier, "recorded_at": recorded_at, "rank_value": value(tier)}
                    for tier, recorded_at in histories.get(member.id, [])
                ],
            })
        return web.json_response({"players": players})

    async def api_attendance(self, request: web.Request) -> web.Response:
        self.authenticate(request)
        return web.json_response(self.attendance(request))

    async def api_attendance_csv(self, request: web.Request) -> web.Response:
        _, member, _ = self.authenticate(request)
        data = self.attendance(request)
        output = io.StringIO()
        # Semikolon und BOM, damit Excel die Datei auf deutschen Systemen direkt richtig öffnet.
        writer = csv.writer(output, delimiter=";")
        writer.writerow(["Name", "Discord-ID", "Zusagen", "Vielleicht", "Absagen", "Keine Reaktion", "Termine", "Quote %", "Markiert"])
        for row in data["players"]:
            writer.writerow([
                spreadsheet_safe(row["user"]["name"]), row["user"]["id"], row["going"], row["maybe"], row["declined"],
                row["none"], row["total"], "" if row["rate"] is None else row["rate"], "ja" if row["flagged"] else "nein",
            ])
        log.info("Panel: %s (%s) hat die Anwesenheit der letzten %s Tage exportiert", member, member.id, data["days"])
        filename = f"anwesenheit-{datetime.now(BERLIN):%Y-%m-%d}-{data['days']}-tage.csv"
        return web.Response(
            body=("\ufeff" + output.getvalue()).encode(),
            headers={
                "Content-Type": "text/csv; charset=utf-8",
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Cache-Control": "no-store",
            },
        )

    async def event_form(self, request: web.Request) -> tuple[str, str, int, bool]:
        """Formular prüfen: Zeit kommt als „JJJJ-MM-TTTHH:MM“ in Berliner Zeit."""
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            raise json_error(400, "Ungültige Anfrage.")
        title, details, starts_at = body.get("title"), body.get("details", ""), body.get("starts_at")
        if not isinstance(title, str) or not isinstance(details, str) or not isinstance(starts_at, str):
            raise json_error(400, "Titel, Datum und Uhrzeit fehlen.")
        try:
            moment = datetime.strptime(starts_at, "%Y-%m-%dT%H:%M").replace(tzinfo=BERLIN)
        except ValueError:
            raise json_error(400, "Datum und Uhrzeit sind ungültig.") from None
        try:
            clean_title, clean_details, timestamp = self.hooks.validate_event(title, moment, details)
        except ValueError as error:
            raise json_error(400, str(error)) from None
        return clean_title, clean_details, timestamp, body.get("notify") is True

    @staticmethod
    def event_id(request: web.Request) -> int:
        try:
            return int(request.match_info["message_id"])
        except ValueError:
            raise json_error(400, "Ungültiger Termin.") from None

    async def run_event_change(self, action: Awaitable[str], member: discord.Member, audit_text: str) -> web.Response:
        # Audit erst nach Erfolg, damit dort nichts steht, was Discord abgelehnt hat.
        try:
            message = await action
        except ValueError as error:
            return web.json_response({"error": str(error)}, status=400)
        except discord.HTTPException:
            log.exception("Termin konnte in Discord nicht geändert werden")
            return web.json_response({"error": "Discord hat die Änderung abgelehnt. Details stehen im Log."}, status=502)
        await self.audit_action(member, audit_text)
        return web.json_response({"message": message})

    def event_title(self, message_id: int) -> str:
        event = next((event for event in self.hooks.list_events(self.guild().id) if event.message_id == message_id), None)
        return discord.utils.escape_markdown(event.title) if event and event.title else f"`{message_id}`"

    async def api_create_event(self, request: web.Request) -> web.Response:
        _, member, _ = self.authenticate(request)
        title, details, starts_at, _ = await self.event_form(request)

        async def publish() -> str:
            await self.hooks.publish_event(self.guild(), title, details, starts_at)
            return "Termin in #spieltermine veröffentlicht."

        return await self.run_event_change(
            publish(), member, f"hat den Termin „{discord.utils.escape_markdown(title)}“ für <t:{starts_at}:F> angelegt"
        )

    async def api_edit_event(self, request: web.Request) -> web.Response:
        _, member, _ = self.authenticate(request)
        message_id = self.event_id(request)
        title, details, starts_at, notify = await self.event_form(request)
        return await self.run_event_change(
            self.hooks.edit_event(self.guild(), message_id, title, details, starts_at, notify=notify),
            member,
            f"hat den Termin „{discord.utils.escape_markdown(title)}“ geändert (jetzt <t:{starts_at}:F>)",
        )

    async def api_cancel_event(self, request: web.Request) -> web.Response:
        _, member, _ = self.authenticate(request)
        message_id = self.event_id(request)
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict) or body.get("confirm") is not True:
            raise json_error(400, "Die Absage muss bestätigt werden.")
        return await self.run_event_change(
            self.hooks.cancel_event(self.guild(), message_id, notify=body.get("notify") is True),
            member,
            f"hat den Termin „{self.event_title(message_id)}“ abgesagt",
        )

    async def api_new_season(self, request: web.Request) -> web.Response:
        _, member, _ = self.authenticate(request, ADMIN)
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict) or body.get("confirm") is not True:
            raise json_error(400, "Die neue Saison muss bestätigt werden.")
        await self.audit_action(member, "hat die neue Saison gestartet (Bewerbungen wieder geöffnet)")
        try:
            reopened = await self.hooks.reopen_applications(self.guild())
        except discord.HTTPException:
            log.exception("Neue Saison aus dem Panel ist fehlgeschlagen")
            return web.json_response(
                {"error": "Nicht alle Bewerber-Rollen konnten entfernt werden. Prüfe die Rollen-Reihenfolge und versuche es erneut."},
                status=502,
            )
        if reopened is None:
            return web.json_response({"error": "Die Rolle „Bewerber“ wurde nicht gefunden."}, status=409)
        return web.json_response({"reopened": reopened})

    def backup_summary(self) -> dict:
        try:
            backups = self.hooks.list_backups()
        except OSError:
            backups = []
        last = backups[0] if backups else None
        state = self.hooks.backup_state
        return {
            "last": {"name": last["name"], "size": last["size"], "created": last["created"]} if last else None,
            "count": len(backups),
            "last_error": state.get("last_error"),
            "last_error_at": state.get("last_error_at"),
        }

    async def api_backups(self, request: web.Request) -> web.Response:
        self.authenticate(request, OWNER)
        return web.json_response({
            "backups": [
                {"name": backup["name"], "size": backup["size"], "created": backup["created"]}
                for backup in self.hooks.list_backups()
            ],
            **{key: value for key, value in self.backup_summary().items() if key.startswith("last_error")},
        })

    async def api_create_backup(self, request: web.Request) -> web.Response:
        _, member, _ = self.authenticate(request, ADMIN)
        await self.audit_action(member, "hat ein Datenbank-Backup erstellt")
        try:
            backup = await self.hooks.run_backup()
        except (OSError, sqlite3.Error) as error:
            return web.json_response({"error": f"Das Backup ist fehlgeschlagen ({type(error).__name__})."}, status=500)
        return web.json_response({"name": backup["name"], "size": backup["size"], "removed": backup["removed"]})

    async def api_download_backup(self, request: web.Request) -> web.StreamResponse:
        _, member, _ = self.authenticate(request, OWNER)
        # Nur Namen aus der eigenen Liste; der Pfad kommt nie aus der Anfrage.
        backup = next((entry for entry in self.hooks.list_backups() if entry["name"] == request.match_info["name"]), None)
        if backup is None:
            raise web.HTTPNotFound(text=json.dumps({"error": "Dieses Backup gibt es nicht."}), content_type="application/json")
        await self.audit_action(member, f"hat das Backup `{backup['name']}` heruntergeladen")
        return web.FileResponse(backup["path"], headers={
            "Content-Disposition": f'attachment; filename="{backup["name"]}"',
            "Content-Type": "application/octet-stream",
            "Cache-Control": "no-store",
        })

    async def audit_action(self, member: discord.Member, action: str) -> None:
        """Steuernde Aktion aus dem Panel ins Log und in den Audit-Kanal."""
        log.info("Panel: %s (%s) %s", member, member.id, action)
        guild = self.guild()
        if guild:
            await self.hooks.audit(guild, f"🖥️ **Panel:** {member.mention} (`{member.id}`) {action}", PANEL_COLOR)

    def command_list(self, level: int) -> list[dict]:
        return [
            {"name": command.name, "usage": command.usage, "description": command.description, "confirm": command.confirm}
            for command in COMMANDS.values()
            if level >= command.level
        ]

    async def websocket(self, request: web.Request) -> web.WebSocketResponse:
        session = self.session(request)
        if session is None:
            raise json_error(401, "Bitte melde dich an.")
        # Browser schicken beim WebSocket-Aufbau keine eigenen Header mit, deshalb kommt das Token als Parameter.
        if not csrf_valid(session, self.config.origin, request.headers.get("Origin"), request.query.get("csrf")):
            log.warning("Panel: WebSocket ohne gültiges CSRF-Token abgelehnt")
            raise json_error(403, "Ungültige Anfrage. Bitte lade die Seite neu.")
        _, _, level = self.authenticate(request)
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=16 * 1024)
        await ws.prepare(request)
        connection = Connection(ws, session)
        self.connections.add(connection)
        writer = asyncio.create_task(connection.write())
        try:
            connection.send({
                "type": "hello",
                "logs": self.console.snapshot(),
                "commands": self.command_list(level),
                "status": self.status(),
                "voice": self.voice_snapshot(),
            })
            async for message in ws:
                if message.type == aiohttp.WSMsgType.TEXT:
                    self.handle_message(connection, message.data)
        finally:
            self.connections.discard(connection)
            writer.cancel()
        return ws

    def handle_message(self, connection: Connection, data: str) -> None:
        try:
            message = json.loads(data)
        except ValueError:
            return
        if not isinstance(message, dict) or message.get("type") != "command":
            return
        request_id = message.get("id")
        line = message.get("line")

        def reply(ok: bool, text: str) -> None:
            connection.send({"type": "result", "id": request_id, "ok": ok, "text": text})

        level = self.connection_level(connection)
        if level == 0:
            reply(False, "Du hast keinen Zugriff mehr auf das Panel.")
            self.spawn(connection.ws.close(code=WS_CLOSE_NO_ACCESS, message=b"Kein Zugriff mehr"))
            return
        if not isinstance(line, str) or not line.strip() or len(line) > 2100:
            reply(False, "Bitte gib einen Befehl ein. Mit „help“ siehst du alle Befehle.")
            return
        if not self.command_limiter.allow(connection.user_id):
            reply(False, "Zu viele Befehle in kurzer Zeit. Bitte warte einen Moment.")
            return
        self.spawn(self.run_command(connection.user_id, level, line, message.get("confirm") is True, reply))

    async def run_command(self, user_id: int, level: int, line: str, confirmed: bool, reply) -> None:
        name, arguments = parse_command_line(line)
        command = COMMANDS.get(name)
        guild = self.guild()
        member = guild.get_member(user_id) if guild else None
        if command is None:
            reply(False, f"Unbekannter Befehl „{name[:50]}“. Mit „help“ siehst du alle Befehle.")
            return
        if guild is None or member is None:
            reply(False, "Der Bot ist gerade nicht mit Discord verbunden.")
            return
        if level < command.level:
            log.warning("Panel-Befehl %s von %s abgelehnt: fehlende Rechte", name, user_id)
            reply(False, f"„{name}“ braucht mindestens die Rolle {LEVEL_NAMES[command.level]}.")
            return
        if command.confirm and not confirmed:
            reply(False, f"„{name}“ muss im Panel bestätigt werden.")
            return
        if command.controlling:
            log.info("Panel-Befehl von %s (%s): %s", member, member.id, line.strip()[:300])
            await self.hooks.audit(
                guild,
                f"🖥️ **Panel-Befehl** von {member.mention} (`{member.id}`):\n"
                f"{discord.utils.escape_markdown(line.strip()[:1500])}",
                PANEL_COLOR,
            )
        try:
            reply(True, await command.run(CommandContext(self, guild, member, level), arguments))
        except CommandError as error:
            reply(False, str(error))
        except Exception:
            log.exception("Panel-Befehl %s ist fehlgeschlagen", name)
            reply(False, "Der Befehl ist fehlgeschlagen. Details stehen im Log.")


async def read_git_commit() -> str:
    """Aktueller Commit, einmal beim Start gelesen (feste Argumente, keine Shell)."""
    try:
        process = await asyncio.create_subprocess_exec(
            "git", "rev-parse", "--short", "HEAD",
            cwd=Path(__file__).parent,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        output, _ = await asyncio.wait_for(process.communicate(), timeout=5)
    except (OSError, asyncio.TimeoutError):
        return "unbekannt"
    return output.decode().strip() or "unbekannt"


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
