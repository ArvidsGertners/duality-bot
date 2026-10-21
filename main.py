"""Set up Duality Mannheim's Discord and run its member onboarding bot.

Preview: python main.py
Apply and keep the bot online: python main.py --apply

The bot token can come from an environment variable or a hidden terminal
prompt. The guild ID defaults to Duality Mannheim. Never put a token in this
file. --apply creates missing items and updates managed permissions.
"""

import argparse
import asyncio
import io
import json
import math
import sqlite3
import getpass
import logging
import os
import time
import sys
import weakref
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple
from zoneinfo import ZoneInfo
from urllib.parse import quote, unquote, urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks


logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("discord").setLevel(logging.INFO)
log = logging.getLogger("duality")
log.setLevel(logging.INFO)


VALORANT_RANKS = [
    "Iron", "Bronze", "Silver", "Gold", "Platinum", "Diamond",
    "Ascendant", "Immortal", "Radiant",
]
UNIVERSITY_ROLES = [
    "DHBW Mannheim",
    "DHBW Andere",
    "Universität Mannheim",
    "Hochschule Mannheim",
    "Universität Heidelberg",
    "KIT Karlsruhe",
    "Hochschule Karlsruhe",
    "Hochschule Darmstadt",
    "TU Darmstadt",
    "Goethe-Universität Frankfurt",
    "Universität Stuttgart",
    "Universität Freiburg",
    "Andere Uni",
    "Keine Uni",
]
VALORANT_ROLES = ("Duelist", "Initiator", "Controller", "Sentinel")
ROLE_NAMES = [
    "Owner", "Admin", "Coach", "Spieler", "Bewerber",
    "Ping Valorant", "Ping Events", *VALORANT_RANKS, *UNIVERSITY_ROLES,
]
STAFF_ROLES = ("Owner", "Admin", "Coach")
TEAM_ROLES = (*STAFF_ROLES, "Spieler")
APPLICATION_RETENTION_DAYS = 30
APPLICATION_PENDING_PREFIX = "DUALITY_APPLICATION_PENDING|"
APPLICATION_DECISION_PREFIX = "DUALITY_APPLICATION_"
EVENT_MARKER = "DUALITY_EVENT_V1"
DATA_PATH = Path(__file__).with_name("duality_bot_data.sqlite3")
# Termine bleiben 90 Tage gespeichert, damit /profil die Teilnahmequote zeigen kann.
EVENT_RETENTION_DAYS = 90
APPLICATION_MIN_VOTES = 2
HENRIKDEV_API = "https://api.henrikdev.xyz"
# Fehlercode der API, wenn ein Account existiert, aber keine aktuellen Matches hat.
HENRIKDEV_NO_RECENT_MATCHES = 24
RANK_SYNC_DELAY_SECONDS = 3
TEMP_VOICE_CREATORS = {"➕ Neuer Voice": "🔊", "➕ Neuer Valorant-Voice": "🎯"}
BACKGROUND_TASKS: set[asyncio.Task] = set()
# Schwache Referenzen: Ein Lock verschwindet, sobald niemand mehr darauf wartet.
APPLICATION_LOCKS: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()
APPLICATION_SUBMIT_LOCKS: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()
RSVP_LOCKS: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()
MANAGED_IDS: dict[tuple[int, str, str], int] = {}

CATEGORIES = [
    {
        "name": "👋 EINGANGSHALLE",
        "roles": None,
        "channels": [
            ("text", "willkommen"),
            ("text", "ankündigungen"),
            ("text", "social-media"),
            ("text", "regeln"),
            ("text", "tickets"),
        ],
    },
    {
        "name": "💬 LOUNGE",
        "roles": None,
        "channels": [("text", "allgemein"), ("voice", "Community Voice"), ("voice", "➕ Neuer Voice")],
    },
    {
        "name": "🎯 VALORANT",
        "roles": None,
        "channels": [
            ("text", "valo-chat"),
            ("voice", "Valorant 1"),
            ("voice", "Valorant 2"),
            ("voice", "Valorant 3"),
            ("voice", "➕ Neuer Valorant-Voice"),
            ("text", "bewerbung"),
        ],
    },
    {
        "name": "📝 BEWERBER",
        "roles": ("Bewerber", *STAFF_ROLES),
        "channels": [("text", "bewerber-chat")],
    },
    {
        "name": "🎮 TEAM",
        "roles": TEAM_ROLES,
        "channels": [("text", "team-chat"), ("text", "spieltermine")],
    },
    {
        "name": "🔒 STAFF",
        "roles": STAFF_ROLES,
        "channels": [
            ("text", "bewerbungen"),
            ("text", "staff-chat"),
            ("text", "orga"),
            ("text", "audit-log"),
            ("voice", "Staff Voice"),
        ],
    },
]

APPLICATION_MARKER = "DUALITY_APPLICATION_V3"
RULES_MARKER = "DUALITY_RULES_V1"
WELCOME_MARKER = "DUALITY_WELCOME_V1"
TICKETS_MARKER = "DUALITY_TICKETS_V1"
DEFAULT_GUILD_ID = "1562056155558056027"
# Erhöhen, wenn sich die Rechte in CATEGORIES oder den *_overwrites-Funktionen
# ändern. Dann setzt das nächste --apply die verwalteten Rechte einmal neu;
# sonst bleiben manuelle Anpassungen der Admins bei Neustarts erhalten.
PERMISSIONS_VERSION = 2


@contextmanager
def open_db():
    # sqlite3's eigener Kontextmanager committet nur; schließen muss man selbst.
    connection = sqlite3.connect(DATA_PATH)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def initialize_storage() -> None:
    with open_db() as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS event_rsvps ("
            "message_id INTEGER NOT NULL, user_id INTEGER NOT NULL, choice TEXT NOT NULL, "
            "PRIMARY KEY (message_id, user_id))"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS scheduled_events ("
            "message_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, "
            "starts_at INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS event_reminders_sent ("
            "message_id INTEGER NOT NULL, reminder_key TEXT NOT NULL, sent_at INTEGER NOT NULL, "
            "PRIMARY KEY (message_id, reminder_key))"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS player_roster_names ("
            "guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, public_name TEXT NOT NULL, "
            "updated_at INTEGER NOT NULL, PRIMARY KEY (guild_id, user_id))"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS setup_migrations ("
            "migration_key TEXT PRIMARY KEY, applied_at INTEGER NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS managed_ids ("
            "guild_id INTEGER NOT NULL, kind TEXT NOT NULL, managed_key TEXT NOT NULL, "
            "object_id INTEGER NOT NULL, PRIMARY KEY (guild_id, kind, managed_key))"
        )
        # Nur der Status liegt hier; Name, Kurs und Tracker-Link bleiben in der
        # Discord-Nachricht und verschwinden mit ihr.
        connection.execute(
            "CREATE TABLE IF NOT EXISTS applications ("
            "message_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, applicant_id INTEGER NOT NULL, "
            "status TEXT NOT NULL, submitted_at INTEGER NOT NULL, decided_by INTEGER, decided_at INTEGER)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS application_votes ("
            "message_id INTEGER NOT NULL, staff_id INTEGER NOT NULL, vote TEXT NOT NULL, "
            "PRIMARY KEY (message_id, staff_id))"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS player_profiles ("
            "guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, riot_name TEXT, riot_tag TEXT, region TEXT, "
            "main_role TEXT, playable_roles TEXT, rank_tier TEXT, rank_synced_at INTEGER, "
            "updated_at INTEGER NOT NULL, PRIMARY KEY (guild_id, user_id))"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS tickets ("
            "thread_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, reporter_id INTEGER NOT NULL, "
            "created_at INTEGER NOT NULL, closed_at INTEGER)"
        )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS temp_voice_channels ("
            "channel_id INTEGER PRIMARY KEY, guild_id INTEGER NOT NULL, owner_id INTEGER NOT NULL, "
            "created_at INTEGER NOT NULL)"
        )
        rows = connection.execute("SELECT guild_id, kind, managed_key, object_id FROM managed_ids").fetchall()
    MANAGED_IDS.clear()
    MANAGED_IDS.update({(guild_id, kind, key): object_id for guild_id, kind, key, object_id in rows})


def migration_applied(guild_id: int, migration_key: str) -> bool:
    with open_db() as connection:
        row = connection.execute(
            "SELECT 1 FROM setup_migrations WHERE migration_key = ?",
            (f"{guild_id}:{migration_key}",),
        ).fetchone()
    return row is not None


def mark_migration_applied(guild_id: int, migration_key: str) -> None:
    with open_db() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO setup_migrations (migration_key, applied_at) VALUES (?, ?)",
            (f"{guild_id}:{migration_key}", int(time.time())),
        )


def adopt_unscoped_migrations(guild_id: int) -> None:
    # Ältere Versionen haben erledigte Setup-Schritte ohne Server-ID gespeichert.
    # Sie gehören zu dem Server, für den der Bot bisher eingerichtet wurde.
    with open_db() as connection:
        connection.execute(
            "UPDATE OR IGNORE setup_migrations SET migration_key = ? || ':' || migration_key "
            "WHERE migration_key NOT LIKE '%:%'",
            (str(guild_id),),
        )
        connection.execute("DELETE FROM setup_migrations WHERE migration_key NOT LIKE '%:%'")


class Application(NamedTuple):
    message_id: int
    guild_id: int
    applicant_id: int
    status: str
    submitted_at: int
    decided_by: int | None = None
    decided_at: int | None = None


APPLICATION_COLUMNS = "message_id, guild_id, applicant_id, status, submitted_at, decided_by, decided_at"


def save_application(application: Application) -> None:
    with open_db() as connection:
        connection.execute(
            f"INSERT OR IGNORE INTO applications ({APPLICATION_COLUMNS}) VALUES (?, ?, ?, ?, ?, ?, ?)",
            application,
        )


def get_application(message_id: int) -> Application | None:
    with open_db() as connection:
        row = connection.execute(
            f"SELECT {APPLICATION_COLUMNS} FROM applications WHERE message_id = ?", (message_id,)
        ).fetchone()
    return Application(*row) if row else None


def decide_application(message_id: int, status: str, decided_by: int | None, decided_at: int) -> bool:
    # Nur offene Bewerbungen ändern sich; so kann keine Entscheidung eine andere überschreiben.
    with open_db() as connection:
        cursor = connection.execute(
            "UPDATE applications SET status = ?, decided_by = ?, decided_at = ? "
            "WHERE message_id = ? AND status = 'pending'",
            (status, decided_by, decided_at, message_id),
        )
    return cursor.rowcount == 1


def find_pending_applications(guild_id: int, applicant_id: int | None = None) -> list[Application]:
    query = f"SELECT {APPLICATION_COLUMNS} FROM applications WHERE guild_id = ? AND status = 'pending'"
    parameters: tuple[int, ...] = (guild_id,)
    if applicant_id is not None:
        query += " AND applicant_id = ?"
        parameters += (applicant_id,)
    with open_db() as connection:
        return [Application(*row) for row in connection.execute(query, parameters)]


def expired_applications(guild_id: int, decided_before: int) -> list[Application]:
    with open_db() as connection:
        return [
            Application(*row)
            for row in connection.execute(
                f"SELECT {APPLICATION_COLUMNS} FROM applications "
                "WHERE guild_id = ? AND status IN ('declined', 'left') AND decided_at <= ?",
                (guild_id, decided_before),
            )
        ]


def delete_application(message_id: int) -> None:
    with open_db() as connection:
        connection.execute("DELETE FROM applications WHERE message_id = ?", (message_id,))
        connection.execute("DELETE FROM application_votes WHERE message_id = ?", (message_id,))


def applications_of(guild_id: int, applicant_id: int) -> list[Application]:
    with open_db() as connection:
        return [
            Application(*row)
            for row in connection.execute(
                f"SELECT {APPLICATION_COLUMNS} FROM applications WHERE guild_id = ? AND applicant_id = ?",
                (guild_id, applicant_id),
            )
        ]


def set_vote(message_id: int, staff_id: int, vote: str | None) -> None:
    with open_db() as connection:
        if vote is None:
            connection.execute(
                "DELETE FROM application_votes WHERE message_id = ? AND staff_id = ?", (message_id, staff_id)
            )
        else:
            connection.execute(
                "INSERT INTO application_votes (message_id, staff_id, vote) VALUES (?, ?, ?) "
                "ON CONFLICT(message_id, staff_id) DO UPDATE SET vote = excluded.vote",
                (message_id, staff_id, vote),
            )


def get_votes(message_id: int) -> dict[int, str]:
    with open_db() as connection:
        rows = connection.execute(
            "SELECT staff_id, vote FROM application_votes WHERE message_id = ? ORDER BY rowid", (message_id,)
        ).fetchall()
    return dict(rows)


PROFILE_FIELDS = ("riot_name", "riot_tag", "region", "main_role", "playable_roles", "rank_tier", "rank_synced_at")


class Profile(NamedTuple):
    guild_id: int
    user_id: int
    riot_name: str | None
    riot_tag: str | None
    region: str | None
    main_role: str | None
    playable_roles: str | None
    rank_tier: str | None
    rank_synced_at: int | None
    updated_at: int

    @property
    def riot_id(self) -> str | None:
        return f"{self.riot_name}#{self.riot_tag}" if self.riot_name and self.riot_tag else None


def update_profile(guild_id: int, user_id: int, **fields) -> None:
    unknown = set(fields) - set(PROFILE_FIELDS)
    if unknown:
        raise ValueError(f"Unbekannte Profilfelder: {unknown}")
    now = int(time.time())
    with open_db() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO player_profiles (guild_id, user_id, updated_at) VALUES (?, ?, ?)",
            (guild_id, user_id, now),
        )
        if fields:
            assignments = ", ".join(f"{name} = ?" for name in fields)
            connection.execute(
                f"UPDATE player_profiles SET {assignments}, updated_at = ? WHERE guild_id = ? AND user_id = ?",
                (*fields.values(), now, guild_id, user_id),
            )


def get_profile(guild_id: int, user_id: int) -> Profile | None:
    with open_db() as connection:
        row = connection.execute(
            f"SELECT guild_id, user_id, {', '.join(PROFILE_FIELDS)}, updated_at FROM player_profiles "
            "WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        ).fetchone()
    return Profile(*row) if row else None


def linked_profiles(guild_id: int) -> list[Profile]:
    with open_db() as connection:
        return [
            Profile(*row)
            for row in connection.execute(
                f"SELECT guild_id, user_id, {', '.join(PROFILE_FIELDS)}, updated_at FROM player_profiles "
                "WHERE guild_id = ? AND riot_name IS NOT NULL AND riot_tag IS NOT NULL",
                (guild_id,),
            )
        ]


def attendance(guild_id: int, user_id: int, since: int, until: int) -> tuple[int, int]:
    """(Zusagen, Termine) für vergangene Termine im Zeitraum."""
    with open_db() as connection:
        total = connection.execute(
            "SELECT COUNT(*) FROM scheduled_events WHERE guild_id = ? AND starts_at BETWEEN ? AND ?",
            (guild_id, since, until),
        ).fetchone()[0]
        going = connection.execute(
            "SELECT COUNT(*) FROM scheduled_events AS event JOIN event_rsvps AS rsvp "
            "ON rsvp.message_id = event.message_id WHERE event.guild_id = ? AND event.starts_at BETWEEN ? AND ? "
            "AND rsvp.user_id = ? AND rsvp.choice = 'going'",
            (guild_id, since, until, user_id),
        ).fetchone()[0]
    return going, total


def save_ticket(thread_id: int, guild_id: int, reporter_id: int) -> None:
    with open_db() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO tickets (thread_id, guild_id, reporter_id, created_at) VALUES (?, ?, ?, ?)",
            (thread_id, guild_id, reporter_id, int(time.time())),
        )


def get_ticket_reporter(thread_id: int) -> int | None:
    with open_db() as connection:
        row = connection.execute("SELECT reporter_id FROM tickets WHERE thread_id = ?", (thread_id,)).fetchone()
    return row[0] if row else None


def close_ticket(thread_id: int) -> bool:
    with open_db() as connection:
        cursor = connection.execute(
            "UPDATE tickets SET closed_at = ? WHERE thread_id = ? AND closed_at IS NULL",
            (int(time.time()), thread_id),
        )
    return cursor.rowcount == 1


def save_temp_voice(channel_id: int, guild_id: int, owner_id: int) -> None:
    with open_db() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO temp_voice_channels (channel_id, guild_id, owner_id, created_at) VALUES (?, ?, ?, ?)",
            (channel_id, guild_id, owner_id, int(time.time())),
        )


def temp_voice_ids(guild_id: int) -> set[int]:
    with open_db() as connection:
        return {
            channel_id
            for (channel_id,) in connection.execute(
                "SELECT channel_id FROM temp_voice_channels WHERE guild_id = ?", (guild_id,)
            )
        }


def delete_temp_voice(channel_id: int) -> None:
    with open_db() as connection:
        connection.execute("DELETE FROM temp_voice_channels WHERE channel_id = ?", (channel_id,))


def collect_user_data(guild_id: int, user_id: int) -> dict:
    """Alles, was der Bot in seiner Datenbank über eine Person speichert."""
    with open_db() as connection:
        connection.row_factory = sqlite3.Row

        def rows(query: str, parameters: tuple) -> list[dict]:
            return [dict(row) for row in connection.execute(query, parameters)]

        return {
            "profil": rows("SELECT * FROM player_profiles WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)),
            "teamname": rows("SELECT * FROM player_roster_names WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)),
            "bewerbungen": rows("SELECT * FROM applications WHERE guild_id = ? AND applicant_id = ?", (guild_id, user_id)),
            "terminantworten": rows(
                "SELECT rsvp.message_id, rsvp.choice, event.starts_at FROM event_rsvps AS rsvp "
                "LEFT JOIN scheduled_events AS event ON event.message_id = rsvp.message_id WHERE rsvp.user_id = ?",
                (user_id,),
            ),
            "staff_stimmen": rows("SELECT * FROM application_votes WHERE staff_id = ?", (user_id,)),
            "tickets": rows("SELECT * FROM tickets WHERE guild_id = ? AND reporter_id = ?", (guild_id, user_id)),
            "eigene_voice_channels": rows(
                "SELECT * FROM temp_voice_channels WHERE guild_id = ? AND owner_id = ?", (guild_id, user_id)
            ),
        }


def delete_user_data(guild_id: int, user_id: int) -> None:
    # Staff-Stimmen und Tickets bleiben als Nachweis für Entscheidungen und
    # Meldungen erhalten; Bewerbungen löscht der Aufrufer zusammen mit ihren Nachrichten.
    with open_db() as connection:
        connection.execute("DELETE FROM player_profiles WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        connection.execute("DELETE FROM player_roster_names WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        connection.execute("DELETE FROM event_rsvps WHERE user_id = ?", (user_id,))


def write_rsvp(message_id: int, user_id: int, choice: str) -> None:
    with open_db() as connection:
        connection.execute(
            "INSERT INTO event_rsvps (message_id, user_id, choice) VALUES (?, ?, ?) "
            "ON CONFLICT(message_id, user_id) DO UPDATE SET choice = excluded.choice",
            (message_id, user_id, choice),
        )


def get_rsvps(message_id: int) -> dict[str, list[int]]:
    with open_db() as connection:
        rows = connection.execute(
            "SELECT choice, user_id FROM event_rsvps WHERE message_id = ? ORDER BY user_id",
            (message_id,),
        ).fetchall()
    result = {"going": [], "maybe": [], "declined": []}
    for choice, user_id in rows:
        result.setdefault(choice, []).append(user_id)
    return result


def save_scheduled_event(message_id: int, guild_id: int, channel_id: int, starts_at: int) -> None:
    with open_db() as connection:
        connection.execute(
            "INSERT INTO scheduled_events (message_id, guild_id, channel_id, starts_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(message_id) DO UPDATE SET "
            "guild_id = excluded.guild_id, channel_id = excluded.channel_id, starts_at = excluded.starts_at",
            (message_id, guild_id, channel_id, starts_at),
        )


def get_upcoming_events(now: int) -> list[tuple[int, int, int, int]]:
    with open_db() as connection:
        return connection.execute(
            "SELECT message_id, guild_id, channel_id, starts_at FROM scheduled_events "
            "WHERE starts_at > ? ORDER BY starts_at",
            (now,),
        ).fetchall()


def event_reminder_was_sent(message_id: int, reminder_key: str) -> bool:
    with open_db() as connection:
        row = connection.execute(
            "SELECT 1 FROM event_reminders_sent WHERE message_id = ? AND reminder_key = ?",
            (message_id, reminder_key),
        ).fetchone()
    return row is not None


def mark_event_reminder_sent(message_id: int, reminder_key: str) -> None:
    with open_db() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO event_reminders_sent (message_id, reminder_key, sent_at) VALUES (?, ?, ?)",
            (message_id, reminder_key, int(time.time())),
        )


def delete_scheduled_event(message_id: int) -> None:
    with open_db() as connection:
        connection.execute("DELETE FROM scheduled_events WHERE message_id = ?", (message_id,))
        connection.execute("DELETE FROM event_reminders_sent WHERE message_id = ?", (message_id,))


def delete_past_events(started_before: int) -> int:
    with open_db() as connection:
        message_ids = [
            (message_id,)
            for (message_id,) in connection.execute(
                "SELECT message_id FROM scheduled_events WHERE starts_at <= ?", (started_before,)
            )
        ]
        for table in ("event_rsvps", "event_reminders_sent", "scheduled_events"):
            connection.executemany(f"DELETE FROM {table} WHERE message_id = ?", message_ids)
    return len(message_ids)


def save_roster_name(guild_id: int, user_id: int, public_name: str | None) -> None:
    with open_db() as connection:
        if public_name is None:
            connection.execute(
                "DELETE FROM player_roster_names WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            )
        else:
            connection.execute(
                "INSERT INTO player_roster_names (guild_id, user_id, public_name, updated_at) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(guild_id, user_id) DO UPDATE SET "
                "public_name = excluded.public_name, updated_at = excluded.updated_at",
                (guild_id, user_id, public_name, int(time.time())),
            )


def get_roster_names(guild_id: int) -> dict[int, str]:
    with open_db() as connection:
        rows = connection.execute(
            "SELECT user_id, public_name FROM player_roster_names WHERE guild_id = ?",
            (guild_id,),
        ).fetchall()
    return {user_id: public_name for user_id, public_name in rows}


# Rollen und Kanäle werden über ihre gespeicherte ID gefunden, damit ein
# Umbenennen in Discord nichts kaputt macht. Der Name ist nur der Rückfall,
# bis die ID einmal gespeichert ist.
def remember_id(guild_id: int, kind: str, key: str, object_id: int) -> None:
    if MANAGED_IDS.get((guild_id, kind, key)) == object_id:
        return
    with open_db() as connection:
        connection.execute(
            "INSERT INTO managed_ids (guild_id, kind, managed_key, object_id) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(guild_id, kind, managed_key) DO UPDATE SET object_id = excluded.object_id",
            (guild_id, kind, key, object_id),
        )
    MANAGED_IDS[(guild_id, kind, key)] = object_id


def stored_role(guild: discord.Guild, name: str) -> discord.Role | None:
    role_id = MANAGED_IDS.get((guild.id, "role", name))
    return guild.get_role(role_id) if role_id else None


def stored_channel(guild: discord.Guild, kind: str, name: str):
    channel_id = MANAGED_IDS.get((guild.id, kind, name))
    return guild.get_channel(channel_id) if channel_id else None


def find_role(guild: discord.Guild, name: str) -> discord.Role | None:
    role = stored_role(guild, name)
    if role is None:
        role = discord.utils.get(guild.roles, name=name)
        if role:
            remember_id(guild.id, "role", name, role.id)
    return role


def find_text_channel(guild: discord.Guild, name: str) -> discord.TextChannel | None:
    channel = stored_channel(guild, "channel", name)
    if channel is None:
        channel = discord.utils.get(guild.text_channels, name=name)
        if channel:
            remember_id(guild.id, "channel", name, channel.id)
    return channel


def managed_roles(guild: discord.Guild) -> dict[str, discord.Role]:
    return {name: role for name in ROLE_NAMES if (role := find_role(guild, name))}


def member_has_role(member: discord.Member, name: str) -> bool:
    role = find_role(member.guild, name)
    return role is not None and role in member.roles


def is_staff(member: discord.Member) -> bool:
    return any(member_has_role(member, name) for name in STAFF_ROLES)


def staff_level(member: discord.Member) -> int:
    """Rechte-Stufe fürs Panel: Owner 3, Admin 2, Coach 1, sonst 0 (STAFF_ROLES ist absteigend sortiert)."""
    return max(
        (len(STAFF_ROLES) - index for index, name in enumerate(STAFF_ROLES) if member_has_role(member, name)),
        default=0,
    )


def category_overwrites(guild: discord.Guild, roles: dict[str, discord.Role], allowed_names):
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=allowed_names is None)
    }
    if allowed_names is not None:
        for role_name in allowed_names:
            role = roles.get(role_name)
            if role:
                overwrites[role] = discord.PermissionOverwrite(
                    view_channel=True,
                    send_messages=True,
                    read_message_history=True,
                    connect=True,
                    speak=True,
                )
    if allowed_names is not None and guild.me:
        overwrites[guild.me] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            embed_links=True,
            manage_messages=True,
        )
    return overwrites


def application_overwrites(guild: discord.Guild, roles: dict[str, discord.Role]):
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=False,
            read_message_history=True,
        )
    }
    for role_name in ("Bewerber", "Spieler"):
        role = roles.get(role_name)
        if role:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=False,
                send_messages=False,
                read_message_history=False,
            )
    for role_name in STAFF_ROLES:
        role = roles.get(role_name)
        if role:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            )
    # Discord verrechnet alle Rollen-Overwrites gemeinsam und Erlauben gewinnt
    # dabei gegen Verbieten: Staff mit zusätzlich Spieler/Bewerber sieht den Kanal.
    if guild.me:
        overwrites[guild.me] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            embed_links=True,
        )
    return overwrites


def rules_overwrites(guild: discord.Guild, roles: dict[str, discord.Role]):
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=False,
            read_message_history=True,
        )
    }
    for role_name in STAFF_ROLES:
        role = roles.get(role_name)
        if role:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
            )
    if guild.me:
        overwrites[guild.me] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            embed_links=True,
        )
    return overwrites


def tickets_overwrites(guild: discord.Guild, roles: dict[str, discord.Role]):
    # Alle sehen #tickets, schreiben aber nur in ihren eigenen privaten Ticket-Threads.
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=True,
            send_messages=False,
            read_message_history=True,
            create_public_threads=False,
            create_private_threads=False,
            send_messages_in_threads=True,
        )
    }
    for role_name in STAFF_ROLES:
        role = roles.get(role_name)
        if role:
            overwrites[role] = discord.PermissionOverwrite(view_channel=True, send_messages=True)
    if guild.me:
        overwrites[guild.me] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            embed_links=True,
            create_private_threads=True,
            send_messages_in_threads=True,
        )
    return overwrites


SPECIAL_CHANNEL_OVERWRITES = {
    "bewerbung": application_overwrites,
    "regeln": rules_overwrites,
    "tickets": tickets_overwrites,
}


ERROR_TEXT = "Das hat leider nicht geklappt. Bitte versuche es später erneut oder wende dich direkt an den Staff."


async def send_error_response(interaction: discord.Interaction, text: str = ERROR_TEXT) -> None:
    try:
        if interaction.response.is_done():
            await interaction.followup.send(text, ephemeral=True)
        else:
            await interaction.response.send_message(text, ephemeral=True)
    except discord.HTTPException:
        log.exception("Fehlermeldung konnte nicht gesendet werden")


class DualityView(discord.ui.View):
    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        log.error("Fehler in %s (%s)", type(self).__name__, item, exc_info=error)
        await send_error_response(interaction)


async def set_member_roles(
    member: discord.Member,
    *,
    add: Sequence[discord.Role] = (),
    remove: Sequence[discord.Role] = (),
    reason: str,
) -> None:
    # Ein einziger API-Aufruf, damit kein halber Rollenwechsel übrig bleibt.
    remove_ids = {role.id for role in remove}
    new_roles = [role for role in member.roles if not role.is_default() and role.id not in remove_ids]
    new_roles.extend(role for role in add if role not in new_roles)
    await member.edit(roles=new_roles, reason=reason)


def has_member_choice(member: discord.Member) -> bool:
    return member_has_role(member, "Bewerber") or member_has_role(member, "Spieler")


def is_tracker_profile(value: str) -> bool:
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and (host == "tracker.gg" or host.endswith(".tracker.gg"))
        and parsed.path.startswith("/valorant/profile/riot/")
    )


def parse_riot_id(text: str) -> tuple[str, str] | None:
    name, separator, tag = text.strip().rpartition("#")
    name, tag = name.strip(), tag.strip()
    if not separator or not 3 <= len(name) <= 16 or not 3 <= len(tag) <= 5:
        return None
    return name, tag


def riot_id_from_tracker(url: str) -> tuple[str, str] | None:
    # tracker.gg/valorant/profile/riot/Name%23Tag/overview
    path = unquote(urlparse(url.strip()).path)
    prefix = "/valorant/profile/riot/"
    if not path.startswith(prefix):
        return None
    return parse_riot_id(path.removeprefix(prefix).split("/", 1)[0])


def rank_role_name(tier_name: str) -> str | None:
    """'Diamond 2' → 'Diamond'; 'Unrated' und Unbekanntes → None."""
    base = tier_name.split(" ", 1)[0].capitalize()
    return base if base in VALORANT_RANKS else None


class RankLookupError(Exception):
    pass


class HenrikDevClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.session: aiohttp.ClientSession | None = None

    async def get(self, path: str) -> dict:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        for attempt in range(2):
            async with self.session.get(HENRIKDEV_API + path, headers={"Authorization": self.api_key}) as response:
                if response.status == 429 and attempt == 0:
                    try:
                        wait = float(response.headers.get("Retry-After") or response.headers.get("X-RateLimit-Reset") or 60)
                    except ValueError:
                        wait = 60
                    await asyncio.sleep(min(wait, 120))
                    continue
                if response.status == 404:
                    try:
                        error_code = (await response.json())["errors"][0]["code"]
                    except (aiohttp.ContentTypeError, KeyError, IndexError, TypeError, ValueError):
                        error_code = None
                    if error_code == HENRIKDEV_NO_RECENT_MATCHES:
                        raise RankLookupError(
                            "Für diese Riot-ID gibt es keine aktuellen Matches. Spiel eine Runde "
                            "(z. B. Deathmatch) und versuche es danach erneut."
                        )
                    raise RankLookupError("Diese Riot-ID wurde nicht gefunden.")
                if response.status != 200:
                    raise RankLookupError(f"Die HenrikDev-API antwortet gerade mit Status {response.status}.")
                return await response.json()
        raise RankLookupError("Die HenrikDev-API ist gerade ausgelastet. Bitte versuche es später erneut.")

    async def current_rank(self, name: str, tag: str, region: str | None = None) -> tuple[str, str]:
        """Liefert (Rangname wie 'Gold 2', Region)."""
        riot_name, riot_tag = quote(name, safe=""), quote(tag, safe="")
        try:
            if not region:
                account = await self.get(f"/valorant/v2/account/{riot_name}/{riot_tag}")
                region = account["data"]["region"]
            mmr = await self.get(f"/valorant/v3/mmr/{region}/pc/{riot_name}/{riot_tag}")
            return mmr["data"]["current"]["tier"]["name"], region
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            raise RankLookupError("Die HenrikDev-API ist gerade nicht erreichbar.") from error
        except (KeyError, TypeError) as error:
            raise RankLookupError("Die HenrikDev-API hat eine unerwartete Antwort geschickt.") from error

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()


RANK_CLIENT: HenrikDevClient | None = None


async def set_exclusive_role(member: discord.Member, role_names, selected_role: discord.Role | None, reason: str) -> bool:
    """Setzt höchstens eine Rolle aus einer Gruppe. Gibt zurück, ob sich etwas geändert hat."""
    group_ids = {role.id for name in role_names if (role := find_role(member.guild, name))}
    previous_roles = [role for role in member.roles if role.id in group_ids and role != selected_role]
    add_roles = [selected_role] if selected_role and selected_role not in member.roles else []
    if not add_roles and not previous_roles:
        return False
    await set_member_roles(member, add=add_roles, remove=previous_roles, reason=reason)
    return True


async def sync_member_rank(member: discord.Member, profile: Profile) -> str:
    """Holt den aktuellen Rang über die Riot-ID und setzt die passende Rangrolle."""
    if RANK_CLIENT is None:
        raise RankLookupError("Die automatische Rangabfrage ist nicht eingerichtet (HENRIKDEV_API_KEY fehlt).")
    tier_name, region = await RANK_CLIENT.current_rank(profile.riot_name, profile.riot_tag, profile.region)
    update_profile(member.guild.id, member.id, region=region, rank_tier=tier_name, rank_synced_at=int(time.time()))
    role_name = rank_role_name(tier_name)
    selected_role = find_role(member.guild, role_name) if role_name else None
    await set_exclusive_role(member, VALORANT_RANKS, selected_role, "Valorant-Rang automatisch über Riot-ID aktualisiert")
    return tier_name


def run_in_background(coroutine) -> None:
    task = asyncio.create_task(coroutine)
    BACKGROUND_TASKS.add(task)
    task.add_done_callback(BACKGROUND_TASKS.discard)


async def sync_rank_quietly(member: discord.Member) -> None:
    profile = get_profile(member.guild.id, member.id)
    if profile is None or profile.riot_id is None or RANK_CLIENT is None:
        return
    try:
        await sync_member_rank(member, profile)
    except (RankLookupError, discord.HTTPException) as error:
        log.warning("Rang von %s konnte nicht aktualisiert werden: %s", member.id, error)


class TrackerLinkModal(discord.ui.Modal, title="Mitspielen – Tracker.gg-Profil"):
    real_name = discord.ui.TextInput(
        label="Wie dürfen wir dich nennen?",
        placeholder="Dein bevorzugter Name oder Nickname",
        style=discord.TextStyle.short,
        max_length=80,
        required=True,
    )
    course = discord.ui.TextInput(
        label="Kurs / Ausbildung (optional)",
        placeholder="Nur wenn du das mit uns teilen möchtest",
        style=discord.TextStyle.short,
        max_length=100,
        required=False,
    )
    tracker_url = discord.ui.TextInput(
        label="Dein Valorant Tracker.gg-Profillink",
        placeholder="https://tracker.gg/valorant/profile/riot/...",
        style=discord.TextStyle.short,
        max_length=300,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Das geht nur direkt auf dem Duality-Server.", ephemeral=True)
            return
        member = interaction.user
        real_name = str(self.real_name).strip()
        if not real_name:
            await interaction.response.send_message("Bitte gib an, wie wir dich nennen dürfen.", ephemeral=True)
            return
        if not is_tracker_profile(str(self.tracker_url)):
            await interaction.response.send_message(
                "Bitte füge einen HTTPS-Link zu deinem Valorant-Profil auf tracker.gg ein.", ephemeral=True
            )
            return

        if has_member_choice(member):
            await interaction.response.send_message("Du hast bereits eine Zugangsoption gewählt.", ephemeral=True)
            return

        draft = {
            "guild_id": interaction.guild.id,
            "member_id": member.id,
            "real_name": real_name,
            "course": str(self.course).strip(),
            "tracker_url": str(self.tracker_url).strip(),
        }
        view = ApplicationSelectionView(member.id, draft)
        await interaction.response.send_message(view.step_text(), view=view, ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.error("Fehler im Bewerbungsformular", exc_info=error)
        await send_error_response(interaction)


class ApplicationChoiceSelect(discord.ui.Select):
    def __init__(self, *, state_key: str, placeholder: str, options, minimum: int, maximum: int, row: int):
        self.state_key = state_key
        super().__init__(
            placeholder=placeholder,
            options=options,
            min_values=minimum,
            max_values=maximum,
            custom_id=f"duality:application:choose:{state_key}",
            row=row,
        )

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if not isinstance(view, ApplicationSelectionView):
            await interaction.response.send_message("Diese Bewerbungsauswahl ist abgelaufen.", ephemeral=True)
            return
        if not view.owns(interaction):
            await interaction.response.send_message("Diese Bewerbungsangaben gehören einem anderen Mitglied.", ephemeral=True)
            return
        view.selections[self.state_key] = list(self.values)
        await interaction.response.defer()


AGE_CONFIRMED = "16+"
AGE_TOO_YOUNG = "unter-16"
# Discord erlaubt 25 Optionen pro Dropdown und 5 Zeilen pro Nachricht. In
# Schritt 2 braucht die Button-Zeile eine davon, bleiben 4 Dropdowns für Spielzeiten.
MAX_OPTIONS_PER_SELECT = 25
MAX_AVAILABILITY_SELECTS = 4


class ApplicationSelectionView(DualityView):
    DAYS = (
        ("Mittwoch", "Mi"),
        ("Donnerstag", "Do"),
        ("Freitag", "Fr"),
        ("Samstag", "Sa"),
        ("Sonntag", "So"),
    )
    TIME_WINDOWS = ("19:00–20:00", "19:00–21:00", "19:00–22:00", "20:00–23:00", "ab 19:00 flexibel")

    def __init__(self, member_id: int, draft: dict[str, int | str]):
        super().__init__(timeout=600)
        self.member_id = member_id
        self.draft = draft
        self.selections: dict[str, list[str]] = {}
        self.step = 1
        self.show_profile_step()

    @classmethod
    def availability_slots(cls) -> list[tuple[str, str, str]]:
        return [
            (f"{short_day} · {window}", f"{day}|{window}", f"Typische Spielzeit am {day}")
            for day, short_day in cls.DAYS
            for window in cls.TIME_WINDOWS
        ]

    def owns(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.member_id and interaction.guild_id == self.draft.get("guild_id")

    def step_text(self) -> str:
        if self.step == 1:
            return (
                "**Schritt 1 von 2:** Bestätige dein Alter, wähle deine Main-Rolle und markiere alle Rollen, "
                "die du im Team spielen kannst, einschließlich deiner Main-Rolle. Deine Angaben bleiben privat. "
                "Die Auswahl läuft nach 10 Minuten ohne Klick ab."
            )
        return (
            "**Schritt 2 von 2:** Wähle für jeden passenden Abend deine typische Spielzeit aus. "
            "Du kannst mehrere Abende auswählen."
        )

    def options(self, state_key: str, entries) -> list[discord.SelectOption]:
        # Beim Zurückblättern bleibt die bisherige Auswahl sichtbar.
        chosen = set(self.selections.get(state_key, ()))
        return [
            discord.SelectOption(label=label, value=value, description=description, default=value in chosen)
            for label, value, description in entries
        ]

    def availability(self) -> list[str]:
        return [
            value
            for key, values in self.selections.items()
            if key.startswith("availability:")
            for value in values
        ]

    def add_button(self, label: str, style: discord.ButtonStyle, row: int, handler) -> None:
        button = discord.ui.Button(label=label, style=style, row=row)
        button.callback = handler
        self.add_item(button)

    def show_profile_step(self) -> None:
        self.step = 1
        self.clear_items()
        self.add_item(ApplicationChoiceSelect(
            state_key="age",
            placeholder="Bist du mindestens 16 Jahre alt?",
            options=self.options("age", [
                ("Ja, ich bin mindestens 16", AGE_CONFIRMED, None),
                ("Nein, ich bin jünger als 16", AGE_TOO_YOUNG, None),
            ]),
            minimum=1,
            maximum=1,
            row=0,
        ))
        self.add_item(ApplicationChoiceSelect(
            state_key="main_role",
            placeholder="Deine Main-Rolle auswählen",
            options=self.options("main_role", [(role, role, None) for role in VALORANT_ROLES]),
            minimum=1,
            maximum=1,
            row=1,
        ))
        self.add_item(ApplicationChoiceSelect(
            state_key="playable_roles",
            placeholder="Welche Rollen kannst du spielen? (Main-Rolle mit auswählen)",
            options=self.options("playable_roles", [(role, role, None) for role in VALORANT_ROLES]),
            minimum=1,
            maximum=len(VALORANT_ROLES),
            row=2,
        ))
        self.add_button("Weiter zu den Spielzeiten", discord.ButtonStyle.primary, 3, self.go_to_availability)

    def show_availability_step(self) -> None:
        self.step = 2
        self.clear_items()
        slots = self.availability_slots()
        chunks = [slots[start:start + MAX_OPTIONS_PER_SELECT] for start in range(0, len(slots), MAX_OPTIONS_PER_SELECT)]
        for index, chunk in enumerate(chunks):
            placeholder = "Spielzeiten auswählen (Mehrfachauswahl)"
            if len(chunks) > 1:
                first_day, last_day = chunk[0][0].split(" · ")[0], chunk[-1][0].split(" · ")[0]
                placeholder = f"Spielzeiten {first_day}–{last_day} auswählen (Mehrfachauswahl)"
            self.add_item(ApplicationChoiceSelect(
                state_key=f"availability:{index}",
                placeholder=placeholder,
                options=self.options(f"availability:{index}", chunk),
                minimum=0,
                maximum=len(chunk),
                row=index,
            ))
        self.add_button("Zurück", discord.ButtonStyle.secondary, len(chunks), self.go_to_profile)
        self.add_button("Bewerbung abschicken", discord.ButtonStyle.success, len(chunks), self.submit_application)

    async def on_timeout(self):
        self.draft.clear()

    async def reject_foreign(self, interaction: discord.Interaction) -> bool:
        if interaction.guild and isinstance(interaction.user, discord.Member) and self.owns(interaction):
            return False
        await interaction.response.send_message("Diese Bewerbung gehört einem anderen Mitglied oder Server.", ephemeral=True)
        return True

    async def go_to_availability(self, interaction: discord.Interaction):
        if await self.reject_foreign(interaction):
            return
        age = self.selections.get("age", [])
        main_role = self.selections.get("main_role", [])
        if age == [AGE_TOO_YOUNG]:
            self.draft.clear()
            self.stop()
            await interaction.response.edit_message(
                content="Bewerbungen ins Valorant-Team sind erst ab 16 Jahren möglich. "
                "In der Community bist du trotzdem herzlich willkommen!",
                view=None,
            )
            return
        if age != [AGE_CONFIRMED] or not main_role:
            await interaction.response.send_message(
                "Bitte beantworte die Altersfrage und wähle deine Main-Rolle aus.", ephemeral=True
            )
            return
        if main_role[0] not in self.selections.get("playable_roles", []):
            await interaction.response.send_message(
                "Bitte wähle bei den spielbaren Rollen auch deine Main-Rolle aus.", ephemeral=True
            )
            return
        self.show_availability_step()
        await interaction.response.edit_message(content=self.step_text(), view=self)

    async def go_to_profile(self, interaction: discord.Interaction):
        if await self.reject_foreign(interaction):
            return
        self.show_profile_step()
        await interaction.response.edit_message(content=self.step_text(), view=self)

    async def submit_application(self, interaction: discord.Interaction):
        if await self.reject_foreign(interaction):
            return

        member = interaction.user
        if has_member_choice(member):
            await interaction.response.send_message("Du hast bereits eine Zugangsoption gewählt.", ephemeral=True)
            return
        availability = self.availability()
        if not availability:
            await interaction.response.send_message("Bitte wähle mindestens eine Spielzeit aus.", ephemeral=True)
            return
        main_role = self.selections["main_role"][0]
        playable_roles = self.selections["playable_roles"]
        applicant_role = find_role(interaction.guild, "Bewerber")
        staff_channel = find_text_channel(interaction.guild, "bewerbungen")
        applicant_chat = find_text_channel(interaction.guild, "bewerber-chat")
        if not applicant_role or not staff_channel or not applicant_chat:
            await interaction.response.send_message(
                "Die Bewerbungskanäle sind noch nicht eingerichtet. Bitte melde dich beim Staff.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        submit_lock = APPLICATION_SUBMIT_LOCKS.setdefault(member.id, asyncio.Lock())
        async with submit_lock:
            try:
                member = await interaction.guild.fetch_member(member.id)
            except discord.HTTPException:
                await interaction.followup.send(
                    "Dein Mitgliedsstatus konnte gerade nicht geprüft werden. Bitte versuche es erneut.",
                    ephemeral=True,
                )
                return
            if has_member_choice(member):
                await interaction.followup.send(
                    "Du hast bereits eine Bewerbung eingereicht oder bist Spieler. Eine weitere Bewerbung ist hier nicht möglich.",
                    ephemeral=True,
                )
                self.stop()
                return

            try:
                await member.add_roles(applicant_role, reason="Bewerbung eingereicht")
            except discord.HTTPException:
                await interaction.followup.send(
                    "Deine Bewerbung konnte nicht freigeschaltet werden. Bitte melde dich beim Staff.",
                    ephemeral=True,
                )
                return

            submitted_at = int(time.time())
            application_embed = discord.Embed(
                title="Neue Team-Bewerbung",
                description=f"Bewerbung von {member.mention} · eingegangen <t:{submitted_at}:R>",
                color=discord.Color.from_rgb(110, 70, 255),
            )
            application_embed.add_field(name="Mindestalter", value="16+ bestätigt", inline=True)
            application_embed.add_field(name="Bevorzugter Name", value=str(self.draft["real_name"]), inline=True)
            application_embed.add_field(
                name="Kurs/Ausbildung", value=str(self.draft["course"]) or "Nicht angegeben", inline=True
            )
            application_embed.add_field(name="Main-Rolle", value=main_role, inline=True)
            application_embed.add_field(
                name="Spielbare Rollen (inkl. Main-Rolle)", value=", ".join(playable_roles), inline=True
            )
            slot_order = [value for _, value, _ in self.availability_slots()]
            short_days = dict(self.DAYS)
            selected_times = [value.split("|", 1) for value in sorted(availability, key=slot_order.index)]
            availability_text = "\n".join(f"{short_days[day]} · {window}" for day, window in selected_times)
            application_embed.add_field(name="Spielzeiten", value=availability_text[:1024], inline=False)
            application_embed.add_field(name="Tracker.gg", value=str(self.draft["tracker_url"])[:1024], inline=False)
            application_embed.set_footer(text=f"Status: {APPLICATION_STATUS_LABELS['pending']}")
            try:
                staff_message = await staff_channel.send(
                    embed=application_embed,
                    view=ApplicationReviewView(),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                try:
                    await member.remove_roles(applicant_role, reason="Bewerbung konnte nicht an den Staff übermittelt werden")
                except discord.HTTPException:
                    log.exception("Bewerberrolle konnte nach fehlgeschlagener Übermittlung nicht zurückgenommen werden")
                await interaction.followup.send(
                    "Die Bewerbung konnte gerade nicht an den Staff übermittelt werden. Bitte versuche es später erneut oder kontaktiere Staff.",
                    ephemeral=True,
                )
                return
            save_application(Application(staff_message.id, interaction.guild.id, member.id, "pending", submitted_at))
            riot_id = riot_id_from_tracker(str(self.draft["tracker_url"]))
            update_profile(
                interaction.guild.id,
                member.id,
                main_role=main_role,
                playable_roles=", ".join(playable_roles),
                **({"riot_name": riot_id[0], "riot_tag": riot_id[1], "region": None} if riot_id else {}),
            )
            if riot_id:
                run_in_background(sync_rank_quietly(member))
            await open_application_thread(staff_message, member, str(self.draft["real_name"]))

            try:
                await applicant_chat.send(
                    f"{member.mention} willkommen im Bewerberbereich. Du kannst hier Fragen zum Bewerbungsablauf stellen.",
                    allowed_mentions=discord.AllowedMentions(
                        users=[member],
                        roles=False,
                        everyone=False,
                    ),
                )
            except discord.HTTPException:
                log.exception("Begrüßung im Bewerber-Chat konnte nicht gesendet werden")

            await interaction.followup.send(
                "Danke! Deine Bewerbung wurde an den Staff geschickt. Du hast jetzt die Rolle `Bewerber` und Zugang zu `#bewerber-chat`, wo du Fragen zum Ablauf stellen kannst. `#bewerbung` kannst du bis zur nächsten Saison nicht mehr sehen oder erneut nutzen. Der Staff meldet sich nach der Prüfung bei dir.",
                ephemeral=True,
            )
            self.draft.clear()
            self.stop()


if len(ApplicationSelectionView.availability_slots()) > MAX_OPTIONS_PER_SELECT * MAX_AVAILABILITY_SELECTS:
    raise RuntimeError(
        f"Höchstens {MAX_OPTIONS_PER_SELECT * MAX_AVAILABILITY_SELECTS} Kombinationen aus Spieltagen und Zeitfenstern möglich."
    )


class OnboardingView(DualityView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Bewerbung starten",
        style=discord.ButtonStyle.primary,
        custom_id="duality:application:start",
    )
    async def application_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Bitte nutze diese Auswahl direkt im Server.", ephemeral=True)
            return
        if has_member_choice(interaction.user):
            await interaction.response.send_message("Du hast bereits eine Team-Rolle.", ephemeral=True)
            return
        await interaction.response.send_modal(TrackerLinkModal())


APPLICATION_STATUS_LABELS = {
    "pending": "offen",
    "accepted": "angenommen",
    "declined": "abgelehnt",
    "left": "geschlossen (Server verlassen)",
}
LEGACY_DECISIONS = {"ACCEPTED": "accepted", "DECLINED": "declined", "LEFT": "left"}


def legacy_application(message: discord.Message) -> Application | None:
    """Liest Bewerbungen, deren Status ältere Bot-Versionen nur im Footer gespeichert haben."""
    if not message.embeds or message.guild is None:
        return None
    footer = message.embeds[0].footer.text or ""
    submitted_at = int(message.created_at.timestamp())
    try:
        if footer.startswith(APPLICATION_PENDING_PREFIX):
            applicant_id = int(footer.removeprefix(APPLICATION_PENDING_PREFIX))
            return Application(message.id, message.guild.id, applicant_id, "pending", submitted_at)
        if footer.startswith(APPLICATION_DECISION_PREFIX):
            decision, applicant_id, decided_by, decided_at = footer.removeprefix(APPLICATION_DECISION_PREFIX).split("|")
            return Application(
                message.id,
                message.guild.id,
                int(applicant_id),
                LEGACY_DECISIONS[decision],
                submitted_at,
                int(decided_by) or None,
                int(decided_at),
            )
    except (KeyError, ValueError):
        return None
    return None


def load_application(message: discord.Message) -> Application | None:
    application = get_application(message.id)
    if application is None:
        application = legacy_application(message)
        if application:
            save_application(application)
    return application


VOTE_FIELD = "Staff-Abstimmung"


def required_votes(guild: discord.Guild) -> int:
    # Bei sehr kleinem Staff darf eine Entscheidung nicht dauerhaft blockiert sein.
    staff_ids = {
        member.id
        for name in STAFF_ROLES
        if (role := find_role(guild, name))
        for member in role.members
        if not getattr(member, "bot", False)
    }
    return max(1, min(APPLICATION_MIN_VOTES, len(staff_ids)))


def render_application(message: discord.Message, application: Application) -> discord.Embed:
    embed = discord.Embed.from_dict(message.embeds[0].to_dict())
    # Aus der Datenbank neu aufbauen, damit ein erneutes Rendern nichts doppelt.
    fields = [field for field in embed.fields if field.name not in {"Entscheidung", VOTE_FIELD}]
    embed.clear_fields()
    for field in fields:
        embed.add_field(name=field.name, value=field.value, inline=field.inline)
    votes = get_votes(application.message_id)
    yes = [f"<@{staff_id}>" for staff_id, vote in votes.items() if vote == "yes"]
    no = [f"<@{staff_id}>" for staff_id, vote in votes.items() if vote == "no"]
    if votes or application.status == "pending":
        vote_text = f"👍 {len(yes)}{' · ' + ', '.join(yes) if yes else ''}\n👎 {len(no)}{' · ' + ', '.join(no) if no else ''}"
        if application.status == "pending":
            vote_text += f"\nEntscheidung ab {required_votes(message.guild)} Stimmen möglich"
        embed.add_field(name=VOTE_FIELD, value=vote_text[:1024], inline=False)
    decided = f"<t:{application.decided_at}:F>" if application.decided_at else ""
    if application.status == "accepted":
        embed.add_field(name="Entscheidung", value=f"Angenommen von <@{application.decided_by}> · {decided}", inline=False)
        embed.color = discord.Color.green()
    elif application.status == "declined":
        embed.add_field(name="Entscheidung", value=f"Abgelehnt von <@{application.decided_by}> · {decided}", inline=False)
        embed.color = discord.Color.red()
    elif application.status == "left":
        embed.add_field(
            name="Entscheidung",
            value=f"Automatisch geschlossen: Die Person hat den Server verlassen · {decided}",
            inline=False,
        )
        embed.color = discord.Color.light_grey()
    embed.set_footer(text=f"Status: {APPLICATION_STATUS_LABELS[application.status]}")
    return embed


async def show_application(message: discord.Message, application: Application) -> None:
    view = None
    if application.status == "pending":
        votes = list(get_votes(application.message_id).values())
        view = ApplicationReviewView(yes=votes.count("yes"), no=votes.count("no"))
    await message.edit(
        embed=render_application(message, application),
        view=view,
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def application_thread(message: discord.Message) -> discord.Thread | None:
    # Ein Thread an einer Nachricht hat dieselbe ID wie die Nachricht.
    thread = message.guild.get_thread(message.id)
    if thread is None:
        try:
            thread = await message.guild.fetch_channel(message.id)
        except discord.HTTPException:
            return None
    return thread if isinstance(thread, discord.Thread) else None


async def open_application_thread(message: discord.Message, applicant: discord.Member, real_name: str) -> None:
    try:
        thread = await message.create_thread(name=f"Bewerbung {real_name}"[:100], auto_archive_duration=10080)
        await thread.send(
            f"Diskussion und Notizen zur Bewerbung von {applicant.mention}. Nur Staff sieht diesen Thread. "
            f"Stimmt unter der Bewerbung mit 👍/👎 ab; entschieden werden kann ab {required_votes(message.guild)} Stimmen.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        log.exception("Staff-Thread für Bewerbung %s konnte nicht erstellt werden", message.id)


async def close_application_thread(message: discord.Message, application: Application) -> None:
    thread = await application_thread(message)
    if thread is None:
        return
    try:
        await thread.send(
            f"Status: **{APPLICATION_STATUS_LABELS[application.status]}**. Der Thread wird archiviert.",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await thread.edit(archived=True)
    except discord.HTTPException:
        log.warning("Staff-Thread %s konnte nicht archiviert werden", thread.id)


async def vote_on_application(interaction: discord.Interaction, vote: str) -> None:
    if not interaction.guild or not isinstance(interaction.user, discord.Member) or not interaction.message:
        await interaction.response.send_message("Abstimmen geht nur im Server.", ephemeral=True)
        return
    if not is_staff(interaction.user):
        await interaction.response.send_message("Nur Staff kann über Bewerbungen abstimmen.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    lock = APPLICATION_LOCKS.setdefault(interaction.message.id, asyncio.Lock())
    async with lock:
        application = load_application(interaction.message)
        if application is None or application.status != "pending":
            await interaction.followup.send("Über diese Bewerbung kann nicht mehr abgestimmt werden.", ephemeral=True)
            return
        previous = get_votes(application.message_id).get(interaction.user.id)
        new_vote = None if previous == vote else vote
        set_vote(application.message_id, interaction.user.id, new_vote)
        await show_application(interaction.message, application)
    text = {"yes": "Deine Stimme: 👍", "no": "Deine Stimme: 👎", None: "Deine Stimme wurde zurückgenommen."}[new_vote]
    await interaction.followup.send(text, ephemeral=True)


async def review_application(interaction: discord.Interaction, accepted: bool) -> None:
    if not interaction.guild or not isinstance(interaction.user, discord.Member) or not interaction.message:
        await interaction.response.send_message("Diese Entscheidung ist nur im Server möglich.", ephemeral=True)
        return
    if not is_staff(interaction.user):
        await interaction.response.send_message("Nur Staff kann Bewerbungen entscheiden.", ephemeral=True)
        return

    # Vor dem Lock bestätigen: Wer am Lock wartet, würde sonst Discords
    # 3-Sekunden-Frist für die erste Antwort überschreiten.
    await interaction.response.defer(ephemeral=True, thinking=True)
    lock = APPLICATION_LOCKS.setdefault(interaction.message.id, asyncio.Lock())
    async with lock:
        await _review_application_locked(interaction, accepted)


async def _review_application_locked(interaction: discord.Interaction, accepted: bool) -> None:
    guild = interaction.guild
    message = interaction.message
    application = load_application(message)
    if application is None:
        await interaction.followup.send("Diese Nachricht ist keine gültige Bewerbung.", ephemeral=True)
        return
    if application.status != "pending":
        # Falls die Nachricht nach einer Entscheidung nicht aktualisiert werden
        # konnte, holt ein weiterer Klick das jetzt nach.
        if message.components:
            try:
                await show_application(message, application)
            except discord.HTTPException:
                log.exception("Bewerbung %s konnte nicht aktualisiert werden", message.id)
        await interaction.followup.send("Diese Bewerbung wurde bereits bearbeitet.", ephemeral=True)
        return

    player_role = find_role(guild, "Spieler")
    applicant_role = find_role(guild, "Bewerber")
    if not player_role or not applicant_role:
        await interaction.followup.send("Die benötigten Rollen fehlen. Bitte Staff prüfen lassen.", ephemeral=True)
        return

    try:
        applicant = await guild.fetch_member(application.applicant_id)
    except discord.NotFound:
        decide_application(message.id, "left", None, int(time.time()))
        application = get_application(message.id)
        await show_application(message, application)
        await close_application_thread(message, application)
        await interaction.followup.send(
            "Die Person ist nicht mehr auf dem Server. Die Bewerbung wurde geschlossen und wird "
            f"nach {APPLICATION_RETENTION_DAYS} Tagen automatisch gelöscht.",
            ephemeral=True,
        )
        return
    except discord.HTTPException:
        await interaction.followup.send(
            "Die Person konnte gerade nicht geladen werden. Bitte versuche es erneut.", ephemeral=True
        )
        return

    vote_count = len(get_votes(message.id))
    needed = required_votes(guild)
    if vote_count < needed:
        await interaction.followup.send(
            f"Für eine Entscheidung braucht es mindestens {needed} Staff-Stimmen (👍/👎); bisher sind es {vote_count}.",
            ephemeral=True,
        )
        return

    try:
        if accepted:
            await set_member_roles(
                applicant,
                add=[player_role],
                remove=[applicant_role],
                reason=f"Bewerbung angenommen von {interaction.user}",
            )
        elif applicant_role not in applicant.roles:
            await applicant.add_roles(applicant_role, reason="Bewerberstatus nach Ablehnung beibehalten")
    except discord.HTTPException:
        log.exception("Rollen für Bewerbung %s konnten nicht gesetzt werden", message.id)
        await interaction.followup.send(
            "Die Rollen konnten nicht geändert werden; die Bewerbung bleibt offen. "
            "Prüfe, ob die Bot-Rolle über `Spieler` und `Bewerber` steht.",
            ephemeral=True,
        )
        return

    status = "accepted" if accepted else "declined"
    decide_application(message.id, status, interaction.user.id, int(time.time()))
    application = get_application(message.id)
    if accepted:
        dm_text = "Deine Bewerbung bei Duality Mannheim wurde angenommen. Du hast jetzt die Rolle `Spieler` und Zugang zum Teambereich. Willkommen im Team!"
    else:
        dm_text = "Danke für deine Bewerbung bei Duality Mannheim. Der Staff kann dir aktuell keinen Teamplatz anbieten. Deine Rolle `Bewerber` bleibt bis zur nächsten Saison bestehen. Vor jeder neuen Saison öffnen wir die Bewerbungen wieder, dann kannst du dich erneut bewerben. Wenn du Rückfragen hast, wende dich bitte direkt an den Staff."

    dm_sent = True
    try:
        await applicant.send(dm_text)
    except discord.HTTPException:
        dm_sent = False

    try:
        await show_application(message, application)
    except discord.HTTPException:
        log.exception("Bewerbung %s konnte nicht als entschieden markiert werden", message.id)
        await interaction.followup.send(
            "Die Entscheidung ist gespeichert, aber die Bewerbungsnachricht konnte nicht aktualisiert werden. "
            "Klicke gleich noch einmal auf einen der Buttons, dann wird sie nachgezogen.",
            ephemeral=True,
        )
        return
    await close_application_thread(message, application)

    response = "Bewerbung angenommen" if accepted else "Bewerbung abgelehnt; die Bewerberrolle sperrt eine erneute Bewerbung bis `/neue-saison`"
    if not dm_sent:
        response += ". Die DM konnte nicht zugestellt werden"
    if not accepted:
        response += f". Der Eintrag wird nach {APPLICATION_RETENTION_DAYS} Tagen automatisch gelöscht"
    await interaction.followup.send(response + ".", ephemeral=True)


class ApplicationReviewView(DualityView):
    def __init__(self, yes: int = 0, no: int = 0):
        super().__init__(timeout=None)
        self.vote_yes.label = f"👍 {yes}"
        self.vote_no.label = f"👎 {no}"

    @discord.ui.button(label="👍 0", style=discord.ButtonStyle.secondary, custom_id="duality:application:vote_yes")
    async def vote_yes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await vote_on_application(interaction, "yes")

    @discord.ui.button(label="👎 0", style=discord.ButtonStyle.secondary, custom_id="duality:application:vote_no")
    async def vote_no(self, interaction: discord.Interaction, button: discord.ui.Button):
        await vote_on_application(interaction, "no")

    @discord.ui.button(label="Annehmen", style=discord.ButtonStyle.success, custom_id="duality:application:accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await review_application(interaction, accepted=True)

    @discord.ui.button(label="Ablehnen", style=discord.ButtonStyle.danger, custom_id="duality:application:decline")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await review_application(interaction, accepted=False)


async def close_applications_of_departed_member(member: discord.Member) -> None:
    pending = find_pending_applications(member.guild.id, member.id)
    if not pending:
        return
    staff_channel = find_text_channel(member.guild, "bewerbungen")
    for application in pending:
        lock = APPLICATION_LOCKS.setdefault(application.message_id, asyncio.Lock())
        async with lock:
            if not decide_application(application.message_id, "left", None, int(time.time())):
                continue
            log.info("Bewerbung von %s geschlossen: Server verlassen", member.id)
            if staff_channel is None:
                continue
            try:
                message = await staff_channel.fetch_message(application.message_id)
                closed = get_application(application.message_id)
                await show_application(message, closed)
                await close_application_thread(message, closed)
            except discord.HTTPException:
                log.warning("Bewerbungsnachricht %s konnte nicht aktualisiert werden", application.message_id)


async def reopen_applications(guild: discord.Guild) -> int | None:
    applicant_role = find_role(guild, "Bewerber")
    if applicant_role is None:
        return None
    pending_ids = {application.applicant_id for application in find_pending_applications(guild.id)}
    reopened = 0
    for member in list(applicant_role.members):
        if member.id in pending_ids:
            continue
        await member.remove_roles(applicant_role, reason="Neue Saison: erneute Bewerbung möglich")
        reopened += 1
    return reopened


class SeasonResetConfirmView(DualityView):
    def __init__(self, staff_id: int):
        super().__init__(timeout=120)
        self.staff_id = staff_id

    @discord.ui.button(label="Ja, Bewerbungen wieder öffnen", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.staff_id or not interaction.guild:
            await interaction.response.send_message("Diese Bestätigung gehört jemand anderem.", ephemeral=True)
            return
        self.stop()
        await interaction.response.edit_message(content="Bewerbungen werden wieder geöffnet …", view=None)
        reopened = await reopen_applications(interaction.guild)
        if reopened is None:
            text = "Die Rolle `Bewerber` wurde nicht gefunden."
        else:
            text = f"Neue Saison gestartet: {reopened} Personen können sich wieder bewerben. Offene Bewerbungen bleiben unverändert."
        await interaction.edit_original_response(content=text)


def staff_members(guild: discord.Guild) -> list[discord.Member]:
    members = {}
    for name in STAFF_ROLES:
        role = find_role(guild, name)
        if role:
            members.update({member.id: member for member in role.members if not member.bot})
    return list(members.values())


async def create_ticket(interaction: discord.Interaction, details: str, member: discord.Member | None = None) -> None:
    """Öffnet einen privaten Thread, den nur die meldende Person und der Staff sehen."""
    tickets_channel = find_text_channel(interaction.guild, "tickets")
    if tickets_channel is None:
        await send_error_response(interaction, "Das Ticket-System ist noch nicht eingerichtet.")
        return
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)
    reporter = interaction.user
    thread = await tickets_channel.create_thread(
        name=f"Ticket – {reporter.display_name}"[:100],
        type=discord.ChannelType.private_thread,
        invitable=False,
        auto_archive_duration=10080,
        reason="Ticket über den Duality-Bot",
    )
    save_ticket(thread.id, interaction.guild.id, reporter.id)
    await thread.add_user(reporter)
    for staff_member in staff_members(interaction.guild):
        try:
            await thread.add_user(staff_member)
        except discord.HTTPException:
            log.warning("Staff %s konnte nicht zum Ticket %s hinzugefügt werden", staff_member.id, thread.id)

    embed = discord.Embed(
        title="Vertrauliches Ticket",
        description=details,
        color=discord.Color.orange(),
        timestamp=discord.utils.utcnow(),
    )
    embed.add_field(name="Von", value=f"{reporter.mention} (`{reporter.id}`)", inline=True)
    embed.add_field(name="Betroffenes Mitglied", value=member.mention if member else "Nicht angegeben", inline=True)
    embed.set_footer(text="Nur du und der Staff sehen diesen Thread. Schreibt hier für Rückfragen.")
    await thread.send(embed=embed, view=TicketCloseView(), allowed_mentions=discord.AllowedMentions.none())

    orga = find_text_channel(interaction.guild, "orga")
    if orga:
        try:
            await orga.send(f"🎫 Neues Ticket von {reporter.mention}: {thread.mention}", allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException:
            log.warning("Ticket-Hinweis in #orga konnte nicht gesendet werden")
    await interaction.followup.send(
        f"Dein Ticket ist offen: {thread.mention}. Nur du und der Staff sehen es; dort könnt ihr euch austauschen.",
        ephemeral=True,
    )


class TicketModal(discord.ui.Modal, title="Ticket an den Staff"):
    details = discord.ui.TextInput(
        label="Was ist passiert?",
        placeholder="Bitte keine unnötigen privaten Daten eintragen.",
        style=discord.TextStyle.paragraph,
        min_length=10,
        max_length=1500,
    )

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Tickets gehen nur direkt auf dem Server.", ephemeral=True)
            return
        await create_ticket(interaction, str(self.details).strip())

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.error("Fehler beim Ticket", exc_info=error)
        await send_error_response(interaction)


class TicketStartView(DualityView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Ticket öffnen", emoji="🎫", style=discord.ButtonStyle.primary, custom_id="duality:ticket:open")
    async def open_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketModal())


class TicketCloseView(DualityView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Ticket schließen", style=discord.ButtonStyle.secondary, custom_id="duality:ticket:close")
    async def close(self, interaction: discord.Interaction, button: discord.ui.Button):
        thread = interaction.channel
        if not isinstance(thread, discord.Thread) or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Das geht nur im Ticket selbst.", ephemeral=True)
            return
        reporter_id = get_ticket_reporter(thread.id)
        if interaction.user.id != reporter_id and not is_staff(interaction.user):
            await interaction.response.send_message("Nur die meldende Person oder Staff kann das Ticket schließen.", ephemeral=True)
            return
        if not close_ticket(thread.id):
            await interaction.response.send_message("Das Ticket ist bereits geschlossen.", ephemeral=True)
            return
        await interaction.response.send_message(f"🔒 Ticket geschlossen von {interaction.user.mention}.")
        await thread.edit(archived=True)


async def handle_temp_voice(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
    guild = member.guild
    creator_ids = {
        channel.id: prefix
        for name, prefix in TEMP_VOICE_CREATORS.items()
        if (channel := stored_channel(guild, "channel", name))
    }
    if after.channel and after.channel.id in creator_ids:
        category = after.channel.category
        overwrites = dict(category.overwrites) if category else {}
        overwrites[member] = discord.PermissionOverwrite(manage_channels=True)
        channel = await guild.create_voice_channel(
            f"{creator_ids[after.channel.id]} {member.display_name}"[:100],
            category=category,
            overwrites=overwrites,
            reason="Temporärer Voice-Channel",
        )
        save_temp_voice(channel.id, guild.id, member.id)
        try:
            await member.move_to(channel, reason="Eigener Voice-Channel")
        except discord.HTTPException:
            await channel.delete(reason="Ersteller ist nicht mehr im Voice")
            delete_temp_voice(channel.id)
    if before.channel and before.channel != after.channel and before.channel.id in temp_voice_ids(guild.id):
        if not before.channel.members:
            await before.channel.delete(reason="Temporärer Voice-Channel ist leer")
            delete_temp_voice(before.channel.id)


async def clean_up_temp_voice(guild: discord.Guild) -> None:
    # Nach einem Neustart sind leere Kanäle übrig, deren Verlassen der Bot nicht mitbekommen hat.
    for channel_id in temp_voice_ids(guild.id):
        channel = guild.get_channel(channel_id)
        if channel is None:
            delete_temp_voice(channel_id)
        elif not channel.members:
            try:
                await channel.delete(reason="Temporärer Voice-Channel ist leer")
                delete_temp_voice(channel_id)
            except discord.HTTPException:
                log.warning("Leerer Voice-Channel %s konnte nicht gelöscht werden", channel_id)


async def audit(guild: discord.Guild, text: str, color: discord.Color = discord.Color.dark_grey()) -> None:
    channel = find_text_channel(guild, "audit-log")
    if channel is None:
        return
    try:
        await channel.send(
            embed=discord.Embed(description=text[:4000], color=color, timestamp=discord.utils.utcnow()),
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        log.warning("Audit-Log-Eintrag konnte nicht gesendet werden")


async def audit_role_changes(before: discord.Member, after: discord.Member) -> None:
    added = [role for role in after.roles if role not in before.roles]
    removed = [role for role in before.roles if role not in after.roles]
    if not added and not removed:
        return
    lines = [f"**Rollen geändert:** {after.mention} (`{after.id}`)"]
    if added:
        lines.append("➕ " + ", ".join(role.mention for role in added))
    if removed:
        lines.append("➖ " + ", ".join(role.mention for role in removed))
    await audit(after.guild, "\n".join(lines), discord.Color.blurple())


async def audit_message_delete(guild: discord.Guild, payload: discord.RawMessageDeleteEvent) -> None:
    skipped_ids = {
        channel.id for name in ("audit-log", "bewerbungen") if (channel := find_text_channel(guild, name))
    }
    if payload.channel_id in skipped_ids:
        return
    message = payload.cached_message
    if message is not None and message.author.bot:
        return
    text = f"**Nachricht gelöscht** in <#{payload.channel_id}>"
    if message is None:
        text += "\nAutor und Inhalt unbekannt (Nachricht war nicht im Cache)."
    else:
        text += f" · von {message.author.mention} (`{message.author.id}`) · geschrieben <t:{int(message.created_at.timestamp())}:R>"
        if message.content:
            text += f"\n>>> {message.content[:1500]}"
        if message.attachments:
            text += f"\n{len(message.attachments)} Anhang/Anhänge"
    await audit(guild, text, discord.Color.red())


async def delete_my_applications(guild: discord.Guild, user_id: int) -> int:
    staff_channel = find_text_channel(guild, "bewerbungen")
    deleted = 0
    for application in applications_of(guild.id, user_id):
        if staff_channel:
            try:
                message = await staff_channel.fetch_message(application.message_id)
                await message.delete()
            except discord.NotFound:
                pass
        delete_application(application.message_id)
        deleted += 1
    return deleted


class MyDataView(DualityView):
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id
        self.confirmed = False

    @discord.ui.button(label="Meine Daten löschen …", style=discord.ButtonStyle.danger)
    async def delete_data(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id or not interaction.guild:
            await interaction.response.send_message("Das sind nicht deine Daten.", ephemeral=True)
            return
        if not self.confirmed:
            self.confirmed = True
            button.label = "Ja, endgültig löschen"
            await interaction.response.edit_message(
                content="Sicher? Gelöscht werden Profil (Riot-ID, Rollen, Rang-Sync), Teamname, Terminantworten und "
                "deine Bewerbungen samt Nachricht im Staff-Kanal. Deine Discord-Rollen bleiben bestehen. "
                "Tickets und Staff-Stimmen bleiben als Nachweis erhalten.",
                view=self,
            )
            return
        self.stop()
        await interaction.response.defer(ephemeral=True, thinking=True)
        deleted_applications = await delete_my_applications(interaction.guild, self.user_id)
        delete_user_data(interaction.guild.id, self.user_id)
        log.info("Daten von %s auf Wunsch gelöscht", self.user_id)
        await interaction.followup.send(
            f"Erledigt. Deine gespeicherten Daten wurden gelöscht ({deleted_applications} Bewerbung(en)).",
            ephemeral=True,
        )


async def toggle_notification_role(interaction: discord.Interaction, role_name: str) -> None:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Bitte nutze den Button auf dem Server.", ephemeral=True)
        return
    role = find_role(interaction.guild, role_name)
    if role is None:
        await interaction.response.send_message("Diese Benachrichtigungsrolle ist noch nicht eingerichtet.", ephemeral=True)
        return
    if role in interaction.user.roles:
        await interaction.user.remove_roles(role, reason="Benachrichtigungen abbestellt")
        state = "abgewählt"
    else:
        await interaction.user.add_roles(role, reason="Benachrichtigungen abonniert")
        state = "aktiviert"
    await interaction.response.send_message(f"`{role_name}` ist jetzt {state}.", ephemeral=True)


class NotificationView(DualityView):
    # Rang und Hochschule laufen über /riot-id und /hochschule; hier bleiben nur die Pings.
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Valorant-Updates",
        style=discord.ButtonStyle.secondary,
        custom_id="duality:notify:valorant",
    )
    async def valorant(self, interaction: discord.Interaction, button: discord.ui.Button):
        await toggle_notification_role(interaction, "Ping Valorant")

    @discord.ui.button(
        label="Community-Events",
        style=discord.ButtonStyle.secondary,
        custom_id="duality:notify:events",
    )
    async def events(self, interaction: discord.Interaction, button: discord.ui.Button):
        await toggle_notification_role(interaction, "Ping Events")


NO_ROLE_VALUE = "__none__"


async def update_event_rsvps(interaction: discord.Interaction, choice: str) -> None:
    if not interaction.guild or not interaction.message or not interaction.message.embeds:
        await interaction.response.send_message("Dieser Termin ist ungültig.", ephemeral=True)
        return
    footer = interaction.message.embeds[0].footer.text or ""
    if not footer.startswith(EVENT_MARKER):
        await interaction.response.send_message("Das ist kein Termin des Duality-Bots.", ephemeral=True)
        return
    try:
        starts_at = int(footer.rsplit("|", 1)[1])
    except (IndexError, ValueError):
        starts_at = None
    if starts_at is not None and starts_at <= time.time():
        await interaction.response.send_message(
            "Dieser Termin hat bereits begonnen; Zu- und Absagen sind geschlossen.", ephemeral=True
        )
        return

    labels = {
        "going": "✅ Dabei",
        "maybe": "🤔 Vielleicht",
        "declined": "❌ Kann nicht",
    }
    await interaction.response.defer()
    # Schreiben, Lesen und Bearbeiten nacheinander, damit kein gleichzeitiger
    # Klick das Embed mit einem älteren Stand überschreibt.
    lock = RSVP_LOCKS.setdefault(interaction.message.id, asyncio.Lock())
    async with lock:
        write_rsvp(interaction.message.id, interaction.user.id, choice)
        responses = get_rsvps(interaction.message.id)
        embed = discord.Embed.from_dict(interaction.message.embeds[0].to_dict())
        embed.clear_fields()
        for rsvp_choice in ("going", "maybe", "declined"):
            member_ids = responses.get(rsvp_choice, [])
            listed = [f"<@{member_id}>" for member_id in member_ids[:35]]
            if len(member_ids) > len(listed):
                listed.append(f"… und {len(member_ids) - len(listed)} weitere")
            embed.add_field(
                name=f"{labels[rsvp_choice]} ({len(member_ids)})",
                value="\n".join(listed) or "Noch niemand",
                inline=True,
            )
        await interaction.edit_original_response(embed=embed, view=EventRSVPView())
    await interaction.followup.send(f"Deine Antwort wurde auf „{labels[choice]}“ gesetzt.", ephemeral=True)


class EventRSVPView(DualityView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Dabei", emoji="✅", style=discord.ButtonStyle.success, custom_id="duality:event:going")
    async def going(self, interaction: discord.Interaction, button: discord.ui.Button):
        await update_event_rsvps(interaction, "going")

    @discord.ui.button(label="Vielleicht", emoji="🤔", style=discord.ButtonStyle.secondary, custom_id="duality:event:maybe")
    async def maybe(self, interaction: discord.Interaction, button: discord.ui.Button):
        await update_event_rsvps(interaction, "maybe")

    @discord.ui.button(label="Kann nicht", emoji="❌", style=discord.ButtonStyle.danger, custom_id="duality:event:declined")
    async def declined(self, interaction: discord.Interaction, button: discord.ui.Button):
        await update_event_rsvps(interaction, "declined")


async def send_due_event_reminders(client: discord.Client) -> None:
    now = int(time.time())
    for message_id, guild_id, channel_id, starts_at in get_upcoming_events(now):
        seconds_until_start = starts_at - now
        if 23 * 60 * 60 <= seconds_until_start <= 24 * 60 * 60:
            reminder_key = "24h"
            reminder_label = "Morgen"
        elif 0 < seconds_until_start <= 60 * 60:
            reminder_key = "1h"
            reminder_label = "Gleich geht's los"
        else:
            continue

        if event_reminder_was_sent(message_id, reminder_key):
            continue

        guild = client.get_guild(guild_id)
        if guild is None:
            continue

        channel = guild.get_channel(channel_id)
        if not isinstance(channel, discord.TextChannel):
            try:
                fetched_channel = await client.fetch_channel(channel_id)
            except discord.NotFound:
                delete_scheduled_event(message_id)
                continue
            except discord.HTTPException as error:
                log.warning(f"Erinnerung für Termin {message_id} konnte Kanal nicht laden: {error}")
                continue
            if not isinstance(fetched_channel, discord.TextChannel):
                delete_scheduled_event(message_id)
                continue
            channel = fetched_channel

        try:
            event_message = await channel.fetch_message(message_id)
        except discord.NotFound:
            delete_scheduled_event(message_id)
            continue
        except discord.HTTPException as error:
            log.warning(f"Erinnerung für Termin {message_id} konnte Termin nicht laden: {error}")
            continue

        if not event_message.embeds or not (event_message.embeds[0].footer.text or "").startswith(EVENT_MARKER):
            delete_scheduled_event(message_id)
            continue

        attendee_ids = get_rsvps(message_id).get("going", [])
        attendees = [guild.get_member(user_id) for user_id in attendee_ids]
        attendees = [member for member in attendees if member is not None]
        mentioned_attendees = attendees[:100]
        mention_text = " ".join(member.mention for member in mentioned_attendees)
        extra_attendees = max(0, len(attendees) - len(mentioned_attendees))

        title = event_message.embeds[0].title or "Teamtermin"
        content = f"⏰ **{reminder_label}: {title}** beginnt <t:{starts_at}:R>."
        if mention_text:
            content += f"\nDabei: {mention_text}"
            if extra_attendees:
                content += f" und {extra_attendees} weitere Zusagen"
        else:
            content += "\nBisher hat noch niemand **Dabei** ausgewählt."

        try:
            await channel.send(
                content,
                allowed_mentions=discord.AllowedMentions(
                    users=mentioned_attendees,
                    roles=False,
                    everyone=False,
                    replied_user=False,
                ),
            )
        except discord.HTTPException as error:
            log.warning(f"Erinnerung für Termin {message_id} konnte nicht gesendet werden: {error}")
            continue

        mark_event_reminder_sent(message_id, reminder_key)


async def register_existing_scheduled_events(guild: discord.Guild) -> int:
    event_channel = find_text_channel(guild, "spieltermine")
    if event_channel is None:
        return 0

    now = int(time.time())
    imported = 0
    try:
        async for message in event_channel.history(limit=500):
            if message.author != guild.me or not message.embeds:
                continue
            footer = message.embeds[0].footer.text or ""
            if not footer.startswith(f"{EVENT_MARKER}|"):
                continue
            try:
                starts_at = int(footer.rsplit("|", 1)[1])
            except (IndexError, ValueError):
                continue
            if starts_at <= now:
                continue

            save_scheduled_event(message.id, guild.id, event_channel.id, starts_at)
            imported += 1
    except discord.HTTPException as error:
        log.warning(f"Vorhandene Termine konnten nicht für Erinnerungen eingelesen werden: {error}")

    return imported


BERLIN = ZoneInfo("Europe/Berlin")
EVENT_TIME_FORMAT = "%d.%m.%Y %H:%M"
EVENT_SUGGESTED_TIMES = ("18:00", "19:00", "19:30", "20:00", "21:00")
EVENT_DEFAULT_TIME = "19:30"
EVENT_SUGGESTION_DAYS = 14
WEEKDAY_SHORT = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")


def parse_event_time(text: str) -> datetime:
    return datetime.strptime(text.strip(), EVENT_TIME_FORMAT).replace(tzinfo=BERLIN)


def describe_event_time(moment: datetime, now: datetime) -> str:
    relative = {0: " (heute)", 1: " (morgen)"}.get((moment.date() - now.date()).days, "")
    return f"{WEEKDAY_SHORT[moment.weekday()]}, {moment.strftime(EVENT_TIME_FORMAT)} Uhr{relative}"


def matches_event_query(name: str, tokens: list[str]) -> bool:
    # Jedes Suchwort muss am Anfang von Wochentag, Datum, Uhrzeit oder „heute/morgen“
    # stehen; so passt „20“ zu 20:00, aber nicht zur Jahreszahl 2026.
    words = name.casefold().replace(",", " ").replace("(", " ").replace(")", " ").split()
    return all(any(word.startswith(token) for word in words) for token in tokens)


def event_time_suggestions(current: str, now: datetime) -> list[tuple[str, str]]:
    """Ohne Eingabe die nächsten Abende um 19:30; mit Eingabe alle passenden Zeiten."""
    now = now.astimezone(BERLIN)
    tokens = current.casefold().split()
    suggestions = []
    try:
        typed = parse_event_time(current)
    except ValueError:
        typed = None
    if typed and typed > now:
        suggestions.append((describe_event_time(typed, now), typed.strftime(EVENT_TIME_FORMAT)))
    for offset in range(EVENT_SUGGESTION_DAYS + 1):
        day = now.date() + timedelta(days=offset)
        for clock in EVENT_SUGGESTED_TIMES:
            if not tokens and clock != EVENT_DEFAULT_TIME:
                continue
            moment = parse_event_time(f"{day:%d.%m.%Y} {clock}")
            name = describe_event_time(moment, now)
            if moment > now and matches_event_query(name, tokens):
                suggestions.append((name, moment.strftime(EVENT_TIME_FORMAT)))
    # Discord zeigt höchstens 25 Vorschläge.
    return list(dict.fromkeys(suggestions))[:25]


def register_app_commands(tree: app_commands.CommandTree) -> None:
    @tree.error
    async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
        if isinstance(error, app_commands.CommandOnCooldown):
            await send_error_response(
                interaction, f"Bitte warte noch {math.ceil(error.retry_after)} Sekunden, bevor du das erneut sendest."
            )
            return
        command_name = interaction.command.name if interaction.command else "unbekannt"
        log.error("Fehler bei /%s", command_name, exc_info=error)
        await send_error_response(interaction)

    def per_member_cooldown(interaction: discord.Interaction):
        return (interaction.guild_id, interaction.user.id)

    @tree.command(name="help", description="Zeigt die wichtigsten Server- und Bot-Aktionen.")
    async def help_command(interaction: discord.Interaction):
        embed = discord.Embed(
            title="Duality Mannheim – Hilfe",
            description="Hier findest du die wichtigsten Wege durch den Server.",
            color=discord.Color.from_rgb(110, 70, 255),
        )
        embed.add_field(
            name="Community",
            value="Lies `#regeln`, stell dich in `#allgemein` vor und nutze die Buttons in `#willkommen`, um Benachrichtigungen auszuwählen. "
            "Über `➕ Neuer Voice` bekommst du einen eigenen Voice-Channel, der verschwindet, sobald er leer ist.",
            inline=False,
        )
        embed.add_field(
            name="Valorant-Team",
            value="`#bewerbung` ist für alle sichtbar. Starte dort über den Button das Teamformular. Nach dem Absenden erhältst du `Bewerber` und kannst den Bewerbungskanal bis zur nächsten Saison nicht erneut nutzen. Deinen Rang holt `/riot-id Name#Tag` automatisch, deine Hochschule änderst du mit `/hochschule`.",
            inline=False,
        )
        embed.add_field(
            name="Bot-Befehle",
            value="`/report` öffnet ein privates Ticket mit dem Staff (oder der Button in `#tickets`) · `/feedback` sendet eine Idee · "
            "`/profil` zeigt Rang, Rollen und Hochschule · `/riot-id` verknüpft deine Riot-ID für den automatischen Rang · "
            "`/hochschule` ändert deine Hochschule · "
            "`/team` zeigt den aktuellen Kader · `/teamname` setzt optional deinen Namen für den Kader · "
            "`/meine-daten` zeigt, exportiert oder löscht deine gespeicherten Daten · "
            "`/termin` erstellt einen Teamtermin (Staff) · `/neue-saison` öffnet die Bewerbungen wieder (Staff).",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tree.command(name="report", description="Öffnet ein vertrauliches Ticket mit dem Staff.")
    @app_commands.rename(details="beschreibung", member="mitglied")
    @app_commands.describe(details="Was ist passiert? Bitte keine unnötigen privaten Daten eintragen.", member="Betroffenes Mitglied, falls relevant")
    @app_commands.checks.cooldown(1, 60.0, key=per_member_cooldown)
    async def report_command(
        interaction: discord.Interaction,
        details: str,
        member: discord.Member | None = None,
    ):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Dieser Befehl funktioniert nur im Server.", ephemeral=True)
            return
        text = details.strip()
        if len(text) < 10 or len(text) > 1500:
            await interaction.response.send_message("Bitte beschreibe das Problem mit 10 bis 1500 Zeichen.", ephemeral=True)
            return
        await create_ticket(interaction, text, member)

    @tree.command(name="feedback", description="Sendet eine Idee oder Rückmeldung privat an den Staff.")
    @app_commands.rename(suggestion="idee")
    @app_commands.describe(suggestion="Deine Idee oder Rückmeldung an den Staff")
    @app_commands.checks.cooldown(1, 60.0, key=per_member_cooldown)
    async def feedback_command(interaction: discord.Interaction, suggestion: str):
        if not interaction.guild:
            await interaction.response.send_message("Dieser Befehl funktioniert nur im Server.", ephemeral=True)
            return
        text = suggestion.strip()
        if len(text) < 5 or len(text) > 1500:
            await interaction.response.send_message("Bitte schreibe 5 bis 1500 Zeichen.", ephemeral=True)
            return
        staff_channel = find_text_channel(interaction.guild, "orga")
        if staff_channel is None:
            await interaction.response.send_message("Der Staff-Eingang ist noch nicht eingerichtet.", ephemeral=True)
            return
        embed = discord.Embed(
            title="Mitglieder-Feedback",
            description=text,
            color=discord.Color.blurple(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Von", value=f"{interaction.user} (`{interaction.user.id}`)", inline=False)
        await interaction.response.defer(ephemeral=True, thinking=True)
        await staff_channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        await interaction.followup.send(
            "Danke! Dein Feedback wurde privat an Staff gesendet. Es ist nicht anonym; Staff kann deinen Discord-Namen sehen.",
            ephemeral=True,
        )

    @tree.command(name="team", description="Zeigt den aktuellen Duality-Spielerkader.")
    async def team_command(interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Dieser Befehl funktioniert nur im Server.", ephemeral=True)
            return
        player_role = find_role(interaction.guild, "Spieler")
        if player_role is None:
            await interaction.response.send_message("Die Spielerrolle ist noch nicht eingerichtet.", ephemeral=True)
            return
        roster_names = get_roster_names(interaction.guild.id)
        names = sorted(
            (
                discord.utils.escape_markdown(roster_names.get(member.id, member.display_name))
                for member in player_role.members
            ),
            key=str.casefold,
        )
        value = "\n".join(f"• {name}" for name in names) if names else "Der Kader ist noch leer."
        if len(value) > 3900:
            value = value[:3890] + "…"
        embed = discord.Embed(title="Duality Mannheim – Team", description=value, color=discord.Color.from_rgb(110, 70, 255))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tree.command(name="teamname", description="Legt optional fest, welcher Name im Teamkader angezeigt wird.")
    @app_commands.describe(name="Öffentlicher Name für /team; leer lassen, um den Discord-Anzeigenamen zu verwenden.")
    async def teamname_command(interaction: discord.Interaction, name: str | None = None):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Dieser Befehl funktioniert nur im Server.", ephemeral=True)
            return
        if not member_has_role(interaction.user, "Spieler"):
            await interaction.response.send_message("Nur Mitglieder mit der Rolle `Spieler` können ihren Teamnamen festlegen.", ephemeral=True)
            return
        clean_name = (name or "").strip()
        if clean_name and len(clean_name) > 80:
            await interaction.response.send_message("Der Name darf höchstens 80 Zeichen haben.", ephemeral=True)
            return
        save_roster_name(interaction.guild.id, interaction.user.id, clean_name or None)
        if clean_name:
            await interaction.response.send_message(
                f"Im Teamkader wird künftig **{discord.utils.escape_markdown(clean_name)}** angezeigt. Das ist für Servermitglieder über `/team` sichtbar.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "Dein öffentlicher Teamname wurde entfernt; `/team` zeigt wieder deinen Discord-Anzeigenamen.",
                ephemeral=True,
            )

    @tree.command(name="termin", description="Erstellt einen Teamtermin mit Zu- und Absagen.")
    @app_commands.rename(title="titel", when="wann", details="infos")
    @app_commands.describe(
        title="Name des Termins",
        when="Vorschlag wählen oder TT.MM.JJJJ HH:MM tippen, z. B. „fr 20“ oder „morgen“",
        details="Optionale Infos zum Termin",
    )
    async def event_command(
        interaction: discord.Interaction,
        title: str,
        when: str,
        details: str = "",
    ):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Dieser Befehl funktioniert nur im Server.", ephemeral=True)
            return
        if not is_staff(interaction.user):
            await interaction.response.send_message("Nur Staff kann Teamtermine erstellen.", ephemeral=True)
            return
        event_channel = find_text_channel(interaction.guild, "spieltermine")
        if event_channel is None:
            await interaction.response.send_message("`#spieltermine` wurde nicht gefunden.", ephemeral=True)
            return
        if interaction.channel_id != event_channel.id:
            await interaction.response.send_message("Bitte nutze `/termin` in `#spieltermine`.", ephemeral=True)
            return
        try:
            event_time = parse_event_time(when)
        except ValueError:
            await interaction.response.send_message(
                "Wähle einen Vorschlag aus oder nutze das Format `TT.MM.JJJJ HH:MM`, zum Beispiel `25.10.2026 19:30`.",
                ephemeral=True,
            )
            return
        event_timestamp = int(event_time.timestamp())
        if event_timestamp <= int(time.time()):
            await interaction.response.send_message("Der Termin muss in der Zukunft liegen.", ephemeral=True)
            return
        clean_title = title.strip()
        if not clean_title or len(clean_title) > 100 or len(details) > 1000:
            await interaction.response.send_message("Der Titel darf 1–100 und die Zusatzinfo höchstens 1000 Zeichen haben.", ephemeral=True)
            return
        embed = discord.Embed(
            title=clean_title,
            description=(f"🕒 <t:{event_timestamp}:F> · <t:{event_timestamp}:R>\n\n{details.strip()}" if details.strip() else f"🕒 <t:{event_timestamp}:F> · <t:{event_timestamp}:R>"),
            color=discord.Color.from_rgb(110, 70, 255),
        )
        embed.add_field(name="✅ Dabei (0)", value="Noch niemand", inline=True)
        embed.add_field(name="🤔 Vielleicht (0)", value="Noch niemand", inline=True)
        embed.add_field(name="❌ Kann nicht (0)", value="Noch niemand", inline=True)
        embed.set_footer(text=f"{EVENT_MARKER}|{event_timestamp}")
        await interaction.response.defer(ephemeral=True, thinking=True)
        event_message = await event_channel.send(
            embed=embed,
            view=EventRSVPView(),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        save_scheduled_event(event_message.id, interaction.guild.id, event_channel.id, event_timestamp)
        await interaction.followup.send("Der Termin mit RSVP-Buttons wurde in `#spieltermine` veröffentlicht.", ephemeral=True)

    @event_command.autocomplete("when")
    async def event_time_autocomplete(interaction: discord.Interaction, current: str):
        return [
            app_commands.Choice(name=name, value=value)
            for name, value in event_time_suggestions(current, datetime.now(BERLIN))
        ]

    @tree.command(name="neue-saison", description="Öffnet die Bewerbungen für die neue Saison wieder (Staff).")
    async def new_season_command(interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Dieser Befehl funktioniert nur im Server.", ephemeral=True)
            return
        if not is_staff(interaction.user):
            await interaction.response.send_message("Nur Staff kann die neue Saison starten.", ephemeral=True)
            return
        await interaction.response.send_message(
            "Damit verlieren alle mit der Rolle `Bewerber`, deren Bewerbung nicht mehr offen ist, diese Rolle "
            "und können sich über `#bewerbung` erneut bewerben. Offene Bewerbungen bleiben unverändert.",
            view=SeasonResetConfirmView(interaction.user.id),
            ephemeral=True,
        )

    @tree.command(name="profil", description="Zeigt Rang, Rollen und Hochschule eines Mitglieds.")
    @app_commands.rename(member="mitglied")
    @app_commands.describe(member="Wessen Profil? Leer lassen für dein eigenes.")
    async def profile_command(interaction: discord.Interaction, member: discord.Member | None = None):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Dieser Befehl funktioniert nur im Server.", ephemeral=True)
            return
        target = member or interaction.user
        profile = get_profile(interaction.guild.id, target.id)
        # Riot-ID und Teilnahmequote sehen nur die Person selbst und der Staff.
        private_view = target.id == interaction.user.id or is_staff(interaction.user)

        def role_in(names) -> str:
            return next((name for name in names if member_has_role(target, name)), "Nicht angegeben")

        rank = role_in(VALORANT_RANKS)
        if profile and profile.rank_synced_at and profile.riot_id:
            rank = f"{profile.rank_tier} (automatisch, <t:{profile.rank_synced_at}:R>)"
        embed = discord.Embed(title=f"Profil von {target.display_name}", color=discord.Color.from_rgb(110, 70, 255))
        embed.set_thumbnail(url=target.display_avatar.url)
        embed.add_field(name="Rang", value=rank, inline=True)
        embed.add_field(name="Hochschule", value=role_in(UNIVERSITY_ROLES), inline=True)
        embed.add_field(name="Main-Rolle", value=(profile and profile.main_role) or "Nicht angegeben", inline=True)
        embed.add_field(name="Spielbare Rollen", value=(profile and profile.playable_roles) or "Nicht angegeben", inline=True)
        if private_view:
            embed.add_field(name="Riot-ID", value=(profile and profile.riot_id) or "Nicht verknüpft (`/riot-id`)", inline=True)
            if any(member_has_role(target, name) for name in TEAM_ROLES):
                now = int(time.time())
                since = max(now - EVENT_RETENTION_DAYS * 24 * 60 * 60, int(target.joined_at.timestamp()) if target.joined_at else 0)
                going, total = attendance(interaction.guild.id, target.id, since, now)
                quote_text = f"{going} von {total} Terminen zugesagt ({round(100 * going / total)} %)" if total else "Noch keine vergangenen Termine"
                embed.add_field(name=f"Teilnahme (letzte {EVENT_RETENTION_DAYS} Tage)", value=quote_text, inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tree.command(name="riot-id", description="Verknüpft deine Riot-ID, damit dein Rang automatisch aktualisiert wird.")
    @app_commands.rename(riot_id="riot-id")
    @app_commands.describe(riot_id="Name#Tag, z. B. Duality#EUW. Leer lassen, um die Verknüpfung zu lösen.")
    @app_commands.checks.cooldown(1, 30.0, key=per_member_cooldown)
    async def riot_id_command(interaction: discord.Interaction, riot_id: str | None = None):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Dieser Befehl funktioniert nur im Server.", ephemeral=True)
            return
        if not riot_id:
            await interaction.response.defer(ephemeral=True, thinking=True)
            update_profile(interaction.guild.id, interaction.user.id, riot_name=None, riot_tag=None, region=None, rank_tier=None, rank_synced_at=None)
            await set_exclusive_role(interaction.user, VALORANT_RANKS, None, "Riot-ID-Verknüpfung gelöst")
            await interaction.followup.send(
                "Deine Riot-ID ist nicht mehr verknüpft und deine Rangrolle wurde entfernt. "
                "Mit `/riot-id Name#Tag` kannst du sie jederzeit wieder verknüpfen.",
                ephemeral=True,
            )
            return
        parsed = parse_riot_id(riot_id)
        if parsed is None:
            await interaction.response.send_message("Bitte gib deine Riot-ID als `Name#Tag` an, z. B. `Duality#EUW`.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        update_profile(interaction.guild.id, interaction.user.id, riot_name=parsed[0], riot_tag=parsed[1], region=None)
        try:
            tier_name = await sync_member_rank(interaction.user, get_profile(interaction.guild.id, interaction.user.id))
        except RankLookupError as error:
            update_profile(interaction.guild.id, interaction.user.id, riot_name=None, riot_tag=None, region=None)
            await interaction.followup.send(f"Die Riot-ID konnte nicht verknüpft werden: {error}", ephemeral=True)
            return
        role_name = rank_role_name(tier_name)
        await interaction.followup.send(
            f"Verknüpft mit **{discord.utils.escape_markdown(parsed[0])}#{discord.utils.escape_markdown(parsed[1])}**. "
            f"Aktueller Rang: **{tier_name}**"
            + ("" if role_name else " (keine Rangrolle, solange du nicht gewertet bist)")
            + ". Der Bot aktualisiert ihn täglich.",
            ephemeral=True,
        )

    @tree.command(name="hochschule", description="Legt fest oder ändert, an welcher Hochschule du bist.")
    @app_commands.describe(hochschule="Deine Hochschule; „Keine Angabe“ entfernt die Rolle.")
    @app_commands.choices(hochschule=[
        *(app_commands.Choice(name=name, value=name) for name in UNIVERSITY_ROLES),
        app_commands.Choice(name="Keine Angabe", value=NO_ROLE_VALUE),
    ])
    async def university_command(interaction: discord.Interaction, hochschule: app_commands.Choice[str]):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Dieser Befehl funktioniert nur im Server.", ephemeral=True)
            return
        selected_role = None
        if hochschule.value != NO_ROLE_VALUE:
            selected_role = find_role(interaction.guild, hochschule.value)
            if selected_role is None:
                await interaction.response.send_message(
                    "Diese Hochschulrolle ist noch nicht eingerichtet. Bitte informiere den Staff.", ephemeral=True
                )
                return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await set_exclusive_role(interaction.user, UNIVERSITY_ROLES, selected_role, "Hochschule per /hochschule gewählt")
        text = (
            f"Deine Hochschule ist jetzt **{hochschule.value}**." if selected_role
            else "Deine Hochschule wird nicht mehr angezeigt."
        )
        await interaction.followup.send(text, ephemeral=True)

    @tree.command(name="meine-daten", description="Zeigt, exportiert oder löscht, was der Bot über dich speichert.")
    @app_commands.checks.cooldown(1, 60.0, key=per_member_cooldown)
    async def my_data_command(interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Dieser Befehl funktioniert nur im Server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = collect_user_data(interaction.guild.id, interaction.user.id)
        # Name, Kurs, Tracker-Link und Spielzeiten stehen in der Bewerbungsnachricht.
        staff_channel = find_text_channel(interaction.guild, "bewerbungen")
        for entry in data["bewerbungen"]:
            if staff_channel is None:
                break
            try:
                message = await staff_channel.fetch_message(entry["message_id"])
                entry["angaben"] = {field.name: field.value for field in message.embeds[0].fields} if message.embeds else {}
            except discord.HTTPException:
                entry["angaben"] = "Nachricht nicht mehr vorhanden"
        export = {
            "server": interaction.guild.name,
            "nutzer_id": interaction.user.id,
            "exportiert_am": discord.utils.utcnow().isoformat(),
            "daten": data,
            "hinweis": "Discord-Rollen, Nachrichten und Ticket-Verläufe liegen bei Discord selbst und sind hier nicht enthalten.",
        }
        file = discord.File(
            io.BytesIO(json.dumps(export, ensure_ascii=False, indent=2).encode()),
            filename="duality-meine-daten.json",
        )
        counts = ", ".join(f"{name}: {len(rows)}" for name, rows in data.items())
        await interaction.followup.send(
            f"Hier ist alles, was der Bot über dich gespeichert hat ({counts}).",
            file=file,
            view=MyDataView(interaction.user.id),
            ephemeral=True,
        )


async def setup_guild(guild: discord.Guild, apply: bool) -> None:
    print(f"Server: {guild.name} ({guild.id})")
    if apply:
        adopt_unscoped_migrations(guild.id)

    async def migrate(migration_key: str, migration) -> None:
        # Aufräumschritte laufen nur, bis sie als erledigt gespeichert sind.
        # So werden später bewusst angelegte Kanäle oder Rollen mit altem
        # Namen nicht bei jedem Neustart wieder gelöscht oder umgebaut.
        if migration_applied(guild.id, migration_key):
            return
        if await migration() and apply:
            mark_migration_applied(guild.id, migration_key)

    async def rename_old_categories() -> bool:
        category_renames = {
            "📌 START HIER": "👋 EINGANGSHALLE",
            "💬 COMMUNITY": "💬 LOUNGE",
            "🔎 TEAM-SUCHE": "📝 BEWERBER",
        }
        existing_category_names = {category.name for category in guild.categories}
        for old_name, new_name in category_renames.items():
            old_category = discord.utils.get(guild.categories, name=old_name)
            if old_category and new_name not in existing_category_names:
                if apply:
                    await old_category.edit(name=new_name, reason="Duality Mannheim Kanalstruktur aktualisiert")
                    existing_category_names.discard(old_name)
                    existing_category_names.add(new_name)
                    print(f"  Kategorie umbenannt: {old_name} → {new_name}")
                else:
                    print(f"  Kategorie würde umbenannt: {old_name} → {new_name}")
        return True

    await migrate("category_renames_v1", rename_old_categories)

    roles = managed_roles(guild)
    for name in ROLE_NAMES:
        if name in roles:
            print(f"  Rolle vorhanden: {name}")
        elif apply:
            role = await guild.create_role(name=name, reason="Duality Mannheim Setup")
            roles[name] = role
            remember_id(guild.id, "role", name, role.id)
            print(f"  Rolle erstellt: {name}")
        else:
            print(f"  Rolle würde erstellt: {name}")

    async def migrate_lft_role() -> bool:
        legacy_lft_role = discord.utils.get(guild.roles, name="LFT")
        applicant_role = roles.get("Bewerber")
        player_role = roles.get("Spieler")
        if not legacy_lft_role:
            return True
        if not applicant_role:
            return False
        old_lft_members = list(legacy_lft_role.members)
        if apply:
            for member in old_lft_members:
                if applicant_role not in member.roles and player_role not in member.roles:
                    await member.add_roles(applicant_role, reason="Bisherige LFT-Bewerbung auf Bewerber umgestellt")
                await member.remove_roles(legacy_lft_role, reason="Duality Mannheim verwendet keine LFT-Rolle mehr")
            print(f"  Alte LFT-Zuweisungen auf Bewerber umgestellt oder entfernt: {len(old_lft_members)}")
        elif old_lft_members:
            print(f"  Alte LFT-Zuweisungen würden auf Bewerber umgestellt oder entfernt: {len(old_lft_members)}")
        return True

    await migrate("lft_to_bewerber_v1", migrate_lft_role)

    async def remove_old_announcements() -> bool:
        old_announcements = next(
            (
                channel
                for channel in guild.text_channels
                if "".join(character for character in channel.name.casefold() if character.isalnum()) == "announcements"
            ),
            None,
        )
        if not old_announcements:
            return True
        if not apply:
            print("  Kanal würde entfernt: #announcements")
            return False
        try:
            await old_announcements.delete(
                reason="Duality Mannheim verwendet #ankündigungen statt #announcements"
            )
        except discord.HTTPException as error:
            if error.code != 50074:
                raise
            required_as = []
            if guild.rules_channel and guild.rules_channel.id == old_announcements.id:
                required_as.append("Regeln-/Guidelines-Kanal")
            if guild.public_updates_channel and guild.public_updates_channel.id == old_announcements.id:
                required_as.append("Community-Updates-Kanal")
            purpose = " und ".join(required_as) if required_as else "Pflichtkanal des Community-Servers"
            print(
                f"  #announcements bleibt bestehen: Discord verwendet ihn noch als {purpose}. "
                "Ändere die Zuordnung unter Servereinstellungen → Community → Übersicht und starte --apply erneut."
            )
            return False
        print("  Überflüssigen Kanal entfernt: #announcements")
        return True

    await migrate("remove_announcements_v1", remove_old_announcements)

    async def remove_old_valorant_announcements() -> bool:
        old_valorant_announcements = discord.utils.get(guild.text_channels, name="valo-announcements")
        if old_valorant_announcements:
            if apply:
                await old_valorant_announcements.delete(
                    reason="Duality Mannheim nutzt den zentralen Ankündigungskanal"
                )
                print("  Überflüssigen Kanal entfernt: #valo-announcements")
            else:
                print("  Kanal würde entfernt: #valo-announcements")
        return True

    await migrate("remove_valo_announcements_v1", remove_old_valorant_announcements)

    async def remove_old_tryout_category() -> bool:
        old_tryout_category = discord.utils.get(guild.categories, name="🧪 TRYOUTS")
        if old_tryout_category:
            if apply:
                for old_channel in list(old_tryout_category.channels):
                    await old_channel.delete(reason="Duality Mannheim entfernt Tryout-Kanäle")
                    print(f"  Alten Kanal entfernt: {old_channel.name}")
                await old_tryout_category.delete(reason="Duality Mannheim entfernt Tryout-Kategorie")
                print("  Alte Kategorie entfernt: 🧪 TRYOUTS")
            else:
                print("  Alte Tryout-Kategorie und alle darin liegenden Kanäle würden entfernt")
        return True

    await migrate("remove_tryouts_v1", remove_old_tryout_category)

    async def remove_old_training_category() -> bool:
        old_training_category = discord.utils.get(guild.categories, name="🏆 TRAINING & SCRIMS")
        if old_training_category:
            if apply:
                for old_channel in list(old_training_category.channels):
                    await old_channel.delete(reason="Duality Mannheim entfernt Training- und Scrim-Bereich")
                    print(f"  Alten Training-/Scrim-Kanal entfernt: {old_channel.name}")
                await old_training_category.delete(reason="Duality Mannheim entfernt Training- und Scrim-Bereich")
                print("  Alte Kategorie entfernt: 🏆 TRAINING & SCRIMS")
            else:
                print("  Training-/Scrim-Kategorie und alle darin enthaltenen Kanäle würden entfernt")
        return True

    await migrate("remove_training_scrims_v1", remove_old_training_category)

    async def remove_unwanted_voice_channels() -> bool:
        unwanted_voice_names = {"legion", "legions", "legionvc", "legionsvc", "radiants", "radiantsvc"}
        for old_voice_channel in list(guild.voice_channels):
            normalized_name = "".join(character for character in old_voice_channel.name.casefold() if character.isalnum())
            if normalized_name not in unwanted_voice_names:
                continue
            if apply:
                await old_voice_channel.delete(reason="Duality Mannheim entfernt nicht mehr gewünschte Legion-/Radiants-Voice-Channels")
                print(f"  Unerwünschten Voice-Channel entfernt: {old_voice_channel.name}")
            else:
                print(f"  Voice-Channel würde entfernt: {old_voice_channel.name}")
        return True

    await migrate("remove_legion_radiants_voice_v1", remove_unwanted_voice_channels)

    permissions_key = f"permissions_v{PERMISSIONS_VERSION}"
    sync_permissions = not migration_applied(guild.id, permissions_key)
    category_lookup = {category.name: category for category in guild.categories}
    for config in CATEGORIES:
        name = config["name"]
        category = stored_channel(guild, "category", name) or category_lookup.get(name)
        if category:
            remember_id(guild.id, "category", name, category.id)
        expected_overwrites = category_overwrites(guild, roles, config["roles"])
        if category is None:
            if not apply:
                print(f"  Kategorie würde erstellt: {name}")
                for kind, channel_name in config["channels"]:
                    print(f"    {kind.capitalize()}-Kanal würde erstellt: {channel_name}")
                continue
            category = await guild.create_category(
                name, overwrites=expected_overwrites, reason="Duality Mannheim Setup"
            )
            category_lookup[name] = category
            remember_id(guild.id, "category", name, category.id)
            print(f"  Kategorie erstellt: {name}")
        elif not sync_permissions:
            print(f"  Kategorie vorhanden: {name}")
        elif apply:
            await category.edit(overwrites=expected_overwrites, reason="Duality Mannheim Rollen-Zugänge")
            print(f"  Kategorie-Berechtigungen gesetzt: {name}")
        else:
            print(f"  Kategorie vorhanden: {name}; Zugänge würden aktualisiert")

        existing = {channel.name: channel for channel in category.channels}
        if name == "💬 LOUNGE" and not migration_applied(guild.id, "lounge_general_v1"):
            old_general = existing.get("general")
            if old_general and "allgemein" not in existing:
                if apply:
                    await old_general.edit(name="allgemein", reason="Duality Mannheim: allgemeiner Chat auf Deutsch")
                    existing.pop("general")
                    existing["allgemein"] = old_general
                    print("    Kanal umbenannt: general → allgemein")
                else:
                    print("    Kanal würde umbenannt: general → allgemein")
            elif old_general:
                if apply:
                    await old_general.delete(reason="Duality Mannheim entfernt doppelten #general-Kanal")
                    existing.pop("general")
                    print("    Doppelter Kanal entfernt: general")
                else:
                    print("    Doppelter Kanal general würde entfernt")
            if apply:
                mark_migration_applied(guild.id, "lounge_general_v1")

        if name == "📝 BEWERBER" and not migration_applied(guild.id, "applicant_chat_rename_v1"):
            old_applicant_chat = existing.get("looking-for-team")
            if old_applicant_chat and "bewerber-chat" not in existing:
                if apply:
                    await old_applicant_chat.edit(
                        name="bewerber-chat",
                        reason="Duality Mannheim benennt den Bewerberbereich um",
                    )
                    existing.pop("looking-for-team")
                    existing["bewerber-chat"] = old_applicant_chat
                    print("    Kanal umbenannt: looking-for-team → bewerber-chat")
                else:
                    print("    Kanal würde umbenannt: looking-for-team → bewerber-chat")
            elif old_applicant_chat:
                print("    Alter Kanal looking-for-team bleibt bestehen, da bewerber-chat bereits existiert")
            if apply:
                mark_migration_applied(guild.id, "applicant_chat_rename_v1")

        for kind, channel_name in config["channels"]:
            # Gespeicherte ID zuerst: Ein umbenannter oder verschobener Kanal wird
            # so wiedererkannt, statt dass ein zweiter angelegt wird.
            channel = stored_channel(guild, "channel", channel_name) or existing.get(channel_name)
            if channel:
                remember_id(guild.id, "channel", channel_name, channel.id)
                if not sync_permissions:
                    print(f"    Kanal vorhanden: {channel_name}")
                elif apply and channel_name in SPECIAL_CHANNEL_OVERWRITES:
                    await channel.edit(
                        overwrites=SPECIAL_CHANNEL_OVERWRITES[channel_name](guild, roles),
                        reason="Duality Mannheim Kanal-Zugänge",
                    )
                    print(f"    Eigene Zugänge gesetzt: {channel_name}")
                elif apply and not channel.permissions_synced:
                    await channel.edit(sync_permissions=True, reason="Duality Mannheim Kategorie-Zugänge")
                    print(f"    Kanal an Kategorie-Zugänge angepasst: {channel_name}")
                else:
                    print(f"    Kanal vorhanden: {channel_name}")
            elif apply:
                if kind == "voice":
                    channel = await guild.create_voice_channel(channel_name, category=category, reason="Duality Mannheim Setup")
                elif channel_name in SPECIAL_CHANNEL_OVERWRITES:
                    channel = await guild.create_text_channel(
                        channel_name,
                        category=category,
                        overwrites=SPECIAL_CHANNEL_OVERWRITES[channel_name](guild, roles),
                        reason="Duality Mannheim Setup mit eigenen Zugängen",
                    )
                else:
                    channel = await guild.create_text_channel(channel_name, category=category, reason="Duality Mannheim Setup")
                remember_id(guild.id, "channel", channel_name, channel.id)
                print(f"    {kind.capitalize()}-Kanal erstellt: {channel_name}")
            else:
                print(f"    {kind.capitalize()}-Kanal würde erstellt: {channel_name}")

    if apply and sync_permissions:
        mark_migration_applied(guild.id, permissions_key)
        print(f"  Rechte-Version {PERMISSIONS_VERSION} gespeichert; Neustarts lassen manuelle Rechte unverändert")

    async def import_legacy_applications() -> bool:
        staff_channel = find_text_channel(guild, "bewerbungen")
        if staff_channel is None:
            return False
        if not apply:
            print("  Bestehende Bewerbungen würden in die Datenbank übernommen")
            return False
        imported = 0
        async for message in staff_channel.history(limit=None):
            if message.author != guild.me or get_application(message.id):
                continue
            application = legacy_application(message)
            if application:
                save_application(application)
                imported += 1
        print(f"  {imported} bestehende Bewerbungen in die Datenbank übernommen")
        return True

    await migrate("applications_to_db_v1", import_legacy_applications)

    async def add_vote_buttons_to_open_applications() -> bool:
        staff_channel = find_text_channel(guild, "bewerbungen")
        if staff_channel is None or not apply:
            return False
        for application in find_pending_applications(guild.id):
            try:
                message = await staff_channel.fetch_message(application.message_id)
                await show_application(message, application)
            except discord.HTTPException:
                log.warning("Offene Bewerbung %s konnte nicht aktualisiert werden", application.message_id)
        print("  Offene Bewerbungen haben jetzt Abstimm-Buttons")
        return True

    await migrate("application_vote_buttons_v1", add_vote_buttons_to_open_applications)

    tickets_channel = find_text_channel(guild, "tickets")
    if apply and tickets_channel:
        tickets_embed = discord.Embed(
            title="Tickets an den Staff",
            description=(
                "Du hast ein Problem, möchtest etwas melden oder brauchst vertraulich Hilfe? "
                "Öffne über den Button ein Ticket oder nutze `/report`.\n\n"
                "Dabei entsteht ein privater Thread, den nur du und der Staff sehen. Dort könnt ihr Rückfragen klären. "
                "Du oder der Staff könnt das Ticket schließen, sobald alles geklärt ist."
            ),
            color=discord.Color.from_rgb(110, 70, 255),
        )
        tickets_embed.set_footer(text=TICKETS_MARKER)
        found_tickets_message = False
        async for message in tickets_channel.history(limit=50):
            if message.author == guild.me and message.embeds and message.embeds[0].footer.text == TICKETS_MARKER:
                await message.edit(embed=tickets_embed, view=TicketStartView())
                found_tickets_message = True
                break
        if not found_tickets_message:
            await tickets_channel.send(embed=tickets_embed, view=TicketStartView())
            print("  Ticket-Start in #tickets veröffentlicht")

    application_channel = find_text_channel(guild, "bewerbung")
    if apply and application_channel:
        embed = discord.Embed(
            title="Bewerbung für Duality Mannheim",
            description=(
                "Du möchtest für Duality Mannheim Valorant spielen? Dieser Kanal ist für alle Mitglieder sichtbar. "
                "Starte hier über den Button deine Bewerbung. Nach dem Absenden erhältst du die Rolle `Bewerber`; "
                "danach ist dieser Kanal für dich bis zur nächsten Saison nicht mehr sichtbar.\n\n"
                "Du brauchst deinen Tracker.gg-Profillink und bestätigst, dass du mindestens 16 bist. "
                "Außerdem wählst du deine Main-Rolle (Duelist, Initiator, Controller oder Sentinel), alle Rollen, "
                "die du spielen kannst, sowie deine Verfügbarkeit pro Abend von Mittwoch bis Sonntag aus. "
                "Du kannst zum Beispiel Mittwoch 19–22 Uhr und Freitag 20–23 Uhr getrennt auswählen. "
                "Wir fragen auch deinen bevorzugten Namen und optional Kurs/Ausbildung ab.\n\n"
                "Deine Angaben werden privat an den Staff geschickt. Nach dem Absenden erhältst du Zugang zu `#bewerber-chat`, "
                "wo du Fragen zum Ablauf stellen kannst. "
                "Auch bei einer Ablehnung bleibt die Rolle `Bewerber` bis zur nächsten Saison bestehen; vor jeder neuen Saison "
                "kannst du dich erneut bewerben. Wenn du Rückfragen hast, wende dich direkt an den Staff. "
                f"Abgelehnte Bewerbungseinträge werden nach {APPLICATION_RETENTION_DAYS} Tagen gelöscht."
            ),
            color=discord.Color.from_rgb(110, 70, 255),
        )
        embed.set_footer(text=APPLICATION_MARKER)
        found_application_message = False
        async for message in application_channel.history(limit=50):
            if message.author == guild.me and message.embeds:
                marker = message.embeds[0].footer.text
                if marker in {"DUALITY_ONBOARDING_V1", "DUALITY_ONBOARDING_V2", APPLICATION_MARKER}:
                    await message.edit(embed=embed, view=OnboardingView())
                    print("  Bewerbungsnachricht in #bewerbung aktualisiert")
                    found_application_message = True
                    break
        if not found_application_message:
            await application_channel.send(embed=embed, view=OnboardingView())
            print("  Bewerbungsstart in #bewerbung veröffentlicht")

    rules_channel = find_text_channel(guild, "regeln")
    if apply and rules_channel:
        rules_embed = discord.Embed(
            title="Regeln von Duality Mannheim",
            description=(
                "Mit deiner Teilnahme am Server und an unseren Teamaktivitäten akzeptierst du diese Regeln:\n\n"
                "**1. Respektvoller Umgang** — Behandle Mitglieder freundlich. Belästigung, Drohungen, "
                "Diskriminierung und persönliche Angriffe sind nicht erlaubt.\n\n"
                "**2. Schutz der Privatsphäre** — Teile keine privaten Daten oder Nachrichten anderer Personen "
                "ohne deren Zustimmung. Bewerbungsangaben bleiben dem Staff vorbehalten.\n\n"
                "**3. Passende Kanäle und kein Spam** — Nutze die Kanäle passend zu ihrem Thema. Spam, "
                "unerlaubte Werbung und störendes Verhalten sind nicht erlaubt.\n\n"
                "**4. Fair Play** — Cheating, unerlaubte Exploits und absichtlicher Betrug sind im Teamkontext "
                "nicht akzeptiert.\n\n"
                "**5. Aufnahmen und Streams** — Stimme Aufnahmen oder Streams in gemeinsamen Sprachkanälen "
                "vorher mit allen Beteiligten ab.\n\n"
                "**6. DHBW Mannheim** — Duality Mannheim ist ein offiziell von der DHBW Mannheim anerkanntes "
                "Team. Für Teamaktivitäten gelten zusätzlich die jeweils aktuellen, für uns einschlägigen "
                "Regeln und Richtlinien der DHBW Mannheim. Bei Unsicherheit wende dich an den Staff.\n\n"
                "**7. Team-Bewerbung ab 16** — Für eine Bewerbung ins Valorant-Team musst du mindestens 16 Jahre "
                "alt sein. Diese Voraussetzung gilt für Team-Bewerbungen.\n\n"
                "**8. Moderation und Hilfe** — Folge den begründeten Anweisungen des Staffs. Bei Problemen kannst "
                "du `/report` nutzen oder dich vertraulich an Coach oder Admin wenden; Ideen kannst du mit `/feedback` senden."
            ),
            color=discord.Color.from_rgb(110, 70, 255),
        )
        rules_embed.set_footer(text=RULES_MARKER)
        found_rules_message = False
        async for message in rules_channel.history(limit=50):
            if message.author == guild.me and message.embeds:
                if message.embeds[0].footer.text == RULES_MARKER:
                    await message.edit(embed=rules_embed)
                    print("  Regelwerk in #regeln aktualisiert")
                    found_rules_message = True
                    break
        if not found_rules_message:
            await rules_channel.send(embed=rules_embed)
            print("  Regelwerk in #regeln veröffentlicht")

    welcome = find_text_channel(guild, "willkommen")
    if apply and welcome:
        old_markers = {"DUALITY_ONBOARDING_V1", "DUALITY_ONBOARDING_V2", APPLICATION_MARKER}
        async for message in welcome.history(limit=50):
            if message.author == guild.me and message.embeds and message.embeds[0].footer.text in old_markers:
                await message.delete()
                print("  Alte Bewerbungsnachricht aus #willkommen entfernt")

        welcome_embed = discord.Embed(
            title="Willkommen bei Duality Mannheim! 👋",
            description=(
                "Schön, dass du da bist! Dieser Server ist für alle Mitglieder da – egal, ob du Valorant spielst "
                "oder erst einmal die Community kennenlernen möchtest.\n\n"
                "**Zum Ankommen**\n"
                "• Lies zuerst `#regeln`.\n"
                "• Stell dich in `#allgemein` vor und lerne die Community kennen.\n\n"
                "**Du möchtest dich fürs Valorant-Team bewerben?**\n"
                "`#bewerbung` in der Valorant-Kategorie ist für alle sichtbar. Dort kannst du über den Button deine Bewerbung starten. "
                "Nach dem Absenden erhältst du die Rolle `Bewerber` und kannst den Kanal bis zur nächsten Saison weder sehen noch erneut nutzen. "
                "Für Fragen zum Bewerbungsablauf kannst du anschließend in `#bewerber-chat` schreiben. "
                "Bewerbungen sind "
                "ab 16 Jahren möglich; besonders wichtig sind Spielzeiten Mittwoch bis Sonntag ab 19 Uhr. "
                "Nach dem Absenden prüft der Staff deine Bewerbung. Zugang zum Teambereich gibt es erst nach einer Annahme.\n\n"
                "**Rang, Hochschule und Profil**\n"
                "• `/riot-id Name#Tag`: verknüpft deine Riot-ID. Der Bot holt deinen Valorant-Rang automatisch "
                "und aktualisiert ihn täglich. `/riot-id` ohne Angabe löst die Verknüpfung wieder.\n"
                "• `/hochschule`: ändert deine Hochschule (beim Beitritt fragt Discord sie schon ab).\n"
                "• `/profil`: zeigt dein Profil mit Rang, Hochschule und Rollen.\n\n"
                "**Eigener Voice-Channel**\n"
                "Betritt `➕ Neuer Voice` oder `➕ Neuer Valorant-Voice`, dann bekommst du einen eigenen Kanal. "
                "Er verschwindet, sobald er leer ist.\n\n"
                "**Benachrichtigungen**\n"
                "Nutze die Buttons unter dieser Nachricht, um freiwillig Pings für Valorant-Updates oder Community-Events zu erhalten. "
                "Ein zweiter Klick schaltet sie wieder ab.\n\n"
                "Alle Befehle findest du mit `/help`."
            ),
            color=discord.Color.from_rgb(110, 70, 255),
        )
        welcome_embed.set_footer(text=WELCOME_MARKER)
        found_welcome_message = False
        async for message in welcome.history(limit=50):
            if message.author == guild.me and message.embeds:
                if message.embeds[0].footer.text == WELCOME_MARKER:
                    await message.edit(embed=welcome_embed, view=NotificationView())
                    print("  Willkommensnachricht in #willkommen aktualisiert")
                    found_welcome_message = True
                    break
        if not found_welcome_message:
            await welcome.send(embed=welcome_embed, view=NotificationView())
            print("  Willkommensnachricht in #willkommen veröffentlicht")


async def main(apply: bool) -> None:
    print("Duality-Setup wird gestartet.", flush=True)
    print(f"Python {sys.version.split()[0]} | discord.py {discord.__version__}", flush=True)
    token = os.getenv("DISCORD_BOT_TOKEN")
    guild_id = os.getenv("DISCORD_GUILD_ID", DEFAULT_GUILD_ID)
    if not token:
        print("Der Bot-Token wird jetzt verdeckt abgefragt; beim Tippen erscheint kein Zeichen.", flush=True)
        token = getpass.getpass("Bot-Token: ").strip()
    else:
        print("Bot-Token aus DISCORD_BOT_TOKEN gelesen.", flush=True)
    if not token:
        raise SystemExit("Es wurde kein Discord-Bot-Token eingegeben.")
    if not guild_id:
        raise SystemExit("Bitte DISCORD_GUILD_ID als Umgebungsvariable setzen.")
    try:
        target_id = int(guild_id)
    except ValueError:
        raise SystemExit("DISCORD_GUILD_ID muss eine numerische Server-ID sein.")

    print(f"Zielserver-ID: {target_id}", flush=True)
    print("Modus: Änderungen anwenden und Bot online halten." if apply else "Modus: Vorschau ohne Änderungen.", flush=True)
    # Auch die Vorschau braucht die Datenbank, um erledigte Aufräumschritte zu erkennen.
    initialize_storage()
    global RANK_CLIENT
    henrikdev_key = os.getenv("HENRIKDEV_API_KEY", "").strip()
    if henrikdev_key:
        RANK_CLIENT = HenrikDevClient(henrikdev_key)
        print("HenrikDev-API-Key aus HENRIKDEV_API_KEY gelesen; automatische Rangabfrage aktiv.", flush=True)
    else:
        print("HENRIKDEV_API_KEY fehlt; die automatische Rangabfrage ist ausgeschaltet.", flush=True)
    intents = discord.Intents.none()
    intents.guilds = True
    intents.members = True
    # Für Join-to-Create-Voice und gelöschte Nachrichten im Audit-Log; beide nicht privilegiert.
    intents.voice_states = True
    intents.guild_messages = True
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)
    register_app_commands(tree)
    client.add_view(OnboardingView())
    client.add_view(ApplicationReviewView())
    client.add_view(NotificationView())
    client.add_view(EventRSVPView())
    client.add_view(TicketStartView())
    client.add_view(TicketCloseView())

    @tasks.loop(hours=24)
    async def sync_ranks():
        guild = client.get_guild(target_id)
        if guild is None or RANK_CLIENT is None:
            return
        synced = 0
        for profile in linked_profiles(guild.id):
            member = guild.get_member(profile.user_id)
            if member is None:
                continue
            try:
                await sync_member_rank(member, profile)
                synced += 1
            except (RankLookupError, discord.HTTPException) as error:
                log.warning("Rang von %s konnte nicht aktualisiert werden: %s", profile.user_id, error)
            # Abstand halten, damit der API-Key nicht ins Rate-Limit läuft.
            await asyncio.sleep(RANK_SYNC_DELAY_SECONDS)
        log.info("%s Ränge über Riot-ID aktualisiert", synced)

    @sync_ranks.error
    async def sync_ranks_error(error: Exception):
        log.error("Fehler beim täglichen Rang-Abgleich", exc_info=error)

    @tasks.loop(hours=24)
    async def purge_declined_applications():
        now = int(time.time())
        removed_events = delete_past_events(now - EVENT_RETENTION_DAYS * 24 * 60 * 60)
        if removed_events:
            log.info("%s vergangene Termine aus der Datenbank entfernt", removed_events)

        guild = client.get_guild(target_id)
        if guild is None:
            return
        staff_channel = find_text_channel(guild, "bewerbungen")
        if staff_channel is None:
            return
        cutoff = now - APPLICATION_RETENTION_DAYS * 24 * 60 * 60
        for application in expired_applications(guild.id, cutoff):
            try:
                message = await staff_channel.fetch_message(application.message_id)
                await message.delete()
            except discord.NotFound:
                pass
            delete_application(application.message_id)
            log.info("Bewerbung nach %s Tagen gelöscht", APPLICATION_RETENTION_DAYS)

    @purge_declined_applications.error
    async def purge_declined_applications_error(error: Exception):
        log.error("Fehler bei der Bewerbungs-Löschfrist", exc_info=error)

    @tasks.loop(minutes=1)
    async def event_reminders():
        await send_due_event_reminders(client)

    @event_reminders.error
    async def event_reminders_error(error: Exception):
        log.error("Fehler bei den Termin-Erinnerungen", exc_info=error)

    setup_complete = False
    commands_synced = False
    existing_events_registered = False

    @client.event
    async def on_connect():
        print("Gateway-Verbindung hergestellt; Discord authentifiziert den Bot ...", flush=True)

    @client.event
    async def on_disconnect():
        print("Gateway-Verbindung getrennt; discord.py versucht ggf. erneut zu verbinden.", flush=True)

    @client.event
    async def on_resumed():
        print("Discord-Sitzung erfolgreich fortgesetzt.", flush=True)

    @client.event
    async def on_member_remove(member: discord.Member):
        if apply and member.guild.id == target_id:
            await audit(member.guild, f"**Server verlassen:** {member.mention} (`{member.id}`, {member})", discord.Color.orange())
            await close_applications_of_departed_member(member)

    @client.event
    async def on_member_join(member: discord.Member):
        if apply and member.guild.id == target_id:
            created = int(member.created_at.timestamp())
            await audit(
                member.guild,
                f"**Beigetreten:** {member.mention} (`{member.id}`, {member}) · Account erstellt <t:{created}:R>",
                discord.Color.green(),
            )

    @client.event
    async def on_member_update(before: discord.Member, after: discord.Member):
        if apply and after.guild.id == target_id:
            await audit_role_changes(before, after)

    @client.event
    async def on_raw_message_delete(payload: discord.RawMessageDeleteEvent):
        guild = client.get_guild(payload.guild_id) if payload.guild_id else None
        if apply and guild and guild.id == target_id:
            await audit_message_delete(guild, payload)

    @client.event
    async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if apply and member.guild.id == target_id and before.channel != after.channel:
            await handle_temp_voice(member, before, after)

    @client.event
    async def on_ready():
        nonlocal setup_complete, commands_synced, existing_events_registered
        print(f"Mit Discord verbunden als {client.user}.", flush=True)
        guild = client.get_guild(target_id)
        if guild is None:
            log.error("Bot nicht im Server oder Server-ID falsch. Prüfe die Server-ID und lade den Bot in den Server ein.")
            await client.close()
            return
        if not setup_complete:
            print(f"Server gefunden: {guild.name}. Richte Serverstruktur ein ...", flush=True)
            await setup_guild(guild, apply)
            setup_complete = True
        if apply:
            if not existing_events_registered:
                imported_events = await register_existing_scheduled_events(guild)
                existing_events_registered = True
                print(f"  {imported_events} bestehende Termine für Erinnerungen übernommen")
            if not commands_synced:
                guild_object = discord.Object(id=target_id)
                tree.copy_global_to(guild=guild_object)
                await tree.sync(guild=guild_object)
                commands_synced = True
                print("  Slash-Befehle für diesen Server veröffentlicht")
            await clean_up_temp_voice(guild)
            if not purge_declined_applications.is_running():
                purge_declined_applications.start()
            if not sync_ranks.is_running():
                sync_ranks.start()
            if not event_reminders.is_running():
                event_reminders.start()
            print("Onboarding-Bot läuft. Mit Ctrl+C beenden.")
        else:
            await client.close()

    panel_runner = None
    try:
        if os.getenv("PANEL_ENABLED") == "1":
            if apply:
                from panel import PanelHooks, start_panel

                panel_runner = await start_panel(PanelHooks(
                    client=client,
                    guild_id=target_id,
                    staff_level=staff_level,
                    audit=audit,
                ))
            else:
                print("PANEL_ENABLED ist gesetzt, das Web-Panel läuft aber nur mit --apply.", flush=True)
        async with client:
            print("Verbinde mit Discord ...", flush=True)
            await client.start(token)
    finally:
        if panel_runner:
            await panel_runner.cleanup()
        if RANK_CLIENT:
            await RANK_CLIENT.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Duality Mannheim Discord-Setup und Mitglieder-Onboarding")
    parser.add_argument("--apply", action="store_true", help="Setup anwenden und Onboarding-Bot online halten")
    parser.add_argument("--debug", action="store_true", help="Zusätzliche Gateway-Verbindungsdetails ausgeben")
    args = parser.parse_args()
    if args.debug:
        logging.getLogger("discord.gateway").setLevel(logging.DEBUG)
        print("Gateway-Diagnose ist aktiviert.", flush=True)
    try:
        asyncio.run(main(args.apply))
    except KeyboardInterrupt:
        print("Bot beendet.")
