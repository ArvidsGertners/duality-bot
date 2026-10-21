"""Tests für das Web-Panel ohne Discord-Verbindung: python -m unittest discover -s tests"""

import asyncio
import io
import logging
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp.test_utils import TestClient, TestServer

import main
import panel


def env_value(name: str) -> str | None:
    """Wert aus der Umgebung oder aus .env im Projektordner; echte Tokens stehen nie im Code."""
    if os.environ.get(name):
        return os.environ[name]
    try:
        lines = (Path(__file__).resolve().parent.parent / ".env").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        key, separator, value = line.strip().removeprefix("export ").partition("=")
        if separator and key.strip() == name:
            return value.strip().strip("'\"") or None
    return None


SECRET = b"x" * 40
ORIGIN = "http://localhost:8080"
CONFIG = panel.PanelConfig(client_id="123", client_secret="geheim", base_url=ORIGIN, secret_key=SECRET)


class FakeMember:
    def __init__(self, user_id: int, name: str):
        self.id = user_id
        self.mention = f"<@{user_id}>"
        self.display_name = name
        self.display_avatar = SimpleNamespace(url="https://cdn.discordapp.com/embed/avatars/0.png")
        self.bot = False

    def __str__(self) -> str:
        return self.display_name


class FakeChannel:
    def __init__(self, channel_id: int, name: str, can_send: bool = True):
        self.id = channel_id
        self.name = name
        self.can_send = can_send
        self.send = mock.AsyncMock()

    def permissions_for(self, member):
        return SimpleNamespace(view_channel=True, send_messages=self.can_send)


class FakeGuild:
    def __init__(self, members):
        self.id = 1
        self.name = "Duality Test"
        self.member_count = 4
        self.me = FakeMember(1, "Duality-Bot")
        self.members = {member.id: member for member in members}
        self.text_channels = [FakeChannel(111111111111111111, "allgemein"), FakeChannel(222222222222222222, "Orga", can_send=False)]

    def get_member(self, user_id: int):
        return self.members.get(user_id)

    def get_channel(self, channel_id: int):
        return next((channel for channel in self.text_channels if channel.id == channel_id), None)


class FakeClient:
    def __init__(self, guild):
        self.guild = guild
        self.ready = True
        self.latency = 0.042
        self.user = "Duality-Bot#0001"

    def get_guild(self, guild_id: int):
        return self.guild

    def is_ready(self) -> bool:
        return self.ready


OWNER_ID, ADMIN_ID, COACH_ID, PLAYER_ID = 100000000000000010, 100000000000000020, 100000000000000030, 100000000000000040
LEVELS = {OWNER_ID: panel.OWNER, ADMIN_ID: panel.ADMIN, COACH_ID: panel.COACH}


class FakeLoop:
    def __init__(self, running: bool = True):
        self.running = running
        self.next_iteration = datetime(2026, 10, 22, 11, 0, tzinfo=timezone.utc)

    def is_running(self) -> bool:
        return self.running


def make_hooks(client=None) -> panel.PanelHooks:
    guild = FakeGuild([FakeMember(user_id, f"Person {user_id}") for user_id in (OWNER_ID, ADMIN_ID, COACH_ID, PLAYER_ID)])

    return panel.PanelHooks(
        client=client or FakeClient(guild),
        guild_id=1,
        staff_level=lambda member: LEVELS.get(member.id, 0),
        audit=mock.AsyncMock(),
        sync_ranks=mock.AsyncMock(return_value="Rang abgeglichen"),
        send_reminders=mock.AsyncMock(return_value=1),
        purge=mock.AsyncMock(return_value=(2, 3)),
        cleanup_voice=mock.AsyncMock(return_value=4),
        restart=mock.AsyncMock(),
        started_at=time.time() - 3 * 60 * 60,
        gateway={"connected": True, "changed_at": 0},
        loops={"sync_ranks": ("Rang-Abgleich", FakeLoop()), "event_reminders": ("Termin-Erinnerungen", FakeLoop(running=False))},
        loop_status={"event_reminders": {"last_run": 1792580000, "last_error": "RuntimeError: kaputt", "last_error_at": 1792580000}},
        rank_client=lambda: None,
        find_role=lambda guild, name: SimpleNamespace(members=[1, 2]) if name == "Spieler" else None,
        staff_members=lambda guild: [guild.get_member(OWNER_ID), guild.get_member(COACH_ID)],
        alerts=main.Alerts(),
    )


def session_cookie(user_id: int, session: dict | None = None) -> tuple[dict, str]:
    session = session or panel.new_session(user_id)
    return session, f"{panel.SESSION_COOKIE}={panel.sign(session, SECRET)}"


class SigningTests(unittest.TestCase):
    def test_roundtrip(self):
        session = panel.new_session(42, now=1000)
        token = panel.sign(session, SECRET)
        self.assertEqual(panel.unsign(token, SECRET, "session", now=1001), session)

    def test_tampered_payload_is_rejected(self):
        token = panel.sign(panel.new_session(42, now=1000), SECRET)
        body, signature = token.split(".")
        forged = panel._b64encode(panel._b64decode(body).replace(b"42", b"43"))
        self.assertIsNone(panel.unsign(f"{forged}.{signature}", SECRET, "session", now=1001))

    def test_wrong_key_expired_purpose_and_garbage(self):
        session = panel.new_session(42, now=1000)
        token = panel.sign(session, SECRET)
        self.assertIsNone(panel.unsign(token, b"y" * 40, "session", now=1001))
        self.assertIsNone(panel.unsign(token, SECRET, "session", now=session["exp"]))
        self.assertIsNone(panel.unsign(token, SECRET, "state", now=1001))
        for garbage in (None, "", "abc", "a.b.c", "äöü.ß", "e30.", "!!!.???"):
            self.assertIsNone(panel.unsign(garbage, SECRET, "session"))

    def test_session_lasts_twelve_hours(self):
        session = panel.new_session(42, now=1000)
        self.assertEqual(session["exp"], 1000 + 12 * 60 * 60)
        self.assertNotEqual(session["csrf"], panel.new_session(42, now=1000)["csrf"])


class CsrfTests(unittest.TestCase):
    session = {"csrf": "token-123"}

    def test_valid(self):
        self.assertTrue(panel.csrf_valid(self.session, ORIGIN, ORIGIN, "token-123"))

    def test_wrong_origin_or_token(self):
        self.assertFalse(panel.csrf_valid(self.session, ORIGIN, "https://evil.example", "token-123"))
        self.assertFalse(panel.csrf_valid(self.session, ORIGIN, None, "token-123"))
        self.assertFalse(panel.csrf_valid(self.session, ORIGIN, ORIGIN, "token-124"))
        self.assertFalse(panel.csrf_valid(self.session, ORIGIN, ORIGIN, None))
        self.assertFalse(panel.csrf_valid({}, ORIGIN, ORIGIN, ""))


class RateLimiterTests(unittest.TestCase):
    def test_limit_and_window(self):
        limiter = panel.RateLimiter(2, 10)
        self.assertTrue(limiter.allow("a", now=0))
        self.assertTrue(limiter.allow("a", now=1))
        self.assertFalse(limiter.allow("a", now=2))
        self.assertTrue(limiter.allow("b", now=2))
        self.assertTrue(limiter.allow("a", now=10.5))


class ConfigTests(unittest.TestCase):
    base = {
        "DISCORD_CLIENT_ID": "123",
        "DISCORD_CLIENT_SECRET": "geheim",
        "PANEL_BASE_URL": "https://panel.example.de/",
        "PANEL_SECRET_KEY": "k" * 40,
    }

    def test_valid_defaults(self):
        config, problem = panel.load_config(self.base)
        self.assertIsNone(problem)
        self.assertEqual((config.host, config.port), ("127.0.0.1", 8080))
        self.assertEqual(config.base_url, "https://panel.example.de")
        self.assertEqual(config.redirect_uri, "https://panel.example.de/auth/callback")
        self.assertTrue(config.secure_cookies)

    def test_missing_and_invalid(self):
        self.assertIn("PANEL_SECRET_KEY", panel.load_config({**self.base, "PANEL_SECRET_KEY": ""})[1])
        self.assertIsNone(panel.load_config({**self.base, "PANEL_SECRET_KEY": "kurz"})[0])
        self.assertIsNone(panel.load_config({**self.base, "PANEL_BASE_URL": "http://panel.example.de"})[0])
        self.assertIsNone(panel.load_config({**self.base, "PANEL_BASE_URL": "https://x.de/panel"})[0])
        self.assertIsNone(panel.load_config({**self.base, "PANEL_PORT": "abc"})[0])

    def test_local_http_without_secure_cookie(self):
        config, _ = panel.load_config({**self.base, "PANEL_BASE_URL": "http://localhost:8080", "PANEL_PORT": "9000"})
        self.assertFalse(config.secure_cookies)
        self.assertEqual(config.port, 9000)


class ClientIpTests(unittest.TestCase):
    def test_forwarded_only_from_local_proxy(self):
        local = SimpleNamespace(remote="127.0.0.1", headers={"X-Forwarded-For": "1.2.3.4, 5.6.7.8"})
        direct = SimpleNamespace(remote="9.9.9.9", headers={"X-Forwarded-For": "1.2.3.4"})
        self.assertEqual(panel.client_ip(local), "5.6.7.8")
        self.assertEqual(panel.client_ip(direct), "9.9.9.9")


class StaffLevelTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = mock.patch.object(main, "DATA_PATH", Path(directory.name) / "test.sqlite3")
        patcher.start()
        self.addCleanup(patcher.stop)
        main.initialize_storage()

    def test_highest_role_wins(self):
        roles = [SimpleNamespace(id=index, name=name) for index, name in enumerate(main.STAFF_ROLES + ("Spieler",), 1)]
        guild = SimpleNamespace(id=99, roles=roles, get_role=lambda role_id: None)

        def member(*names):
            return SimpleNamespace(guild=guild, roles=[role for role in roles if role.name in names])

        self.assertEqual(main.staff_level(member("Owner", "Coach")), 3)
        self.assertEqual(main.staff_level(member("Admin")), 2)
        self.assertEqual(main.staff_level(member("Coach", "Spieler")), 1)
        self.assertEqual(main.staff_level(member("Spieler")), 0)


class PanelHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hooks = make_hooks()
        self.panel = panel.Panel(CONFIG, self.hooks)
        self.client = TestClient(TestServer(self.panel.build_app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def test_healthcheck(self):
        response = await self.client.get("/healthz")
        self.assertEqual(response.status, 200)
        self.hooks.client.ready = False
        response = await self.client.get("/healthz")
        self.assertEqual(response.status, 503)

    async def test_security_headers(self):
        response = await self.client.get("/")
        self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
        self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    async def test_login_page_without_session(self):
        response = await self.client.get("/")
        self.assertEqual(response.status, 200)
        self.assertIn("Mit Discord anmelden", await response.text())

    async def test_index_for_staff_and_former_staff(self):
        _, cookie = session_cookie(COACH_ID)
        response = await self.client.get("/", headers={"Cookie": cookie})
        self.assertIn('id="nav"', await response.text())
        _, cookie = session_cookie(PLAYER_ID)
        response = await self.client.get("/", headers={"Cookie": cookie})
        self.assertEqual(response.status, 403)

    async def test_api_me(self):
        self.assertEqual((await self.client.get("/api/me")).status, 401)
        session, cookie = session_cookie(ADMIN_ID)
        response = await self.client.get("/api/me", headers={"Cookie": cookie})
        data = await response.json()
        self.assertEqual((data["level"], data["role"], data["csrf"]), (panel.ADMIN, "Admin", session["csrf"]))
        self.assertEqual(response.headers["Cache-Control"], "no-store")

    async def test_role_removal_takes_effect_immediately(self):
        _, cookie = session_cookie(COACH_ID)
        self.assertEqual((await self.client.get("/api/me", headers={"Cookie": cookie})).status, 200)
        LEVELS.pop(COACH_ID)
        try:
            self.assertEqual((await self.client.get("/api/me", headers={"Cookie": cookie})).status, 403)
        finally:
            LEVELS[COACH_ID] = panel.COACH

    async def test_tampered_cookie(self):
        _, cookie = session_cookie(OWNER_ID)
        response = await self.client.get("/api/me", headers={"Cookie": cookie[:-2] + "xx"})
        self.assertEqual(response.status, 401)

    async def test_bot_disconnected(self):
        self.hooks.client.guild = None
        _, cookie = session_cookie(OWNER_ID)
        self.assertEqual((await self.client.get("/api/me", headers={"Cookie": cookie})).status, 503)

    async def test_logout_requires_csrf_and_origin(self):
        session, cookie = session_cookie(COACH_ID)
        good = {"Cookie": cookie, "Origin": ORIGIN, "X-CSRF-Token": session["csrf"]}
        self.assertEqual((await self.client.post("/auth/logout")).status, 401)
        self.assertEqual((await self.client.post("/auth/logout", headers={**good, "X-CSRF-Token": "falsch"})).status, 403)
        self.assertEqual((await self.client.post("/auth/logout", headers={**good, "Origin": "https://evil.example"})).status, 403)
        without_origin = {key: value for key, value in good.items() if key != "Origin"}
        self.assertEqual((await self.client.post("/auth/logout", headers=without_origin)).status, 403)
        self.assertEqual((await self.client.post("/auth/logout", headers=good)).status, 200)
        # Nach dem Abmelden ist die Session auch mit dem alten Cookie ungültig.
        self.assertEqual((await self.client.get("/api/me", headers={"Cookie": cookie})).status, 401)

    async def test_login_redirect_sets_state(self):
        response = await self.client.get("/auth/login", allow_redirects=False)
        self.assertEqual(response.status, 302)
        location = urlparse(response.headers["Location"])
        query = parse_qs(location.query)
        self.assertEqual(location.netloc, "discord.com")
        self.assertEqual(query["scope"], ["identify"])
        self.assertEqual(query["redirect_uri"], [CONFIG.redirect_uri])
        state_cookie = response.cookies[panel.STATE_COOKIE]
        self.assertTrue(state_cookie["httponly"])
        self.assertEqual(state_cookie["samesite"], "Lax")
        stored = panel.unsign(state_cookie.value, SECRET, "state")
        self.assertEqual(stored["state"], query["state"][0])

    async def login_via_callback(self, user_id: int, state: str = "abc", sent_state: str = "abc"):
        state_token = panel.sign({"use": "state", "state": state, "exp": 2**40}, SECRET)
        with mock.patch.object(self.panel, "fetch_user_id", mock.AsyncMock(return_value=user_id)):
            return await self.client.get(
                f"/auth/callback?code=xyz&state={sent_state}",
                headers={"Cookie": f"{panel.STATE_COOKIE}={state_token}"},
                allow_redirects=False,
            )

    async def test_callback_checks_state(self):
        response = await self.login_via_callback(OWNER_ID, sent_state="anders")
        self.assertEqual(response.status, 400)
        self.assertNotIn(panel.SESSION_COOKIE, response.cookies)

    async def test_callback_for_staff_sets_session(self):
        response = await self.login_via_callback(OWNER_ID)
        self.assertEqual(response.status, 302)
        cookie = response.cookies[panel.SESSION_COOKIE]
        self.assertTrue(cookie["httponly"])
        self.assertFalse(cookie["secure"])
        self.assertEqual(panel.unsign(cookie.value, SECRET, "session")["uid"], OWNER_ID)

    async def test_callback_rejects_non_staff(self):
        response = await self.login_via_callback(PLAYER_ID)
        self.assertEqual(response.status, 403)
        self.assertNotIn(panel.SESSION_COOKIE, response.cookies)

    async def test_login_rate_limit(self):
        for _ in range(panel.LOGIN_ATTEMPTS):
            self.assertEqual((await self.client.get("/auth/login", allow_redirects=False)).status, 302)
        self.assertEqual((await self.client.get("/auth/login", allow_redirects=False)).status, 429)


class ParserTests(unittest.TestCase):
    def test_command_line(self):
        self.assertEqual(panel.parse_command_line("  SAY  #allgemein  Hallo   Welt "), ("say", "#allgemein  Hallo   Welt"))
        self.assertEqual(panel.parse_command_line("help"), ("help", ""))
        self.assertEqual(panel.parse_command_line("   "), ("", ""))

    def test_user_id(self):
        self.assertEqual(panel.parse_user_id("<@123456789012345678>"), 123456789012345678)
        self.assertEqual(panel.parse_user_id("<@!123456789012345678>"), 123456789012345678)
        self.assertEqual(panel.parse_user_id(" 123456789012345678 "), 123456789012345678)
        for invalid in ("@Max", "12", "<@12a>", "123456789012345678 x"):
            self.assertIsNone(panel.parse_user_id(invalid))

    def test_find_channel(self):
        guild = FakeGuild([])
        allgemein = guild.text_channels[0]
        for text in ("#allgemein", "allgemein", "#ALLGEMEIN", "<#111111111111111111>", "111111111111111111"):
            self.assertIs(panel.find_channel(guild, text), allgemein)
        self.assertIsNone(panel.find_channel(guild, "#gibtsnicht"))
        self.assertIsNone(panel.find_channel(guild, "<#999999999999999999>"))

    def test_command_rights(self):
        self.assertEqual(panel.COMMANDS["help"].level, panel.COACH)
        for name in ("sync-ranks", "reminders", "purge", "cleanup-voice", "say"):
            self.assertEqual(panel.COMMANDS[name].level, panel.ADMIN)
        self.assertEqual(panel.COMMANDS["restart"].level, panel.OWNER)
        self.assertIsNotNone(panel.COMMANDS["say"].confirm)
        self.assertIsNotNone(panel.COMMANDS["restart"].confirm)


class ConsoleLogTests(unittest.TestCase):
    def test_redaction(self):
        webhook = "https://discord.com/api/webhooks/123456/abcDEF-123_xyz"
        self.assertEqual(panel.redact(f"Fehler bei {webhook}"), "Fehler bei https://discord.com/api/webhooks/123456/***")
        self.assertEqual(panel.redact("Authorization: Bearer abc.def"), "Authorization: Bearer ***")
        self.assertEqual(panel.redact("key HDEV-1234-abcd"), "key HDEV-***")
        self.assertEqual(
            panel.redact("Shard ID None has connected to Gateway (Session ID: 82ad6f28fff)."),
            "Shard ID None has connected to Gateway (Session ID: ***).",
        )

    def test_bot_token_from_env_is_redacted(self):
        token = env_value("DISCORD_BOT_TOKEN")
        if not token:
            self.skipTest("DISCORD_BOT_TOKEN ist weder gesetzt noch in .env")
        # Eigene Meldung, damit ein Fehlschlag den Token nicht ausgibt.
        self.assertTrue(token not in panel.redact(f"Token {token} läuft ab"), "Bot-Token wurde nicht geschwärzt")

    def test_buffer_size_and_handler(self):
        console = panel.ConsoleLog(size=3)
        logger = logging.getLogger("duality.test.console")
        handler = panel.ConsoleLogHandler(console)
        logger.addHandler(handler)
        logger.propagate = False
        try:
            for number in range(5):
                logger.warning("Eintrag %s", number)
            try:
                raise ValueError("kaputt")
            except ValueError:
                logger.exception("Fehler beim Test")
        finally:
            logger.removeHandler(handler)
        entries = console.snapshot()
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[-1]["level"], "ERROR")
        self.assertEqual(entries[-1]["message"], "Fehler beim Test (ValueError: kaputt)")
        self.assertNotIn("Traceback", entries[-1]["message"])
        self.assertEqual([entry["id"] for entry in entries], [4, 5, 6])

    def test_stdout_passthrough(self):
        console = panel.ConsoleLog()
        terminal = io.StringIO()
        stream = panel.ConsoleStdout(terminal, console)
        print("Server: Duality", file=stream)
        stream.write("  Rolle vorhanden: ")
        self.assertEqual(len(console.snapshot()), 1)
        stream.write("Owner\n\n")
        self.assertEqual(terminal.getvalue(), "Server: Duality\n  Rolle vorhanden: Owner\n\n")
        self.assertEqual([entry["message"] for entry in console.snapshot()], ["Server: Duality", "  Rolle vorhanden: Owner"])
        self.assertEqual(console.snapshot()[0]["name"], "ausgabe")


class ConsoleWebSocketTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hooks = make_hooks()
        self.panel = panel.Panel(CONFIG, self.hooks)
        self.client = TestClient(TestServer(self.panel.build_app()))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    async def connect(self, user_id: int, csrf: str | None = None, origin: str = ORIGIN):
        session, cookie = session_cookie(user_id)
        ws = await self.client.ws_connect(
            f"/ws?csrf={csrf if csrf is not None else session['csrf']}",
            headers={"Cookie": cookie, "Origin": origin},
        )
        hello = await ws.receive_json(timeout=2)
        self.assertEqual(hello["type"], "hello")
        self.addAsyncCleanup(ws.close)
        return session, ws, hello

    async def command(self, ws, line: str, confirm: bool = False, request_id: int = 1) -> dict:
        await ws.send_json({"type": "command", "id": request_id, "line": line, "confirm": confirm})
        while True:
            message = await ws.receive_json(timeout=3)
            if message["type"] == "result" and message["id"] == request_id:
                return message

    async def test_handshake_requires_session_csrf_and_origin(self):
        from aiohttp import WSServerHandshakeError

        session, cookie = session_cookie(COACH_ID)
        cases = [
            ({"Origin": ORIGIN}, session["csrf"], 401),
            ({"Cookie": cookie, "Origin": ORIGIN}, "falsch", 403),
            ({"Cookie": cookie, "Origin": "https://evil.example"}, session["csrf"], 403),
            ({"Cookie": cookie}, session["csrf"], 403),
        ]
        for headers, csrf, status in cases:
            with self.assertRaises(WSServerHandshakeError) as caught:
                await self.client.ws_connect(f"/ws?csrf={csrf}", headers=headers)
            self.assertEqual(caught.exception.status, status)

    async def test_hello_lists_only_allowed_commands_and_old_logs(self):
        logging.getLogger("duality").info("Vor dem Verbinden")
        _, _, hello = await self.connect(COACH_ID)
        self.assertEqual([command["name"] for command in hello["commands"]], ["help", "status", "tasks"])
        self.assertEqual(hello["status"]["guild"]["name"], "Duality Test")
        self.assertIn("Vor dem Verbinden", [entry["message"] for entry in hello["logs"]])
        _, _, hello = await self.connect(OWNER_ID)
        self.assertIn("restart", [command["name"] for command in hello["commands"]])

    async def test_live_log_and_print(self):
        _, ws, _ = await self.connect(COACH_ID)
        logging.getLogger("discord.gateway").warning("Gateway hustet")
        print("Ausgabe per print")
        received = []
        while len(received) < 2:
            message = await ws.receive_json(timeout=2)
            if message["type"] == "log":
                received.append(message["entry"])
        self.assertEqual((received[0]["level"], received[0]["name"], received[0]["message"]), ("WARNING", "discord.gateway", "Gateway hustet"))
        self.assertEqual((received[1]["name"], received[1]["message"]), ("ausgabe", "Ausgabe per print"))

    async def test_unknown_command_is_friendly(self):
        _, ws, _ = await self.connect(COACH_ID)
        result = await self.command(ws, "rm -rf /")
        self.assertFalse(result["ok"])
        self.assertIn("Unbekannter Befehl „rm“", result["text"])

    async def test_command_without_rights(self):
        _, ws, _ = await self.connect(COACH_ID)
        result = await self.command(ws, "sync-ranks")
        self.assertFalse(result["ok"])
        self.assertIn("mindestens die Rolle Admin", result["text"])
        self.hooks.sync_ranks.assert_not_awaited()
        _, ws, _ = await self.connect(ADMIN_ID)
        result = await self.command(ws, "restart", confirm=True)
        self.assertIn("mindestens die Rolle Owner", result["text"])
        self.hooks.restart.assert_not_awaited()

    async def test_help(self):
        _, ws, _ = await self.connect(ADMIN_ID)
        result = await self.command(ws, "help")
        self.assertTrue(result["ok"])
        self.assertIn("say #kanal <text>", result["text"])
        self.assertNotIn("restart", result["text"])
        self.hooks.audit.assert_not_awaited()

    async def test_controlling_commands_call_hooks_and_audit(self):
        _, ws, _ = await self.connect(ADMIN_ID)
        result = await self.command(ws, "reminders")
        self.assertEqual(result, {"type": "result", "id": 1, "ok": True, "text": "Termin-Erinnerungen geprüft, 1 gesendet."})
        audit_text = self.hooks.audit.await_args.args[1]
        self.assertIn("Panel-Befehl", audit_text)
        self.assertIn(f"<@{ADMIN_ID}>", audit_text)
        self.assertIn("2 vergangene Termine und 3 Bewerbungen", (await self.command(ws, "purge", request_id=2))["text"])
        self.assertIn("4 leere", (await self.command(ws, "cleanup-voice", request_id=3))["text"])
        result = await self.command(ws, f"sync-ranks <@{PLAYER_ID}>", request_id=4)
        self.assertEqual(result["text"], "Rang abgeglichen")
        guild, member = self.hooks.sync_ranks.await_args.args
        self.assertEqual(member.id, PLAYER_ID)
        self.assertIn("nicht auf dem Server", (await self.command(ws, "sync-ranks 999999999999999999", request_id=5))["text"])
        self.assertEqual(self.hooks.audit.await_count, 5)

    async def test_say_needs_confirmation_and_sends_without_pings(self):
        _, ws, _ = await self.connect(ADMIN_ID)
        channel = self.hooks.client.guild.text_channels[0]
        result = await self.command(ws, "say #allgemein Hallo @everyone")
        self.assertIn("bestätigt", result["text"])
        channel.send.assert_not_awaited()
        result = await self.command(ws, "say #allgemein Hallo @everyone", confirm=True, request_id=2)
        self.assertTrue(result["ok"])
        mentions = channel.send.await_args.kwargs["allowed_mentions"]
        self.assertFalse(mentions.everyone)
        self.assertFalse(mentions.users)
        self.assertFalse(mentions.roles)
        self.assertEqual(channel.send.await_args.args, ("Hallo @everyone",))
        result = await self.command(ws, "say #orga Hallo", confirm=True, request_id=3)
        self.assertIn("darf in #Orga nicht schreiben", result["text"])

    async def test_failing_command_hides_details(self):
        self.hooks.send_reminders.side_effect = RuntimeError("geheimes Detail")
        _, ws, _ = await self.connect(ADMIN_ID)
        result = await self.command(ws, "reminders")
        self.assertEqual(result["text"], "Der Befehl ist fehlgeschlagen. Details stehen im Log.")

    async def test_restart(self):
        _, ws, _ = await self.connect(OWNER_ID)
        result = await self.command(ws, "restart", confirm=True)
        self.assertTrue(result["ok"])
        for _ in range(30):
            if self.hooks.restart.await_count:
                break
            await asyncio.sleep(0.1)
        self.hooks.restart.assert_awaited_once()

    async def test_rate_limit(self):
        _, ws, _ = await self.connect(COACH_ID)
        for request_id in range(1, panel.COMMANDS_PER_MINUTE + 1):
            self.assertTrue((await self.command(ws, "help", request_id=request_id))["ok"])
        result = await self.command(ws, "help", request_id=99)
        self.assertIn("Zu viele Befehle", result["text"])

    async def test_role_removal_closes_console(self):
        _, ws, _ = await self.connect(COACH_ID)
        LEVELS.pop(COACH_ID)
        try:
            result = await self.command(ws, "help")
            self.assertIn("keinen Zugriff mehr", result["text"])
            message = await ws.receive(timeout=2)
            self.assertEqual(ws.close_code, panel.WS_CLOSE_NO_ACCESS)
        finally:
            LEVELS[COACH_ID] = panel.COACH

    async def test_logout_closes_console(self):
        session, ws, _ = await self.connect(COACH_ID)
        _, cookie = session_cookie(COACH_ID, session)
        response = await self.client.post(
            "/auth/logout", headers={"Cookie": cookie, "Origin": ORIGIN, "X-CSRF-Token": session["csrf"]}
        )
        self.assertEqual(response.status, 200)
        while not ws.closed:
            await ws.receive(timeout=2)
        self.assertEqual(ws.close_code, panel.WS_CLOSE_NO_ACCESS)

    async def test_shutdown_restores_stdout(self):
        self.assertIsInstance(sys.stdout, panel.ConsoleStdout)
        await self.client.close()
        self.assertNotIsInstance(sys.stdout, panel.ConsoleStdout)
        self.assertNotIn(self.panel.log_handler, logging.getLogger("duality").handlers)


class DashboardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.hooks = make_hooks()
        self.panel = panel.Panel(CONFIG, self.hooks)

    def test_status(self):
        status = self.panel.status()
        self.assertEqual(status["bot"]["latency_ms"], 42)
        self.assertTrue(status["bot"]["connected"])
        self.assertEqual(status["guild"], {"name": "Duality Test", "members": 4, "players": 2, "applicants": 0, "staff": 2})
        reminders = status["tasks"][1]
        self.assertEqual((reminders["running"], reminders["next_run"], reminders["last_error"]), (False, None, "RuntimeError: kaputt"))
        self.assertEqual(status["tasks"][0]["next_run"], int(datetime(2026, 10, 22, 11, 0, tzinfo=timezone.utc).timestamp()))
        self.assertFalse(status["henrikdev"]["enabled"])
        self.assertEqual(status["alerts"], {"channel": "#audit-log", "active": [], "recent": []})

    def test_status_lists_alerts(self):
        self.hooks.alerts.evaluate("gateway", "Bot offline", now=1000)
        self.hooks.alerts.history.append({"kind": "alert", "text": "Bot offline", "time": 1000, "delivered": "Webhook"})
        alerts = self.panel.status()["alerts"]
        self.assertEqual(alerts["active"], [{"text": "Bot offline", "since": 1000, "notified": True}])
        self.assertEqual(alerts["recent"][0]["delivered"], "Webhook")

    def test_status_while_disconnected(self):
        self.hooks.client.latency = float("nan")
        self.hooks.client.guild = None
        self.hooks.gateway["connected"] = False
        status = self.panel.status()
        self.assertIsNone(status["bot"]["latency_ms"])
        self.assertFalse(status["bot"]["connected"])
        self.assertIsNone(status["guild"])

    def test_henrikdev_rate_limit(self):
        api = SimpleNamespace(last_error="Status 429", last_error_at=100, failures_in_row=3, rate_limited_until=time.time() + 60)
        self.hooks.rank_client = lambda: api
        status = self.panel.status()["henrikdev"]
        self.assertTrue(status["enabled"] and status["rate_limited"])
        self.assertEqual(status["failures_in_row"], 3)

    async def test_status_and_tasks_commands(self):
        guild = self.hooks.client.guild
        context = panel.CommandContext(self.panel, guild, guild.get_member(COACH_ID), panel.COACH)
        text = await panel.command_status(context, "")
        self.assertIn("verbunden als Duality-Bot#0001 · Latenz 42 ms · läuft seit 3 h 0 min", text)
        self.assertIn("2 Spieler", text)
        self.assertIn("Gestoppte Tasks: Termin-Erinnerungen", text)
        text = await panel.command_tasks(context, "")
        self.assertIn("Termin-Erinnerungen: GESTOPPT", text)
        self.assertIn("RuntimeError: kaputt", text)
        self.assertEqual(panel.COMMANDS["status"].level, panel.COACH)
        self.assertFalse(panel.COMMANDS["tasks"].controlling)

    def test_format_helpers(self):
        self.assertEqual(panel.format_duration(59), "0 min")
        self.assertEqual(panel.format_duration(2 * 86400 + 5 * 3600), "2 d 5 h")
        self.assertEqual(panel.format_time(None), "–")
        self.assertEqual(panel.format_time(datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc).timestamp()), "01.07.2026 12:00")

    async def test_git_commit(self):
        commit = await panel.read_git_commit()
        self.assertRegex(commit, r"^([0-9a-f]{7,}|unbekannt)$")


class FakeResponse:
    def __init__(self, status: int, payload=None, headers=None):
        self.status = status
        self.payload = payload
        self.headers = headers or {}

    async def json(self):
        return self.payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    closed = False

    def __init__(self, *responses):
        self.responses = list(responses)

    def get(self, url, headers=None):
        return self.responses.pop(0)


class BotStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_tracked_loop_records_runs_and_errors(self):
        @main.tracked("test_loop")
        async def failing():
            raise ValueError("kaputt")

        with self.assertRaises(ValueError):
            await failing()
        status = main.LOOP_STATUS["test_loop"]
        self.assertIsNotNone(status["last_run"])
        self.assertEqual(status["last_error"], "ValueError: kaputt")

    async def test_henrikdev_failures_and_rate_limit(self):
        client = main.HenrikDevClient("schluessel")
        client.session = FakeSession(FakeResponse(500))
        with self.assertRaises(main.RankLookupError):
            await client.get("/x")
        self.assertEqual((client.failures_in_row, client.last_error), (1, "Status 500"))
        client.session = FakeSession(FakeResponse(429, headers={"Retry-After": "0"}), FakeResponse(200, {"ok": True}))
        self.assertEqual(await client.get("/x"), {"ok": True})
        self.assertEqual(client.failures_in_row, 0)
        self.assertGreater(client.rate_limited_until, 0)
        client.session = FakeSession(FakeResponse(404, {"errors": [{"code": 1}]}))
        with self.assertRaises(main.RankLookupError):
            await client.get("/x")
        self.assertEqual(client.failures_in_row, 0)

    def test_gateway_state(self):
        main.set_gateway_connected(True)
        self.assertTrue(main.GATEWAY_STATE["connected"])
        main.set_gateway_connected(False)
        self.assertFalse(main.GATEWAY_STATE["connected"])


if __name__ == "__main__":
    unittest.main()
