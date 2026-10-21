"""Tests für Bot-Funktionen aus main.py ohne Discord-Verbindung: python -m unittest discover -s tests"""

import os
import sqlite3
import stat
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discord
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


class BackupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.backups = self.root / "backups"
        for patcher in (
            mock.patch.object(main, "DATA_PATH", self.root / "bot.sqlite3"),
            mock.patch.dict(main.BACKUP_STATE, {"last_success": None, "last_error": None, "last_error_at": None}),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        main.initialize_storage()
        main.save_ticket(4242, 1, 99)

    def test_backup_is_complete_and_private(self):
        path = main.create_backup(self.backups, datetime(2026, 10, 21, 4, 0, tzinfo=main.BERLIN))
        self.assertEqual(path.name, "duality-backup-2026-10-21_040000.sqlite3")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.backups.stat().st_mode), 0o700)
        with sqlite3.connect(path) as connection:
            self.assertEqual(connection.execute("SELECT thread_id FROM tickets").fetchall(), [(4242,)])
        self.assertEqual([entry.name for entry in self.backups.iterdir()], [path.name])

    def test_list_and_prune_only_touch_backups(self):
        self.backups.mkdir()
        now = datetime(2026, 10, 21, 4, 0, tzinfo=main.BERLIN)
        for days in (1, 13, 15, 40):
            (self.backups / f"duality-backup-{now - timedelta(days=days):%Y-%m-%d_%H%M%S}.sqlite3").write_bytes(b"x")
        for other in ("notizen.txt", "duality-backup-2020-01-01_000000.sqlite3.part", "duality-bot-2020-01-01.sqlite3"):
            (self.backups / other).write_bytes(b"x")
        self.assertEqual(len(main.list_backups(self.backups)), 4)
        removed = main.prune_backups(self.backups, 14, now)
        self.assertEqual(len(removed), 2)
        names = sorted(entry.name for entry in self.backups.iterdir())
        self.assertEqual(len(names), 5)
        self.assertIn("notizen.txt", names)
        self.assertEqual([backup["name"] for backup in main.list_backups(self.backups)][0][15:25], f"{now - timedelta(days=1):%Y-%m-%d}")

    async def test_failure_raises_alert_until_next_success(self):
        blocker = self.root / "kein-ordner"
        blocker.write_text("Datei statt Ordner")
        with self.assertRaises(OSError):
            await main.run_backup(blocker, 14)
        self.assertIsNotNone(main.BACKUP_STATE["last_error"])
        with mock.patch.object(main, "RANK_CLIENT", None):
            self.assertIn("Backup ist fehlgeschlagen", main.collect_problems({}, 0)["backup"])
            result = await main.run_backup(self.backups, 14)
            self.assertIsNone(main.collect_problems({}, 0)["backup"])
        self.assertEqual(result["removed"], 0)
        self.assertTrue((self.backups / result["name"]).exists())


class ListenerTests(unittest.TestCase):
    def test_failing_listener_does_not_stop_others(self):
        calls = []

        def broken(topic):
            raise RuntimeError("kaputt")

        with mock.patch.object(main, "STATE_LISTENERS", [broken, calls.append]), mock.patch.object(main.log, "exception"):
            main.notify_listeners("voice")
        self.assertEqual(calls, ["voice"])

    def test_temp_voice_owners(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(main, "DATA_PATH", Path(directory) / "bot.sqlite3"):
            main.initialize_storage()
            main.save_temp_voice(10, 1, 100)
            main.save_temp_voice(11, 2, 200)
            self.assertEqual(main.temp_voice_owners(1), {10: 100})


class ApplicationQueryTests(unittest.TestCase):
    def test_list_and_vote_counts(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(main, "DATA_PATH", Path(directory) / "bot.sqlite3"):
            main.initialize_storage()
            main.save_application(main.Application(1, 10, 100, "pending", 500))
            main.save_application(main.Application(2, 10, 101, "declined", 900, 7, 950))
            main.save_application(main.Application(3, 11, 102, "pending", 700))
            main.set_vote(1, 7, "yes")
            main.set_vote(1, 8, "no")
            main.set_vote(1, 9, "yes")
            main.set_vote(3, 7, "yes")
            self.assertEqual([application.message_id for application in main.list_applications(10)], [2, 1])
            self.assertEqual(main.application_vote_counts(10), {1: (2, 1)})


class ProfileQueryTests(unittest.TestCase):
    def test_list_profiles_per_guild(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(main, "DATA_PATH", Path(directory) / "bot.sqlite3"):
            main.initialize_storage()
            main.update_profile(1, 100, riot_name="Zoe", riot_tag="EUW")
            main.update_profile(1, 101, main_role="Sentinel")
            main.update_profile(2, 102, riot_name="Andere", riot_tag="EUW")
            profiles = {profile.user_id: profile for profile in main.list_profiles(1)}
            self.assertEqual(set(profiles), {100, 101})
            self.assertEqual(profiles[100].riot_id, "Zoe#EUW")
            self.assertIsNone(profiles[101].riot_id)


class TicketQueryTests(unittest.TestCase):
    def test_list_tickets(self):
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(main, "DATA_PATH", Path(directory) / "bot.sqlite3"):
            main.initialize_storage()
            with mock.patch.object(main.time, "time", return_value=1000):
                main.save_ticket(1, 10, 100)
            with mock.patch.object(main.time, "time", return_value=2000):
                main.save_ticket(2, 10, 101)
                main.close_ticket(1)
            main.save_ticket(3, 11, 102)
            self.assertEqual(main.list_tickets(10), [(2, 101, 2000, None), (1, 100, 1000, 2000)])


class EventFunctionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = mock.patch.object(main, "DATA_PATH", Path(directory.name) / "bot.sqlite3")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_old_database_gets_new_columns_without_data_loss(self):
        with sqlite3.connect(main.DATA_PATH) as connection:
            connection.execute(
                "CREATE TABLE scheduled_events (message_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, "
                "channel_id INTEGER NOT NULL, starts_at INTEGER NOT NULL)"
            )
            connection.execute("INSERT INTO scheduled_events VALUES (1, 10, 20, 3000)")
        main.initialize_storage()
        main.initialize_storage()
        self.assertEqual(main.get_scheduled_event(1), main.ScheduledEvent(1, 10, 20, 3000, None, None, None))

    def test_titles_reminders_and_cancel(self):
        main.initialize_storage()
        main.save_scheduled_event(1, 10, 20, 5000, "Scrim", "Infos")
        main.save_scheduled_event(1, 10, 20, 5000, "Anderer Titel aus Discord", None)
        self.assertEqual(main.get_scheduled_event(1).title, "Scrim")
        main.mark_event_reminder_sent(1, "24h")
        main.update_scheduled_event(1, "Scrim gegen X", "", 5000)
        self.assertTrue(main.event_reminder_was_sent(1, "24h"))
        main.update_scheduled_event(1, "Scrim gegen X", "", 6000)
        self.assertFalse(main.event_reminder_was_sent(1, "24h"))
        self.assertEqual(main.get_scheduled_event(1)[3:6], (6000, "Scrim gegen X", ""))
        main.write_rsvp(1, 99, "going")
        self.assertEqual(main.attendance(10, 99, 0, 10_000), (1, 1))
        self.assertEqual(len(main.get_upcoming_events(0)), 1)
        self.assertTrue(main.cancel_scheduled_event(1))
        self.assertFalse(main.cancel_scheduled_event(1))
        self.assertEqual(main.get_upcoming_events(0), [])
        self.assertEqual(main.attendance(10, 99, 0, 10_000), (0, 0))

    def test_fill_event_text_only_when_missing(self):
        main.initialize_storage()
        main.save_scheduled_event(1, 10, 20, 5000)
        main.save_scheduled_event(2, 10, 20, 5000, "Behalten")
        main.fill_event_text(1, "Nachgetragen", "x")
        main.fill_event_text(2, "Überschrieben", "y")
        self.assertEqual((main.get_scheduled_event(1).title, main.get_scheduled_event(2).title), ("Nachgetragen", "Behalten"))

    def test_validate_event(self):
        future = datetime.now(main.BERLIN) + timedelta(days=1)
        self.assertEqual(main.validate_event("  Training ", future, "  Maps üben "), ("Training", "Maps üben", int(future.timestamp())))
        cases = [
            ("Training", datetime.now(main.BERLIN) - timedelta(minutes=1), "", "Zukunft"),
            ("   ", future, "", "Titel"),
            ("x" * 101, future, "", "Titel"),
            ("Training", future, "x" * 1001, "Zusatzinfo"),
        ]
        for title, moment, details, expected in cases:
            with self.assertRaises(main.EventError) as caught:
                main.validate_event(title, moment, details)
            self.assertIn(expected, str(caught.exception))

    def test_event_embed(self):
        embed = main.event_embed("Training", "", 1792600000, {"going": [1, 2], "maybe": []})
        self.assertEqual(embed.description, "🕒 <t:1792600000:F> · <t:1792600000:R>")
        self.assertEqual([field.name for field in embed.fields], ["✅ Dabei (2)", "🤔 Vielleicht (0)", "❌ Kann nicht (0)"])
        self.assertEqual(embed.fields[0].value, "<@1>\n<@2>")
        self.assertEqual(embed.footer.text, f"{main.EVENT_MARKER}|1792600000")
        self.assertIn("\n\nMaps", main.event_embed("Training", "Maps", 1).description)

    async def test_publish_event(self):
        main.initialize_storage()
        channel = SimpleNamespace(id=20, send=mock.AsyncMock(return_value=SimpleNamespace(id=777)))
        guild = SimpleNamespace(id=10)
        with mock.patch.object(main, "find_text_channel", return_value=channel):
            message = await main.publish_event(guild, "Training", "Infos", 1792600000)
        self.assertEqual(message.id, 777)
        kwargs = channel.send.await_args.kwargs
        self.assertFalse(kwargs["allowed_mentions"].everyone)
        self.assertIsInstance(kwargs["view"], main.EventRSVPView)
        self.assertEqual(main.get_scheduled_event(777)[:6], (777, 10, 20, 1792600000, "Training", "Infos"))
        with mock.patch.object(main, "find_text_channel", return_value=None), self.assertRaises(main.EventError):
            await main.publish_event(guild, "Training", "", 1792600000)


class EventChangeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = mock.patch.object(main, "DATA_PATH", Path(directory.name) / "bot.sqlite3")
        patcher.start()
        self.addCleanup(patcher.stop)
        main.initialize_storage()
        self.start = int(time.time()) + 86400
        main.save_scheduled_event(500, 10, 20, self.start, "Training", "Maps")
        main.write_rsvp(500, 1, "going")
        main.write_rsvp(500, 2, "going")
        main.write_rsvp(500, 3, "declined")
        main.mark_event_reminder_sent(500, "24h")
        members = {1: SimpleNamespace(id=1, mention="<@1>"), 3: SimpleNamespace(id=3, mention="<@3>")}
        self.channel = mock.MagicMock(spec=discord.TextChannel)
        self.channel.send = mock.AsyncMock()
        self.channel.guild = SimpleNamespace(get_member=members.get)
        self.message = SimpleNamespace(edit=mock.AsyncMock(), channel=self.channel, embeds=[])
        self.channel.fetch_message = mock.AsyncMock(return_value=self.message)
        self.guild = SimpleNamespace(id=10, get_channel=lambda channel_id: self.channel if channel_id == 20 else None)

    async def test_move_resets_reminders_and_notifies_going(self):
        result = await main.edit_event(self.guild, 500, "Training neu", "", self.start + 3600, notify=True)
        self.assertIn("verschoben", result)
        self.assertIn("1 Zusagen", result)
        event = main.get_scheduled_event(500)
        self.assertEqual((event.title, event.starts_at), ("Training neu", self.start + 3600))
        self.assertFalse(main.event_reminder_was_sent(500, "24h"))
        embed = self.message.edit.await_args.kwargs["embed"]
        self.assertEqual(embed.footer.text, f"{main.EVENT_MARKER}|{self.start + 3600}")
        self.assertEqual(embed.fields[0].name, "✅ Dabei (2)")
        text = self.channel.send.await_args.args[0]
        mentions = self.channel.send.await_args.kwargs["allowed_mentions"]
        self.assertIn("<@1>", text)
        self.assertNotIn("<@3>", text)
        self.assertEqual([user.id for user in mentions.users], [1])
        self.assertFalse(mentions.everyone)

    async def test_same_time_keeps_reminders_and_stays_quiet(self):
        await main.edit_event(self.guild, 500, "Nur Titel", "Neue Infos", self.start, notify=True)
        self.assertTrue(main.event_reminder_was_sent(500, "24h"))
        self.channel.send.assert_not_awaited()

    async def test_discord_failure_leaves_database_unchanged(self):
        self.message.edit.side_effect = discord.HTTPException(mock.Mock(status=500, reason="x"), "kaputt")
        with self.assertRaises(discord.HTTPException):
            await main.edit_event(self.guild, 500, "Neu", "", self.start + 60)
        self.assertEqual(main.get_scheduled_event(500).title, "Training")

    async def test_guards(self):
        with self.assertRaises(main.EventError):
            await main.edit_event(self.guild, 999, "x", "", self.start)
        main.save_scheduled_event(501, 10, 20, int(time.time()) - 60, "Vorbei")
        with self.assertRaises(main.EventError):
            await main.edit_event(self.guild, 501, "x", "", self.start)
        self.channel.fetch_message.side_effect = discord.NotFound(mock.Mock(status=404, reason="x"), "weg")
        with self.assertRaises(main.EventError) as caught:
            await main.edit_event(self.guild, 500, "x", "", self.start)
        self.assertIn("gelöscht", str(caught.exception))

    async def test_cancel(self):
        result = await main.cancel_event(self.guild, 500, notify=False)
        self.assertIn("abgesagt", result)
        kwargs = self.message.edit.await_args.kwargs
        self.assertTrue(kwargs["embed"].title.startswith("❌ Abgesagt: Training"))
        self.assertTrue(all(item.disabled for item in kwargs["view"].children))
        self.assertIsNotNone(main.get_scheduled_event(500).cancelled_at)
        self.assertEqual(main.get_upcoming_events(0), [])
        self.channel.send.assert_not_awaited()
        with self.assertRaises(main.EventError):
            await main.cancel_event(self.guild, 500)
        with self.assertRaises(main.EventError):
            await main.edit_event(self.guild, 500, "x", "", self.start)

    def test_overviews(self):
        self.assertEqual([event.message_id for event in main.list_events(10)], [500])
        self.assertEqual(main.event_rsvp_overview(10), {500: {"going": [1, 2], "declined": [3]}})
        self.assertEqual(list(main.event_reminder_overview(10)[500]), ["24h"])
        self.assertEqual(main.event_rsvp_overview(11), {})


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
