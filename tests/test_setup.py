"""Tests für das Server-Setup als Diff (B3) mit einem nachgebauten Server: python -m unittest discover -s tests"""

import asyncio
import contextlib
import io
import itertools
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import main


ids = itertools.count(1000)


def setUpModule():
    main.SETTINGS_CACHE = {}
    main.TEXT_CACHE = {}


class History:
    def __init__(self, messages):
        self.messages = list(messages)

    def __aiter__(self):
        self.iterator = iter(self.messages)
        return self

    async def __anext__(self):
        try:
            return next(self.iterator)
        except StopIteration:
            raise StopAsyncIteration from None


class Message:
    def __init__(self, channel, author, marker):
        self.id = next(ids)
        self.channel = channel
        self.author = author
        self.embeds = [main.discord.Embed().set_footer(text=marker)] if marker else []

    async def edit(self, **kwargs):
        if kwargs.get("embed") is not None:
            self.embeds = [kwargs["embed"]]

    async def delete(self):
        self.channel.messages.remove(self)


class Role:
    def __init__(self, name):
        self.id = next(ids)
        self.name = name
        self.members = []


class Member:
    def __init__(self, name):
        self.id = next(ids)
        self.name = name
        self.roles = []


class Channel:
    def __init__(self, guild, name, kind, category=None):
        self.id = next(ids)
        self.guild = guild
        self.name = name
        self.kind = kind
        self.category = category
        self.permissions_synced = False
        self.messages = []
        if category:
            category.channels.append(self)

    def history(self, limit=None):
        return History(reversed(self.messages))

    async def send(self, **kwargs):
        message = Message(self, self.guild.me, kwargs["embed"].footer.text if kwargs.get("embed") else None)
        self.messages.append(message)
        return message

    async def edit(self, **kwargs):
        self.name = kwargs.get("name", self.name)
        self.permissions_synced = kwargs.get("sync_permissions", self.permissions_synced)

    async def delete(self, reason=None):
        self.guild.channels.remove(self)
        if self.category:
            self.category.channels.remove(self)


class Category:
    def __init__(self, guild, name):
        self.id = next(ids)
        self.guild = guild
        self.name = name
        self.channels = []

    async def edit(self, **kwargs):
        self.name = kwargs.get("name", self.name)

    async def delete(self, reason=None):
        self.guild.categories.remove(self)


class Guild:
    def __init__(self):
        self.id = 777
        self.name = "Testserver"
        self.me = Member("Bot")
        self.default_role = Role("@everyone")
        self.roles = [self.default_role]
        self.categories = []
        self.channels = []
        self.rules_channel = None
        self.public_updates_channel = None

    @property
    def text_channels(self):
        return [channel for channel in self.channels if channel.kind == "text"]

    @property
    def voice_channels(self):
        return [channel for channel in self.channels if channel.kind == "voice"]

    def get_role(self, role_id):
        return next((role for role in self.roles if role.id == role_id), None)

    def get_channel(self, channel_id):
        return next((channel for channel in [*self.channels, *self.categories] if channel.id == channel_id), None)

    def add_category(self, name):
        category = Category(self, name)
        self.categories.append(category)
        return category

    def add_channel(self, name, kind="text", category=None):
        channel = Channel(self, name, kind, category)
        self.channels.append(channel)
        return channel

    async def create_role(self, name, reason=None):
        role = Role(name)
        self.roles.append(role)
        return role

    async def create_category(self, name, overwrites=None, reason=None):
        return self.add_category(name)

    async def create_text_channel(self, name, category=None, overwrites=None, reason=None):
        return self.add_channel(name, "text", category)

    async def create_voice_channel(self, name, category=None, reason=None):
        return self.add_channel(name, "voice", category)


def legacy_guild() -> Guild:
    guild = Guild()
    guild.add_category("📌 START HIER")
    tryouts = guild.add_category("🧪 TRYOUTS")
    guild.add_channel("tryout-chat", "text", tryouts)
    guild.add_channel("Tryout Voice", "voice", tryouts)
    guild.add_channel("announcements")
    guild.add_channel("Legion VC", "voice")
    return guild


def count(changes, action, kind=None):
    return sum(change.action == action and (kind is None or change.kind == kind) for change in changes)


class SetupDiffTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patcher = mock.patch.object(main, "DATA_PATH", Path(directory.name) / "bot.sqlite3")
        patcher.start()
        self.addCleanup(patcher.stop)
        main.initialize_storage()

    async def test_preview_of_empty_server(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            changes = await main.setup_guild(Guild(), False, echo=False)
        self.assertEqual(output.getvalue(), "")
        channels = sum(len(config["channels"]) for config in main.CATEGORIES)
        self.assertEqual(count(changes, "create", "role"), len(main.ROLE_NAMES))
        self.assertEqual(count(changes, "create", "category"), len(main.CATEGORIES))
        self.assertEqual(count(changes, "create", "channel"), channels)
        self.assertEqual(count(changes, "delete"), 0)

    async def test_printed_lines_match_the_diff(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            changes = await main.setup_guild(Guild(), False)
        lines = [line.strip() for line in output.getvalue().splitlines()]
        self.assertEqual(lines[0], "Server: Testserver (777)")
        self.assertIn("Rolle würde erstellt: Owner", lines)
        self.assertTrue(all(change.text in lines for change in changes if change.kind != "message"))

    async def test_legacy_server_counts_every_deletion(self):
        guild = legacy_guild()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            changes = await main.setup_guild(guild, False)
        # Die Vorschau gibt die Tryout-Kategorie in einer Zeile aus, der Diff zählt jede Löschung.
        self.assertIn("Alte Tryout-Kategorie und alle darin liegenden Kanäle würden entfernt", output.getvalue())
        deleted = {change.target for change in changes if change.action == "delete"}
        self.assertEqual(deleted, {"#announcements", "tryout-chat", "Tryout Voice", "🧪 TRYOUTS", "Legion VC"})
        self.assertIn("Kategorie würde umbenannt: 📌 START HIER → 👋 EINGANGSHALLE", [change.text for change in changes])
        self.assertFalse(any(change.action == "create" and change.target == "👋 EINGANGSHALLE" for change in changes))
        self.assertEqual(len(guild.channels), 4)

    async def test_apply_then_nothing_left(self):
        guild = legacy_guild()
        with contextlib.redirect_stdout(io.StringIO()):
            applied = await main.setup_guild(guild, True)
            again = await main.setup_guild(guild, False)
        self.assertEqual(count(applied, "delete"), 5)
        self.assertNotIn("Legion VC", [channel.name for channel in guild.channels])
        self.assertEqual(count(applied, "create", "message"), 4)
        self.assertEqual([change.action for change in again if change.action in ("create", "delete")], [])
        self.assertEqual(count(again, "change", "message"), 4)

    async def test_runs_never_overlap(self):
        events = []

        async def slow_setup(guild, apply, echo=True):
            events.append(("start", apply))
            await asyncio.sleep(0.05)
            events.append(("end", apply))
            return []

        with mock.patch.object(main, "setup_guild", slow_setup):
            await asyncio.gather(main.run_setup(None, False), main.run_setup(None, True))
        self.assertEqual([event[0] for event in events], ["start", "end", "start", "end"])


if __name__ == "__main__":
    unittest.main()
