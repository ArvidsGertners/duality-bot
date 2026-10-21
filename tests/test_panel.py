"""Tests für das Web-Panel ohne Discord-Verbindung: python -m unittest discover -s tests"""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp.test_utils import TestClient, TestServer

import main
import panel


SECRET = b"x" * 40
ORIGIN = "http://localhost:8080"
CONFIG = panel.PanelConfig(client_id="123", client_secret="geheim", base_url=ORIGIN, secret_key=SECRET)


class FakeMember:
    def __init__(self, user_id: int, name: str):
        self.id = user_id
        self.display_name = name
        self.display_avatar = SimpleNamespace(url="https://cdn.discordapp.com/embed/avatars/0.png")
        self.bot = False

    def __str__(self) -> str:
        return self.display_name


class FakeGuild:
    def __init__(self, members):
        self.id = 1
        self.name = "Duality Test"
        self.members = {member.id: member for member in members}

    def get_member(self, user_id: int):
        return self.members.get(user_id)


class FakeClient:
    def __init__(self, guild):
        self.guild = guild
        self.ready = True

    def get_guild(self, guild_id: int):
        return self.guild

    def is_ready(self) -> bool:
        return self.ready


OWNER_ID, ADMIN_ID, COACH_ID, PLAYER_ID = 10, 20, 30, 40
LEVELS = {OWNER_ID: panel.OWNER, ADMIN_ID: panel.ADMIN, COACH_ID: panel.COACH}


def make_hooks(client=None) -> panel.PanelHooks:
    guild = FakeGuild([FakeMember(user_id, f"Person {user_id}") for user_id in (OWNER_ID, ADMIN_ID, COACH_ID, PLAYER_ID)])

    async def audit(guild, text, color=None):
        return None

    return panel.PanelHooks(
        client=client or FakeClient(guild),
        guild_id=1,
        staff_level=lambda member: LEVELS.get(member.id, 0),
        audit=audit,
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


if __name__ == "__main__":
    unittest.main()
