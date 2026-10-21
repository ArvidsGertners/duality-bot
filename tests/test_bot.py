"""Tests für Bot-Funktionen aus main.py ohne Discord-Verbindung: python -m unittest discover -s tests"""

import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from aiohttp import web
from aiohttp.test_utils import TestServer

import main


class FakeLoop:
    def __init__(self, running: bool = True, next_run: float | None = None):
        self.running = running
        self.next_iteration = datetime.fromtimestamp(next_run, timezone.utc) if next_run else None

    def is_running(self) -> bool:
        return self.running


class AlertDebounceTests(unittest.TestCase):
    def setUp(self):
        self.alerts = main.Alerts(repeat_seconds=1800)

    def test_alert_once_then_resolved(self):
        first = self.alerts.evaluate("gateway", "offline", now=0)
        self.assertEqual((first["kind"], first["text"]), ("alert", "offline"))
        self.assertIsNone(self.alerts.evaluate("gateway", "offline seit 6 min", now=60))
        self.assertIsNone(self.alerts.evaluate("gateway", "offline seit 40 min", now=2400))
        resolved = self.alerts.evaluate("gateway", None, now=2500)
        self.assertEqual((resolved["kind"], resolved["text"]), ("resolved", "offline"))
        self.assertIsNone(self.alerts.evaluate("gateway", None, now=2600))

    def test_flapping_within_thirty_minutes_stays_quiet(self):
        self.alerts.evaluate("henrikdev", "kaputt", now=0)
        self.alerts.evaluate("henrikdev", None, now=100)
        # Kommt innerhalb der Sperrfrist wieder: keine zweite Warnung, also auch keine Entwarnung.
        self.assertIsNone(self.alerts.evaluate("henrikdev", "kaputt", now=200))
        self.assertIsNone(self.alerts.evaluate("henrikdev", None, now=300))
        self.assertIsNone(self.alerts.evaluate("henrikdev", "kaputt", now=400))
        # Hält das Problem über die Frist hinaus an, wird es nachgemeldet.
        self.assertEqual(self.alerts.evaluate("henrikdev", "kaputt", now=1800)["kind"], "alert")

    def test_keys_are_independent(self):
        self.assertIsNotNone(self.alerts.evaluate("a", "x", now=0))
        self.assertIsNotNone(self.alerts.evaluate("b", "y", now=1))


class CollectProblemsTests(unittest.TestCase):
    def setUp(self):
        for patcher in (
            mock.patch.dict(main.LOOP_STATUS, clear=True),
            mock.patch.dict(main.GATEWAY_STATE, {"connected": True, "changed_at": 0}),
            mock.patch.object(main, "RANK_CLIENT", None),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_tasks(self):
        now = 10_000
        main.LOOP_STATUS.update({
            "failed": {"last_run": 9000, "last_error": "ValueError: kaputt"},
            "stalled": {"last_run": 9000},
            "fine": {"last_run": 9990},
        })
        loops = {
            "failed": ("Löschfristen", FakeLoop(running=False)),
            "stalled": ("Termin-Erinnerungen", FakeLoop(next_run=now - main.LOOP_STALL_SECONDS - 1)),
            "fine": ("Rang-Abgleich", FakeLoop(next_run=now + 60)),
            "never_started": ("Neu", FakeLoop(running=False)),
        }
        problems = main.collect_problems(loops, now)
        self.assertEqual(problems["task:failed"], "Hintergrund-Task „Löschfristen“ ist gestoppt (letzter Fehler: ValueError: kaputt)")
        self.assertIn("hängt", problems["task:stalled"])
        self.assertIsNone(problems["task:fine"])
        self.assertIsNone(problems["task:never_started"])

    def test_gateway_threshold(self):
        main.GATEWAY_STATE.update(connected=False, changed_at=1000)
        self.assertIsNone(main.collect_problems({}, 1000 + 4 * 60, disconnect_minutes=5)["gateway"])
        problem = main.collect_problems({}, 1000 + 7 * 60, disconnect_minutes=5)["gateway"]
        self.assertEqual(problem, "Der Bot ist seit 7 Minuten nicht mit Discord verbunden")

    def test_henrikdev_failures(self):
        client = SimpleNamespace(failures_in_row=2, last_error="Status 429")
        with mock.patch.object(main, "RANK_CLIENT", client):
            self.assertIsNone(main.collect_problems({}, 0)["henrikdev"])
            client.failures_in_row = 3
            self.assertIn("3× in Folge", main.collect_problems({}, 0)["henrikdev"])


class AlertDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_webhook_payload_has_no_mentions(self):
        received = []

        async def webhook(request):
            received.append(await request.json())
            return web.Response(status=204)

        app = web.Application()
        app.router.add_post("/api/webhooks/1/abc", webhook)
        server = TestServer(app)
        await server.start_server()
        self.addAsyncCleanup(server.close)
        alerts = main.Alerts(str(server.make_url("/api/webhooks/1/abc")))
        self.addAsyncCleanup(alerts.close)
        guild = SimpleNamespace(name="Duality")
        with mock.patch.object(main, "audit", mock.AsyncMock()) as audit:
            await alerts.check({"gateway": "Bot offline @everyone"}, guild, connected=True)
            audit.assert_not_awaited()
        self.assertEqual(received[0]["allowed_mentions"], {"parse": []})
        self.assertIn("Warnung", received[0]["content"])
        self.assertEqual(alerts.history[-1]["delivered"], "Webhook")

    async def test_without_webhook_waits_for_gateway(self):
        alerts = main.Alerts()
        guild = SimpleNamespace(name="Duality")
        with mock.patch.object(main, "audit", mock.AsyncMock()) as audit:
            await alerts.check({"gateway": "Bot offline"}, None, connected=False)
            audit.assert_not_awaited()
            self.assertEqual(alerts.history[-1]["delivered"], "ausstehend")
            await alerts.check({"gateway": None}, guild, connected=True)
            # Erst die nachgereichte Warnung, dann die Entwarnung.
            texts = [call.args[1] for call in audit.await_args_list]
            self.assertEqual(len(texts), 2)
            self.assertIn("Warnung", texts[0])
            self.assertIn("wieder in Ordnung", texts[1])
            self.assertEqual(alerts.history[0]["delivered"], "#audit-log (nachgereicht)")

    async def test_failing_webhook_falls_back_to_audit(self):
        alerts = main.Alerts("https://discord.com/api/webhooks/1/abc")
        guild = SimpleNamespace(name="Duality")
        with mock.patch.object(alerts, "post_webhook", mock.AsyncMock(return_value=False)), \
                mock.patch.object(main, "audit", mock.AsyncMock()) as audit:
            await alerts.check({"backup": "Backup fehlgeschlagen"}, guild, connected=True)
        audit.assert_awaited_once()
        self.assertEqual(alerts.history[-1]["delivered"], "#audit-log")

    def test_webhook_prefixes(self):
        self.assertTrue("https://discord.com/api/webhooks/1/x".startswith(main.WEBHOOK_PREFIXES))
        self.assertFalse("https://evil.example/api/webhooks/1/x".startswith(main.WEBHOOK_PREFIXES))


class EnvIntTests(unittest.TestCase):
    def test_defaults_and_bounds(self):
        with mock.patch.dict("os.environ", {"X_TEST": ""}):
            self.assertEqual(main.env_int("X_TEST", 5, 1, 10), 5)
        with mock.patch.dict("os.environ", {"X_TEST": "7"}):
            self.assertEqual(main.env_int("X_TEST", 5, 1, 10), 7)
        for invalid in ("0", "11", "abc"):
            with mock.patch.dict("os.environ", {"X_TEST": invalid}), mock.patch("builtins.print"):
                self.assertEqual(main.env_int("X_TEST", 5, 1, 10), 5)


if __name__ == "__main__":
    unittest.main()
