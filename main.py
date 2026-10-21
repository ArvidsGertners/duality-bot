"""Set up Duality Mannheim's Discord and run its member onboarding bot.

Preview: python main.py
Apply and keep the bot online: python main.py --apply

The bot token can come from an environment variable or a hidden terminal
prompt. The guild ID defaults to Duality Mannheim. Never put a token in this
file. --apply creates missing items and updates managed permissions.
"""

import argparse
import asyncio
import functools
import io
import json
import math
import sqlite3
import getpass
import logging
import os
import re
import time
import sys
import weakref
from collections import deque
from collections.abc import Callable, Sequence
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, time as day_time
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
STARTED_AT = time.time()
# Für Panel und Alarme: Verbindungszustand und wann jeder Hintergrund-Task zuletzt lief oder scheiterte.
GATEWAY_STATE = {"connected": False, "changed_at": int(STARTED_AT)}
LOOP_STATUS: dict[str, dict] = {}
# Wer live über Änderungen im Server Bescheid wissen will (das Panel), trägt sich hier ein.
STATE_LISTENERS: list[Callable[[str], None]] = []

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
# Exit-Code für „Neustart aus dem Panel“. Nicht 0, damit systemd den Dienst
# sowohl mit Restart=always als auch mit Restart=on-failure wieder startet.
RESTART_EXIT_CODE = 75


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
        connection.execute(
            "CREATE TABLE IF NOT EXISTS bot_settings ("
            "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_by INTEGER, updated_at INTEGER NOT NULL)"
        )
        # Ankündigungen aus dem Panel: sofort gesendete und geplante (sent_at NULL = noch offen).
        connection.execute(
            "CREATE TABLE IF NOT EXISTS scheduled_announcements ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, guild_id INTEGER NOT NULL, channel_id INTEGER NOT NULL, "
            "payload TEXT NOT NULL, send_at INTEGER, created_by INTEGER, created_at INTEGER NOT NULL, "
            "updated_by INTEGER, updated_at INTEGER, sent_at INTEGER, message_id INTEGER, error TEXT)"
        )
        # Texte mit Verlauf: der neueste Eintrag je Schlüssel gilt, value NULL heißt „Standard“.
        connection.execute(
            "CREATE TABLE IF NOT EXISTS bot_texts ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT NOT NULL, value TEXT, updated_by INTEGER, "
            "updated_at INTEGER NOT NULL)"
        )
        connection.execute("CREATE INDEX IF NOT EXISTS bot_texts_key ON bot_texts (key, id)")
        # Rangverlauf für die Rangliste; ein Eintrag nur, wenn sich der Rang ändert.
        connection.execute(
            "CREATE TABLE IF NOT EXISTS rank_history ("
            "guild_id INTEGER NOT NULL, user_id INTEGER NOT NULL, rank_tier TEXT NOT NULL, recorded_at INTEGER NOT NULL)"
        )
        connection.execute("CREATE INDEX IF NOT EXISTS rank_history_member ON rank_history (guild_id, user_id, recorded_at)")
        # Spalten, die spätere Versionen ergänzt haben; bestehende Zeilen behalten ihre Daten.
        event_columns = {row[1] for row in connection.execute("PRAGMA table_info(scheduled_events)")}
        for column, definition in (("title", "TEXT"), ("details", "TEXT"), ("cancelled_at", "INTEGER")):
            if column not in event_columns:
                connection.execute(f"ALTER TABLE scheduled_events ADD COLUMN {column} {definition}")
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


def list_applications(guild_id: int) -> list[Application]:
    with open_db() as connection:
        return [
            Application(*row)
            for row in connection.execute(
                f"SELECT {APPLICATION_COLUMNS} FROM applications WHERE guild_id = ? ORDER BY submitted_at DESC",
                (guild_id,),
            )
        ]


def application_vote_counts(guild_id: int) -> dict[int, tuple[int, int]]:
    """(👍, 👎) je Bewerbungsnachricht."""
    with open_db() as connection:
        rows = connection.execute(
            "SELECT vote.message_id, SUM(vote.vote = 'yes'), SUM(vote.vote = 'no') FROM application_votes AS vote "
            "JOIN applications AS application ON application.message_id = vote.message_id "
            "WHERE application.guild_id = ? GROUP BY vote.message_id",
            (guild_id,),
        ).fetchall()
    return {message_id: (yes, no) for message_id, yes, no in rows}


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


def list_profiles(guild_id: int) -> list[Profile]:
    with open_db() as connection:
        return [
            Profile(*row)
            for row in connection.execute(
                f"SELECT guild_id, user_id, {', '.join(PROFILE_FIELDS)}, updated_at FROM player_profiles WHERE guild_id = ?",
                (guild_id,),
            )
        ]


def record_rank(guild_id: int, user_id: int, rank_tier: str, recorded_at: int) -> bool:
    """Neuen Rang in den Verlauf schreiben, aber nur, wenn er sich geändert hat."""
    with open_db() as connection:
        last = connection.execute(
            "SELECT rank_tier FROM rank_history WHERE guild_id = ? AND user_id = ? ORDER BY recorded_at DESC, rowid DESC LIMIT 1",
            (guild_id, user_id),
        ).fetchone()
        if last and last[0] == rank_tier:
            return False
        connection.execute(
            "INSERT INTO rank_history (guild_id, user_id, rank_tier, recorded_at) VALUES (?, ?, ?, ?)",
            (guild_id, user_id, rank_tier, recorded_at),
        )
    return True


def rank_histories(guild_id: int) -> dict[int, list[tuple[str, int]]]:
    histories: dict[int, list[tuple[str, int]]] = {}
    with open_db() as connection:
        for user_id, rank_tier, recorded_at in connection.execute(
            "SELECT user_id, rank_tier, recorded_at FROM rank_history WHERE guild_id = ? ORDER BY recorded_at, rowid",
            (guild_id,),
        ):
            histories.setdefault(user_id, []).append((rank_tier, recorded_at))
    return histories


def attendance(guild_id: int, user_id: int, since: int, until: int) -> tuple[int, int]:
    """(Zusagen, Termine) für vergangene Termine im Zeitraum."""
    with open_db() as connection:
        total = connection.execute(
            "SELECT COUNT(*) FROM scheduled_events WHERE guild_id = ? AND starts_at BETWEEN ? AND ? "
            "AND cancelled_at IS NULL",
            (guild_id, since, until),
        ).fetchone()[0]
        going = connection.execute(
            "SELECT COUNT(*) FROM scheduled_events AS event JOIN event_rsvps AS rsvp "
            "ON rsvp.message_id = event.message_id WHERE event.guild_id = ? AND event.starts_at BETWEEN ? AND ? "
            "AND event.cancelled_at IS NULL AND rsvp.user_id = ? AND rsvp.choice = 'going'",
            (guild_id, since, until, user_id),
        ).fetchone()[0]
    return going, total


ATTENDANCE_THRESHOLD_PERCENT = 50
# Ab so vielen Terminen ohne Reaktion wird eine Person in der Übersicht markiert.
ATTENDANCE_MISSING_LIMIT = 2


def attendance_events(guild_id: int, since: int, until: int) -> tuple[list[tuple[int, int, str | None]], dict[int, dict[int, str]]]:
    """Stattgefundene, nicht abgesagte Termine im Zeitraum und die Antworten darauf."""
    with open_db() as connection:
        events = connection.execute(
            "SELECT message_id, starts_at, title FROM scheduled_events WHERE guild_id = ? AND starts_at BETWEEN ? AND ? "
            "AND cancelled_at IS NULL ORDER BY starts_at",
            (guild_id, since, until),
        ).fetchall()
        rows = connection.execute(
            "SELECT rsvp.message_id, rsvp.user_id, rsvp.choice FROM event_rsvps AS rsvp "
            "JOIN scheduled_events AS event ON event.message_id = rsvp.message_id WHERE event.guild_id = ? "
            "AND event.starts_at BETWEEN ? AND ? AND event.cancelled_at IS NULL",
            (guild_id, since, until),
        ).fetchall()
    responses: dict[int, dict[int, str]] = {}
    for message_id, user_id, choice in rows:
        responses.setdefault(message_id, {})[user_id] = choice
    return events, responses


def summarize_attendance(
    events: Sequence[tuple[int, int, str | None]], responses: dict[int, dict[int, str]], members: dict[int, int]
) -> dict[int, dict]:
    """Pro Person (user_id → Beitritt) Zusagen, Vielleicht, Absagen, keine Reaktion und Quote.
    Termine vor dem Beitritt zählen nicht."""
    summary = {}
    for user_id, joined_at in members.items():
        counts = {"going": 0, "maybe": 0, "declined": 0, "none": 0}
        history = []
        for message_id, starts_at, title in events:
            if starts_at < joined_at:
                continue
            choice = responses.get(message_id, {}).get(user_id, "none")
            counts[choice] += 1
            history.append({"message_id": message_id, "starts_at": starts_at, "title": title, "choice": choice})
        total = sum(counts.values())
        rate = round(100 * counts["going"] / total) if total else None
        summary[user_id] = {
            **counts,
            "total": total,
            "rate": rate,
            "flagged": bool(total) and (
                rate < setting("attendance_threshold_percent") or counts["none"] >= setting("attendance_missing_limit")
            ),
            "history": history,
        }
    return summary


def save_ticket(thread_id: int, guild_id: int, reporter_id: int) -> None:
    with open_db() as connection:
        connection.execute(
            "INSERT OR IGNORE INTO tickets (thread_id, guild_id, reporter_id, created_at) VALUES (?, ?, ?, ?)",
            (thread_id, guild_id, reporter_id, int(time.time())),
        )


def list_tickets(guild_id: int) -> list[tuple[int, int, int, int | None]]:
    """(Thread, meldende Person, erstellt, geschlossen), neueste zuerst."""
    with open_db() as connection:
        return connection.execute(
            "SELECT thread_id, reporter_id, created_at, closed_at FROM tickets WHERE guild_id = ? ORDER BY created_at DESC",
            (guild_id,),
        ).fetchall()


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


def temp_voice_owners(guild_id: int) -> dict[int, int]:
    with open_db() as connection:
        return dict(connection.execute(
            "SELECT channel_id, owner_id FROM temp_voice_channels WHERE guild_id = ?", (guild_id,)
        ).fetchall())


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
            "rangverlauf": rows(
                "SELECT rank_tier, recorded_at FROM rank_history WHERE guild_id = ? AND user_id = ? ORDER BY recorded_at",
                (guild_id, user_id),
            ),
        }


def delete_user_data(guild_id: int, user_id: int) -> None:
    # Staff-Stimmen und Tickets bleiben als Nachweis für Entscheidungen und
    # Meldungen erhalten; Bewerbungen löscht der Aufrufer zusammen mit ihren Nachrichten.
    with open_db() as connection:
        connection.execute("DELETE FROM player_profiles WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        connection.execute("DELETE FROM player_roster_names WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))
        connection.execute("DELETE FROM event_rsvps WHERE user_id = ?", (user_id,))
        connection.execute("DELETE FROM rank_history WHERE guild_id = ? AND user_id = ?", (guild_id, user_id))


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


def save_scheduled_event(
    message_id: int, guild_id: int, channel_id: int, starts_at: int, title: str | None = None, details: str | None = None
) -> None:
    # Ein schon gespeicherter Titel bleibt; ändern geht nur über update_scheduled_event.
    with open_db() as connection:
        connection.execute(
            "INSERT INTO scheduled_events (message_id, guild_id, channel_id, starts_at, title, details) "
            "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(message_id) DO UPDATE SET "
            "guild_id = excluded.guild_id, channel_id = excluded.channel_id, starts_at = excluded.starts_at, "
            "title = COALESCE(scheduled_events.title, excluded.title), "
            "details = COALESCE(scheduled_events.details, excluded.details)",
            (message_id, guild_id, channel_id, starts_at, title, details),
        )


class ScheduledEvent(NamedTuple):
    message_id: int
    guild_id: int
    channel_id: int
    starts_at: int
    title: str | None
    details: str | None
    cancelled_at: int | None


EVENT_COLUMNS = "message_id, guild_id, channel_id, starts_at, title, details, cancelled_at"


def get_scheduled_event(message_id: int) -> ScheduledEvent | None:
    with open_db() as connection:
        row = connection.execute(
            f"SELECT {EVENT_COLUMNS} FROM scheduled_events WHERE message_id = ?", (message_id,)
        ).fetchone()
    return ScheduledEvent(*row) if row else None


def update_scheduled_event(message_id: int, title: str, details: str, starts_at: int) -> None:
    """Neue Angaben speichern; bei neuer Uhrzeit dürfen die Erinnerungen noch einmal raus."""
    with open_db() as connection:
        previous = connection.execute(
            "SELECT starts_at FROM scheduled_events WHERE message_id = ?", (message_id,)
        ).fetchone()
        connection.execute(
            "UPDATE scheduled_events SET title = ?, details = ?, starts_at = ? WHERE message_id = ?",
            (title, details, starts_at, message_id),
        )
        if previous and previous[0] != starts_at:
            connection.execute("DELETE FROM event_reminders_sent WHERE message_id = ?", (message_id,))


def cancel_scheduled_event(message_id: int) -> bool:
    with open_db() as connection:
        cursor = connection.execute(
            "UPDATE scheduled_events SET cancelled_at = ? WHERE message_id = ? AND cancelled_at IS NULL",
            (int(time.time()), message_id),
        )
    return cursor.rowcount == 1


def fill_event_text(message_id: int, title: str | None, details: str | None) -> None:
    """Titel für Termine nachtragen, die vor dem Speichern der Titel angelegt wurden."""
    with open_db() as connection:
        connection.execute(
            "UPDATE scheduled_events SET title = ?, details = ? WHERE message_id = ? AND title IS NULL",
            (title, details, message_id),
        )


def list_events(guild_id: int) -> list[ScheduledEvent]:
    with open_db() as connection:
        return [
            ScheduledEvent(*row)
            for row in connection.execute(
                f"SELECT {EVENT_COLUMNS} FROM scheduled_events WHERE guild_id = ? ORDER BY starts_at", (guild_id,)
            )
        ]


def event_rsvp_overview(guild_id: int) -> dict[int, dict[str, list[int]]]:
    """Zu- und Absagen je Termin."""
    overview: dict[int, dict[str, list[int]]] = {}
    with open_db() as connection:
        rows = connection.execute(
            "SELECT rsvp.message_id, rsvp.choice, rsvp.user_id FROM event_rsvps AS rsvp "
            "JOIN scheduled_events AS event ON event.message_id = rsvp.message_id WHERE event.guild_id = ? "
            "ORDER BY rsvp.user_id",
            (guild_id,),
        ).fetchall()
    for message_id, choice, user_id in rows:
        overview.setdefault(message_id, {}).setdefault(choice, []).append(user_id)
    return overview


def event_reminder_overview(guild_id: int) -> dict[int, dict[str, int]]:
    """Gesendete Erinnerungen je Termin: Schlüssel (24h, 1h) → Zeitpunkt."""
    overview: dict[int, dict[str, int]] = {}
    with open_db() as connection:
        rows = connection.execute(
            "SELECT sent.message_id, sent.reminder_key, sent.sent_at FROM event_reminders_sent AS sent "
            "JOIN scheduled_events AS event ON event.message_id = sent.message_id WHERE event.guild_id = ?",
            (guild_id,),
        ).fetchall()
    for message_id, key, sent_at in rows:
        overview.setdefault(message_id, {})[key] = sent_at
    return overview


def get_upcoming_events(now: int) -> list[tuple[int, int, int, int]]:
    with open_db() as connection:
        return connection.execute(
            "SELECT message_id, guild_id, channel_id, starts_at FROM scheduled_events "
            "WHERE starts_at > ? AND cancelled_at IS NULL ORDER BY starts_at",
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
        # Zustand fürs Panel; „nicht gefunden“ zählt nicht als Störung der API.
        self.failures_in_row = 0
        self.last_error: str | None = None
        self.last_error_at: int | None = None
        self.rate_limited_until = 0.0

    def record_failure(self, text: str) -> None:
        self.failures_in_row += 1
        self.last_error = text
        self.last_error_at = int(time.time())

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
                    self.rate_limited_until = time.time() + min(wait, 120)
                    await asyncio.sleep(min(wait, 120))
                    continue
                if response.status == 404:
                    self.failures_in_row = 0
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
                    self.record_failure(f"Status {response.status}")
                    raise RankLookupError(f"Die HenrikDev-API antwortet gerade mit Status {response.status}.")
                self.failures_in_row = 0
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
            self.record_failure("nicht erreichbar")
            raise RankLookupError("Die HenrikDev-API ist gerade nicht erreichbar.") from error
        except (KeyError, TypeError) as error:
            self.record_failure("unerwartete Antwort")
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


RANK_CONGRATS_CHANNEL = "team-chat"


def rank_value(tier_name: str | None) -> tuple[int, int] | None:
    """'Gold 2' → (3, 2), 'Radiant' → (8, 0); 'Unrated' und Unbekanntes → None. Vergleichbar per <."""
    if not tier_name:
        return None
    base, _, level = tier_name.partition(" ")
    base = base.capitalize()
    if base not in VALORANT_RANKS:
        return None
    return VALORANT_RANKS.index(base), int(level) if level.isdigit() else 0


def is_rank_promotion(old_tier: str | None, new_tier: str | None) -> bool:
    """Nur ein Aufstieg der Hauptstufe zählt, z. B. Gold 3 → Platinum 1, nicht Gold 2 → Gold 3."""
    old_value, new_value = rank_value(old_tier), rank_value(new_tier)
    return old_value is not None and new_value is not None and new_value[0] > old_value[0]


def rank_leaderboard(guild: discord.Guild) -> list[tuple[discord.Member, Profile, str]]:
    """Spieler mit verknüpfter Riot-ID, höchster Rang zuerst; Name wie bei /team."""
    player_role = find_role(guild, "Spieler")
    players = {member.id: member for member in player_role.members} if player_role else {}
    roster_names = get_roster_names(guild.id)
    entries = [
        (players[profile.user_id], profile, roster_names.get(profile.user_id, players[profile.user_id].display_name))
        for profile in linked_profiles(guild.id)
        if profile.user_id in players
    ]
    entries.sort(key=lambda entry: entry[2].casefold())
    entries.sort(key=lambda entry: rank_value(entry[1].rank_tier) or (-1, -1), reverse=True)
    return entries


async def congratulate_rank_up(member: discord.Member, old_tier: str, new_tier: str) -> None:
    if not setting("rank_congrats_enabled") or not member_has_role(member, "Spieler"):
        return
    channel = find_text_channel(member.guild, RANK_CONGRATS_CHANNEL)
    if channel is None:
        return
    try:
        await channel.send(
            f"🎉 Glückwunsch {member.mention}! Du bist von **{old_tier}** auf **{new_tier}** aufgestiegen.",
            allowed_mentions=discord.AllowedMentions(users=[member], roles=False, everyone=False, replied_user=False),
        )
    except discord.HTTPException:
        log.warning("Glückwunsch zum Rangaufstieg von %s konnte nicht gesendet werden", member.id)


async def sync_member_rank(member: discord.Member, profile: Profile) -> str:
    """Holt den aktuellen Rang über die Riot-ID und setzt die passende Rangrolle."""
    if RANK_CLIENT is None:
        raise RankLookupError("Die automatische Rangabfrage ist nicht eingerichtet (HENRIKDEV_API_KEY fehlt).")
    tier_name, region = await RANK_CLIENT.current_rank(profile.riot_name, profile.riot_tag, profile.region)
    synced_at = int(time.time())
    update_profile(member.guild.id, member.id, region=region, rank_tier=tier_name, rank_synced_at=synced_at)
    record_rank(member.guild.id, member.id, tier_name, synced_at)
    role_name = rank_role_name(tier_name)
    selected_role = find_role(member.guild, role_name) if role_name else None
    await set_exclusive_role(member, VALORANT_RANKS, selected_role, "Valorant-Rang automatisch über Riot-ID aktualisiert")
    # Beim ersten Abgleich nach dem Verknüpfen fehlt rank_synced_at; dann gibt es keinen Vergleich.
    if profile.rank_synced_at and is_rank_promotion(profile.rank_tier, tier_name):
        await congratulate_rank_up(member, profile.rank_tier, tier_name)
    return tier_name


RANK_SYNC_LOCK = asyncio.Lock()


async def sync_all_ranks(guild: discord.Guild) -> int:
    """Gleicht alle verknüpften Ränge nacheinander ab. Gibt die Zahl der erfolgreichen Abgleiche zurück."""
    async with RANK_SYNC_LOCK:
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
            await asyncio.sleep(setting("rank_sync_delay_seconds"))
        log.info("%s Ränge über Riot-ID aktualisiert", synced)
        return synced


async def sync_ranks_now(guild: discord.Guild, member: discord.Member | None = None) -> str:
    """Rang-Abgleich auf Befehl aus dem Panel: alle oder eine Person. Gibt eine Rückmeldung zurück."""
    if RANK_CLIENT is None:
        return "Die automatische Rangabfrage ist ausgeschaltet (HENRIKDEV_API_KEY fehlt)."
    if member is None:
        if RANK_SYNC_LOCK.locked():
            return "Ein Rang-Abgleich läuft bereits."
        return f"{await sync_all_ranks(guild)} Ränge aktualisiert."
    profile = get_profile(guild.id, member.id)
    if profile is None or profile.riot_id is None:
        return f"{member.display_name} hat keine Riot-ID verknüpft."
    try:
        tier_name = await sync_member_rank(member, profile)
    except (RankLookupError, discord.HTTPException) as error:
        return f"Der Rang von {member.display_name} konnte nicht abgeglichen werden: {error}"
    return f"Rang von {member.display_name}: {tier_name}"


def tracked(name: str):
    """Hält für einen tasks.loop fest, wann er zuletzt lief und woran er zuletzt gescheitert ist."""
    def decorator(function):
        status = LOOP_STATUS.setdefault(name, {"last_run": None, "last_error": None, "last_error_at": None})

        @functools.wraps(function)
        async def wrapper():
            status["last_run"] = int(time.time())
            try:
                await function()
            except Exception as error:
                status["last_error"] = f"{type(error).__name__}: {error}"
                status["last_error_at"] = int(time.time())
                raise
        return wrapper
    return decorator


def set_gateway_connected(connected: bool) -> None:
    if GATEWAY_STATE["connected"] != connected:
        GATEWAY_STATE.update(connected=connected, changed_at=int(time.time()))


def notify_listeners(topic: str) -> None:
    for listener in list(STATE_LISTENERS):
        try:
            listener(topic)
        except Exception:
            log.exception("Fehler beim Benachrichtigen über %s", topic)


def env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        value = None
    if value is None or not minimum <= value <= maximum:
        print(f"{name} muss eine ganze Zahl von {minimum} bis {maximum} sein; es gilt {default}.", flush=True)
        return default
    return value


ALERT_REPEAT_SECONDS = 30 * 60
ALERT_DISCONNECT_MINUTES = 5
ALERT_HENRIKDEV_FAILURES = 3
# Ein laufender Task gilt als hängend, wenn sein nächster Lauf so lange überfällig ist.
LOOP_STALL_SECONDS = 5 * 60
ALERT_HISTORY_SIZE = 20
WEBHOOK_PREFIXES = tuple(
    f"https://{host}/api/webhooks/" for host in ("discord.com", "discordapp.com", "canary.discord.com", "ptb.discord.com")
)


class Alerts:
    """Warnungen an den Staff, per Webhook (klappt auch ohne Gateway) oder in #audit-log.

    Jede Warnung hat einen Schlüssel. Gemeldet wird, wenn ein Problem auftritt, und
    entwarnt, wenn es verschwindet; derselbe Schlüssel höchstens alle 30 Minuten.
    """

    def __init__(self, webhook_url: str | None = None, repeat_seconds: float = ALERT_REPEAT_SECONDS):
        self.webhook_url = webhook_url
        self.repeat_seconds = repeat_seconds
        self.active: dict[str, dict] = {}
        self.last_alert_at: dict[str, float] = {}
        self.history: deque[dict] = deque(maxlen=ALERT_HISTORY_SIZE)
        self.pending: deque[dict] = deque(maxlen=ALERT_HISTORY_SIZE)
        self.session: aiohttp.ClientSession | None = None

    def evaluate(self, key: str, problem: str | None, now: float) -> dict | None:
        """Meldung, die jetzt raus muss, oder None. Ein Problem, das innerhalb der Sperrfrist
        wiederkommt, wird erst nach Ablauf der Frist gemeldet; eine Entwarnung gibt es nur
        für Warnungen, die auch verschickt wurden."""
        current = self.active.get(key)
        if problem:
            if current is None:
                current = self.active[key] = {"text": problem, "since": now, "notified": False}
            current["text"] = problem
            if current["notified"] or now - self.last_alert_at.get(key, -math.inf) < self.repeat_seconds:
                return None
            current.update(notified=True, alert_text=problem)
            self.last_alert_at[key] = now
            return {"kind": "alert", "key": key, "text": problem, "time": now}
        if current is None:
            return None
        del self.active[key]
        if not current["notified"]:
            return None
        return {"kind": "resolved", "key": key, "text": current["alert_text"], "time": now}

    async def check(self, problems: dict[str, str | None], guild: discord.Guild | None, connected: bool) -> None:
        # Liegengebliebene Warnungen zuerst, damit keine Entwarnung vor ihrer Warnung ankommt.
        while connected and guild and self.pending:
            message = self.pending.popleft()
            await self.post_audit(guild, message)
            message["delivered"] = "#audit-log (nachgereicht)"
        now = time.time()
        for key, problem in problems.items():
            message = self.evaluate(key, problem, now)
            if message is None:
                continue
            if message["kind"] == "alert":
                log.warning("Warnung: %s", message["text"])
            else:
                log.info("Wieder in Ordnung: %s", message["text"])
            self.history.append(message)
            await self.deliver(message, guild, connected)

    @staticmethod
    def render(message: dict) -> str:
        text = discord.utils.escape_markdown(message["text"])
        if message["kind"] == "alert":
            return f"🚨 **Duality-Bot – Warnung:** {text}"
        return f"✅ **Duality-Bot – wieder in Ordnung:** {text}"

    async def deliver(self, message: dict, guild: discord.Guild | None, connected: bool) -> None:
        if self.webhook_url and await self.post_webhook(self.render(message)):
            message["delivered"] = "Webhook"
        elif connected and guild:
            await self.post_audit(guild, message)
            message["delivered"] = "#audit-log"
        else:
            message["delivered"] = "ausstehend"
            self.pending.append(message)

    async def post_audit(self, guild: discord.Guild, message: dict) -> None:
        color = discord.Color.red() if message["kind"] == "alert" else discord.Color.green()
        await audit(guild, self.render(message), color)

    async def post_webhook(self, text: str) -> bool:
        if self.session is None or self.session.closed:
            self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        try:
            async with self.session.post(
                self.webhook_url,
                json={"content": text[:2000], "username": "Duality-Bot", "allowed_mentions": {"parse": []}},
            ) as response:
                if response.status < 300:
                    return True
                log.warning("Alarm-Webhook antwortet mit Status %s", response.status)
        except (aiohttp.ClientError, asyncio.TimeoutError) as error:
            # Nur der Fehlertyp: Die Meldung enthielte die Webhook-URL samt Token.
            log.warning("Alarm-Webhook nicht erreichbar (%s)", type(error).__name__)
        return False

    async def close(self) -> None:
        if self.session and not self.session.closed:
            await self.session.close()


class SettingDefinition(NamedTuple):
    key: str
    label: str
    description: str
    # "int", "bool" oder "hours" (1–4 Stundenangaben, absteigend sortiert)
    kind: str
    default: object
    minimum: int | None = None
    maximum: int | None = None


SETTING_DEFINITIONS = {definition.key: definition for definition in (
    SettingDefinition(
        "application_min_votes", "Stimmen für eine Bewerbungsentscheidung",
        "So viele Staff-Stimmen (👍/👎) braucht es, bevor angenommen oder abgelehnt werden kann. Gibt es weniger Staff, reicht die Staff-Zahl.",
        "int", APPLICATION_MIN_VOTES, 1, 10,
    ),
    SettingDefinition(
        "application_retention_days", "Löschfrist für Bewerbungen (Tage)",
        "Abgelehnte und geschlossene Bewerbungen werden nach so vielen Tagen samt Nachricht gelöscht.",
        "int", APPLICATION_RETENTION_DAYS, 1, 365,
    ),
    SettingDefinition(
        "event_retention_days", "Löschfrist für Termine (Tage)",
        "Vergangene Termine und Antworten werden nach so vielen Tagen gelöscht; so weit reichen Anwesenheit und /profil zurück.",
        "int", EVENT_RETENTION_DAYS, 7, 365,
    ),
    SettingDefinition(
        "event_reminder_hours", "Termin-Erinnerungen (Stunden vorher)",
        "Wann vor einem Termin in #spieltermine erinnert wird, z. B. 24 und 1. Höchstens 4 Zeitpunkte.",
        "hours", (24, 1), 1, 168,
    ),
    SettingDefinition(
        "rank_sync_delay_seconds", "Pause beim Rang-Abgleich (Sekunden)",
        "Abstand zwischen zwei Abfragen bei der HenrikDev-API, damit der Schlüssel nicht ins Rate-Limit läuft.",
        "int", RANK_SYNC_DELAY_SECONDS, 1, 60,
    ),
    SettingDefinition(
        "rank_congrats_enabled", "Glückwunsch bei Rangaufstieg",
        "Postet in #team-chat einen Glückwunsch, wenn ein Spieler eine Hauptstufe aufsteigt.",
        "bool", True,
    ),
    SettingDefinition(
        "attendance_threshold_percent", "Anwesenheit: Schwelle (%)",
        "Spieler unter dieser Zusagequote werden in der Anwesenheit markiert.",
        "int", ATTENDANCE_THRESHOLD_PERCENT, 0, 100,
    ),
    SettingDefinition(
        "attendance_missing_limit", "Anwesenheit: Termine ohne Reaktion",
        "Ab so vielen Terminen ohne Reaktion wird ein Spieler in der Anwesenheit markiert.",
        "int", ATTENDANCE_MISSING_LIMIT, 1, 20,
    ),
    SettingDefinition(
        "alert_disconnect_minutes", "Warnung: Minuten ohne Verbindung",
        "Ab so vielen Minuten ohne Verbindung zu Discord gibt es eine Warnung.",
        "int", ALERT_DISCONNECT_MINUTES, 1, 24 * 60,
    ),
    SettingDefinition(
        "alert_henrikdev_failures", "Warnung: HenrikDev-Fehler in Folge",
        "Ab so vielen fehlgeschlagenen Abfragen in Folge gibt es eine Warnung.",
        "int", ALERT_HENRIKDEV_FAILURES, 1, 20,
    ),
)}
# Gelesene Einstellungen; None heißt „neu aus der Datenbank laden“.
SETTINGS_CACHE: dict[str, object] | None = None


def validate_setting(definition: SettingDefinition, value):
    """Prüft einen Wert und gibt ihn in der Form zurück, mit der der Code arbeitet."""
    if definition.kind == "bool":
        if not isinstance(value, bool):
            raise ValueError("Bitte an oder aus wählen.")
        return value
    if definition.kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("Bitte eine ganze Zahl eingeben.")
        if not definition.minimum <= value <= definition.maximum:
            raise ValueError(f"Erlaubt sind {definition.minimum} bis {definition.maximum}.")
        return value
    if (
        not isinstance(value, (list, tuple)) or not 1 <= len(value) <= 4
        or any(isinstance(hour, bool) or not isinstance(hour, int) for hour in value)
    ):
        raise ValueError("Bitte 1 bis 4 ganze Zahlen angeben, z. B. 24, 1.")
    if any(not definition.minimum <= hour <= definition.maximum for hour in value):
        raise ValueError(f"Jeder Zeitpunkt muss zwischen {definition.minimum} und {definition.maximum} Stunden liegen.")
    if len(set(value)) != len(value):
        raise ValueError("Jeder Zeitpunkt darf nur einmal vorkommen.")
    return tuple(sorted(value, reverse=True))


def load_settings() -> dict[str, object]:
    try:
        with open_db() as connection:
            rows = connection.execute("SELECT key, value FROM bot_settings").fetchall()
    except sqlite3.Error:
        return {}
    values = {}
    for key, raw in rows:
        definition = SETTING_DEFINITIONS.get(key)
        if definition is None:
            continue
        try:
            values[key] = validate_setting(definition, json.loads(raw))
        except (ValueError, TypeError):
            log.warning("Einstellung %s in der Datenbank ist ungültig; es gilt der Standard", key)
    return values


def setting(key: str):
    global SETTINGS_CACHE
    if SETTINGS_CACHE is None:
        SETTINGS_CACHE = load_settings()
    return SETTINGS_CACHE.get(key, SETTING_DEFINITIONS[key].default)


def save_setting(key: str, value, updated_by: int | None) -> tuple[object, object]:
    """Speichert einen geprüften Wert und gibt (alt, neu) zurück; wirkt sofort."""
    global SETTINGS_CACHE
    definition = SETTING_DEFINITIONS.get(key)
    if definition is None:
        raise ValueError("Diese Einstellung gibt es nicht.")
    new_value = validate_setting(definition, value)
    old_value = setting(key)
    stored = list(new_value) if isinstance(new_value, tuple) else new_value
    with open_db() as connection:
        connection.execute(
            "INSERT INTO bot_settings (key, value, updated_by, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_by = excluded.updated_by, "
            "updated_at = excluded.updated_at",
            (key, json.dumps(stored), updated_by, int(time.time())),
        )
    SETTINGS_CACHE = None
    return old_value, new_value


def reset_setting(key: str) -> tuple[object, object]:
    global SETTINGS_CACHE
    definition = SETTING_DEFINITIONS.get(key)
    if definition is None:
        raise ValueError("Diese Einstellung gibt es nicht.")
    old_value = setting(key)
    with open_db() as connection:
        connection.execute("DELETE FROM bot_settings WHERE key = ?", (key,))
    SETTINGS_CACHE = None
    return old_value, definition.default


def settings_overview() -> list[dict]:
    with open_db() as connection:
        stored = {
            key: (updated_by, updated_at)
            for key, updated_by, updated_at in connection.execute("SELECT key, updated_by, updated_at FROM bot_settings")
        }
    overview = []
    for key, definition in SETTING_DEFINITIONS.items():
        updated_by, updated_at = stored.get(key, (None, None))
        value = setting(key)
        overview.append({
            "key": key,
            "label": definition.label,
            "description": definition.description,
            "kind": definition.kind,
            "minimum": definition.minimum,
            "maximum": definition.maximum,
            "default": list(definition.default) if isinstance(definition.default, tuple) else definition.default,
            "value": list(value) if isinstance(value, tuple) else value,
            "is_default": key not in stored,
            "updated_by": updated_by,
            "updated_at": updated_at,
        })
    return overview


class TextDefinition(NamedTuple):
    key: str
    label: str
    # Discord-Grenze: Embed-Beschreibung 4096, Feldwert 1024, Nachricht 2000
    limit: int
    default: str
    placeholders: tuple[str, ...] = ()


# Die bisherigen Texte aus dem Code sind die Standardwerte; im Panel geänderte gehen vor.
TEXT_DEFINITIONS = {definition.key: definition for definition in (
    TextDefinition("welcome", "Willkommen (#willkommen)", 4096, (
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
    )),
    TextDefinition("rules", "Regeln (#regeln)", 4096, (
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
    )),
    TextDefinition("application_info", "Bewerbungs-Infotext (#bewerbung)", 4096, (
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
        "Abgelehnte Bewerbungseinträge werden nach {loeschfrist} Tagen gelöscht."
    ), ("loeschfrist",)),
    TextDefinition("dm_accepted", "DM bei Annahme", 2000, "Deine Bewerbung bei Duality Mannheim wurde angenommen. Du hast jetzt die Rolle `Spieler` und Zugang zum Teambereich. Willkommen im Team!"),
    TextDefinition("dm_declined", "DM bei Ablehnung", 2000, "Danke für deine Bewerbung bei Duality Mannheim. Der Staff kann dir aktuell keinen Teamplatz anbieten. Deine Rolle `Bewerber` bleibt bis zur nächsten Saison bestehen. Vor jeder neuen Saison öffnen wir die Bewerbungen wieder, dann kannst du dich erneut bewerben. Wenn du Rückfragen hast, wende dich bitte direkt an den Staff."),
    TextDefinition("help_community", "/help – Community", 1024, (
        "Lies `#regeln`, stell dich in `#allgemein` vor und nutze die Buttons in `#willkommen`, um Benachrichtigungen auszuwählen. "
                    "Über `➕ Neuer Voice` bekommst du einen eigenen Voice-Channel, der verschwindet, sobald er leer ist."
    )),
    TextDefinition("help_team", "/help – Valorant-Team", 1024, (
        "`#bewerbung` ist für alle sichtbar. Starte dort über den Button das Teamformular. Nach dem Absenden erhältst du `Bewerber` und kannst den Bewerbungskanal bis zur nächsten Saison nicht erneut nutzen. Deinen Rang holt `/riot-id Name#Tag` automatisch, deine Hochschule änderst du mit `/hochschule`."
    )),
    TextDefinition("help_commands", "/help – Bot-Befehle", 1024, (
        "`/report` öffnet ein privates Ticket mit dem Staff (oder der Button in `#tickets`) · `/feedback` sendet eine Idee · "
                    "`/profil` zeigt Rang, Rollen und Hochschule · `/riot-id` verknüpft deine Riot-ID für den automatischen Rang · "
                    "`/hochschule` ändert deine Hochschule · "
                    "`/team` zeigt den aktuellen Kader · `/rangliste` sortiert ihn nach Valorant-Rang · "
                    "`/teamname` setzt optional deinen Namen für den Kader · "
                    "`/meine-daten` zeigt, exportiert oder löscht deine gespeicherten Daten · "
                    "`/termin` erstellt einen Teamtermin (Staff) · `/neue-saison` öffnet die Bewerbungen wieder (Staff)."
    )),
)}
TEXT_PLACEHOLDERS = {
    "loeschfrist": lambda: str(setting("application_retention_days")),
}
TEXT_HISTORY_SIZE = 20
# Aktuelle Rohtexte; None heißt „neu aus der Datenbank laden“, ein fehlender Schlüssel „Standard“.
TEXT_CACHE: dict[str, str] | None = None


def render_text(definition: TextDefinition, raw: str) -> str:
    for name in definition.placeholders:
        raw = raw.replace("{" + name + "}", TEXT_PLACEHOLDERS[name]())
    return raw


def load_texts() -> dict[str, str]:
    try:
        with open_db() as connection:
            rows = connection.execute(
                "SELECT key, value FROM bot_texts WHERE id IN (SELECT MAX(id) FROM bot_texts GROUP BY key)"
            ).fetchall()
    except sqlite3.Error:
        return {}
    return {key: value for key, value in rows if key in TEXT_DEFINITIONS and value is not None}


def raw_text(key: str) -> str:
    global TEXT_CACHE
    if TEXT_CACHE is None:
        TEXT_CACHE = load_texts()
    return TEXT_CACHE.get(key, TEXT_DEFINITIONS[key].default)


def bot_text(key: str) -> str:
    """Text zum Anzeigen: gespeicherte Fassung oder Standard, Platzhalter ausgefüllt."""
    return render_text(TEXT_DEFINITIONS[key], raw_text(key))


def validate_text(definition: TextDefinition, value) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Der Text darf nicht leer sein.")
    value = value.strip()
    length = max(len(value), len(render_text(definition, value)))
    if length > definition.limit:
        raise ValueError(f"Der Text ist {length} Zeichen lang; Discord erlaubt hier höchstens {definition.limit}.")
    return value


def store_text(key: str, value: str | None, updated_by: int | None) -> None:
    global TEXT_CACHE
    with open_db() as connection:
        connection.execute(
            "INSERT INTO bot_texts (key, value, updated_by, updated_at) VALUES (?, ?, ?, ?)",
            (key, value, updated_by, int(time.time())),
        )
        # Nur die letzten Fassungen je Text bleiben als Verlauf erhalten.
        connection.execute(
            "DELETE FROM bot_texts WHERE key = ? AND id NOT IN "
            "(SELECT id FROM bot_texts WHERE key = ? ORDER BY id DESC LIMIT ?)",
            (key, key, TEXT_HISTORY_SIZE),
        )
    TEXT_CACHE = None


def save_text(key: str, value, updated_by: int | None) -> str:
    definition = TEXT_DEFINITIONS.get(key)
    if definition is None:
        raise ValueError("Diesen Text gibt es nicht.")
    clean = validate_text(definition, value)
    store_text(key, clean, updated_by)
    return clean


def reset_text(key: str, updated_by: int | None) -> None:
    if key not in TEXT_DEFINITIONS:
        raise ValueError("Diesen Text gibt es nicht.")
    # Ein leerer Eintrag heißt „Standard“, so bleibt der Wechsel im Verlauf sichtbar.
    store_text(key, None, updated_by)


def text_history(key: str) -> list[dict]:
    with open_db() as connection:
        rows = connection.execute(
            "SELECT id, value, updated_by, updated_at FROM bot_texts WHERE key = ? ORDER BY id DESC LIMIT ?",
            (key, TEXT_HISTORY_SIZE),
        ).fetchall()
    return [
        {"id": row_id, "value": value, "is_default": value is None, "updated_by": updated_by, "updated_at": updated_at}
        for row_id, value, updated_by, updated_at in rows
    ]


def texts_overview() -> list[dict]:
    with open_db() as connection:
        latest = {
            key: (value, updated_by, updated_at)
            for key, value, updated_by, updated_at in connection.execute(
                "SELECT key, value, updated_by, updated_at FROM bot_texts "
                "WHERE id IN (SELECT MAX(id) FROM bot_texts GROUP BY key)"
            )
        }
    overview = []
    for key, definition in TEXT_DEFINITIONS.items():
        value, updated_by, updated_at = latest.get(key, (None, None, None))
        overview.append({
            "key": key,
            "label": definition.label,
            "limit": definition.limit,
            "value": raw_text(key),
            "default": definition.default,
            "is_default": value is None,
            "placeholders": {name: TEXT_PLACEHOLDERS[name]() for name in definition.placeholders},
            "posted_message": key in TEXT_MESSAGES,
            "updated_by": updated_by,
            "updated_at": updated_at,
        })
    return overview


def welcome_message_embed() -> discord.Embed:
    embed = discord.Embed(title="Willkommen bei Duality Mannheim! 👋", description=bot_text("welcome"), color=discord.Color.from_rgb(110, 70, 255))
    embed.set_footer(text=WELCOME_MARKER)
    return embed


def rules_message_embed() -> discord.Embed:
    embed = discord.Embed(title="Regeln von Duality Mannheim", description=bot_text("rules"), color=discord.Color.from_rgb(110, 70, 255))
    embed.set_footer(text=RULES_MARKER)
    return embed


def application_message_embed() -> discord.Embed:
    embed = discord.Embed(title="Bewerbung für Duality Mannheim", description=bot_text("application_info"), color=discord.Color.from_rgb(110, 70, 255))
    embed.set_footer(text=APPLICATION_MARKER)
    return embed


# Texte, die als feste Nachricht im Server stehen: Kanal, erkannte Marker, Embed-Bauer.
TEXT_MESSAGES = {
    "welcome": ("willkommen", {WELCOME_MARKER}, welcome_message_embed),
    "rules": ("regeln", {RULES_MARKER}, rules_message_embed),
    "application_info": ("bewerbung", {"DUALITY_ONBOARDING_V1", "DUALITY_ONBOARDING_V2", APPLICATION_MARKER}, application_message_embed),
}


async def refresh_text_message(guild: discord.Guild, key: str) -> bool:
    """Feste Nachricht nach einer Textänderung bearbeiten statt neu posten.
    Nur das Embed wird ersetzt, Buttons und ihre custom_ids bleiben."""
    if key not in TEXT_MESSAGES:
        return False
    channel_name, markers, build = TEXT_MESSAGES[key]
    channel = find_text_channel(guild, channel_name)
    if channel is None:
        return False
    async for message in channel.history(limit=50):
        if message.author == guild.me and message.embeds and message.embeds[0].footer.text in markers:
            await message.edit(embed=build())
            return True
    return False


# Rollen, die Mitglieder über die Buttons in #willkommen selbst abonnieren; nur diese dürfen gepingt werden.
ANNOUNCEMENT_PING_ROLES = ("Ping Valorant", "Ping Events")
ANNOUNCEMENT_MAX_FIELDS = 10
ANNOUNCEMENT_COLOR = 0x6E46FF


class Announcement(NamedTuple):
    id: int
    guild_id: int
    channel_id: int
    payload: dict
    send_at: int | None
    created_by: int | None
    created_at: int
    updated_by: int | None
    updated_at: int | None
    sent_at: int | None
    message_id: int | None
    error: str | None


ANNOUNCEMENT_COLUMNS = (
    "id, guild_id, channel_id, payload, send_at, created_by, created_at, updated_by, updated_at, sent_at, message_id, error"
)


def https_url(value, label: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError(f"{label}: Bitte eine gültige Adresse angeben.")
    parsed = urlparse(value.strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"{label}: Nur https-Adressen sind erlaubt.")
    return value.strip()


def validate_announcement(data) -> dict:
    """Prüft die Angaben aus dem Editor gegen die Grenzen von Discord und gibt sie bereinigt zurück."""
    if not isinstance(data, dict):
        raise ValueError("Ungültige Ankündigung.")

    def clean(name: str, limit: int, label: str) -> str:
        value = data.get(name) or ""
        if not isinstance(value, str):
            raise ValueError(f"{label}: Ungültiger Wert.")
        value = value.strip()
        if len(value) > limit:
            raise ValueError(f"{label} darf höchstens {limit} Zeichen haben (jetzt {len(value)}).")
        return value

    title = clean("title", 256, "Der Titel")
    description = clean("description", 4096, "Der Text")
    footer = clean("footer", 2048, "Die Fußzeile")
    if not title and not description:
        raise ValueError("Bitte mindestens einen Titel oder Text eingeben.")
    color = data.get("color") or f"#{ANNOUNCEMENT_COLOR:06x}"
    if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise ValueError("Die Farbe muss im Format #RRGGBB angegeben werden.")
    fields = data.get("fields") or []
    if not isinstance(fields, list) or len(fields) > ANNOUNCEMENT_MAX_FIELDS:
        raise ValueError(f"Höchstens {ANNOUNCEMENT_MAX_FIELDS} Felder sind möglich.")
    clean_fields = []
    for number, field in enumerate(fields, 1):
        if not isinstance(field, dict):
            raise ValueError(f"Feld {number} ist ungültig.")
        name = field.get("name") if isinstance(field.get("name"), str) else ""
        value = field.get("value") if isinstance(field.get("value"), str) else ""
        name, value = name.strip(), value.strip()
        if not name or not value:
            raise ValueError(f"Feld {number}: Name und Inhalt dürfen nicht leer sein.")
        if len(name) > 256 or len(value) > 1024:
            raise ValueError(f"Feld {number}: Name höchstens 256, Inhalt höchstens 1024 Zeichen.")
        clean_fields.append({"name": name, "value": value, "inline": field.get("inline") is True})
    total = len(title) + len(description) + len(footer) + sum(len(field["name"]) + len(field["value"]) for field in clean_fields)
    if total > 6000:
        raise ValueError(f"Die ganze Ankündigung darf höchstens 6000 Zeichen haben (jetzt {total}).")
    ping_role = data.get("ping_role") or None
    if ping_role is not None and ping_role not in ANNOUNCEMENT_PING_ROLES:
        raise ValueError("Diese Rolle darf nicht erwähnt werden.")
    return {
        "title": title,
        "description": description,
        "color": color.lower(),
        "image_url": https_url(data.get("image_url"), "Bild"),
        "thumbnail_url": https_url(data.get("thumbnail_url"), "Vorschaubild"),
        "footer": footer,
        "fields": clean_fields,
        "ping_role": ping_role,
    }


def announcement_embed(payload: dict) -> discord.Embed:
    embed = discord.Embed(
        title=payload["title"] or None,
        description=payload["description"] or None,
        color=int(payload["color"][1:], 16),
    )
    for field in payload["fields"]:
        embed.add_field(name=field["name"], value=field["value"], inline=field["inline"])
    if payload["image_url"]:
        embed.set_image(url=payload["image_url"])
    if payload["thumbnail_url"]:
        embed.set_thumbnail(url=payload["thumbnail_url"])
    if payload["footer"]:
        embed.set_footer(text=payload["footer"])
    return embed


def announcement_channels(guild: discord.Guild) -> list[discord.TextChannel]:
    """Textkanäle, in denen der Bot schreiben und Embeds senden darf."""
    return [
        channel for channel in guild.text_channels
        if (permissions := channel.permissions_for(guild.me)).view_channel and permissions.send_messages and permissions.embed_links
    ]


async def send_announcement(guild: discord.Guild, channel_id: int, payload: dict) -> discord.Message:
    channel = next((channel for channel in announcement_channels(guild) if channel.id == channel_id), None)
    if channel is None:
        raise ValueError("In diesem Kanal darf der Bot nicht schreiben.")
    role = find_role(guild, payload["ping_role"]) if payload["ping_role"] else None
    if payload["ping_role"] and role is None:
        raise ValueError(f"Die Rolle „{payload['ping_role']}“ gibt es nicht mehr.")
    # Erwähnt wird höchstens die gewählte Ping-Rolle, nie @everyone oder @here.
    return await channel.send(
        content=role.mention if role else None,
        embed=announcement_embed(payload),
        allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=[role] if role else False),
    )


async def edit_sent_announcement(guild: discord.Guild, announcement: Announcement, payload: dict) -> None:
    """Gesendete Ankündigung in Discord bearbeiten; der Ping wird dabei nicht wiederholt."""
    channel = guild.get_channel(announcement.channel_id)
    if not isinstance(channel, discord.TextChannel):
        raise ValueError("Der Kanal der Ankündigung existiert nicht mehr.")
    try:
        message = await channel.fetch_message(announcement.message_id)
    except discord.NotFound:
        raise ValueError("Die Ankündigung wurde in Discord gelöscht.") from None
    await message.edit(embed=announcement_embed(payload), allowed_mentions=discord.AllowedMentions.none())


def create_announcement(guild_id: int, channel_id: int, payload: dict, send_at: int | None, created_by: int) -> int:
    with open_db() as connection:
        cursor = connection.execute(
            "INSERT INTO scheduled_announcements (guild_id, channel_id, payload, send_at, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (guild_id, channel_id, json.dumps(payload, ensure_ascii=False), send_at, created_by, int(time.time())),
        )
    return cursor.lastrowid


def get_announcement(announcement_id: int) -> Announcement | None:
    with open_db() as connection:
        row = connection.execute(
            f"SELECT {ANNOUNCEMENT_COLUMNS} FROM scheduled_announcements WHERE id = ?", (announcement_id,)
        ).fetchone()
    return Announcement(*row[:3], json.loads(row[3]), *row[4:]) if row else None


def list_announcements(guild_id: int) -> list[Announcement]:
    with open_db() as connection:
        rows = connection.execute(
            f"SELECT {ANNOUNCEMENT_COLUMNS} FROM scheduled_announcements WHERE guild_id = ? "
            "ORDER BY sent_at IS NOT NULL, COALESCE(sent_at, send_at) DESC LIMIT 100",
            (guild_id,),
        ).fetchall()
    return [Announcement(*row[:3], json.loads(row[3]), *row[4:]) for row in rows]


def update_announcement(announcement_id: int, channel_id: int, payload: dict, send_at: int | None, updated_by: int) -> None:
    # Bearbeiten löscht einen alten Fehler, damit der nächste Versuch wieder läuft.
    with open_db() as connection:
        connection.execute(
            "UPDATE scheduled_announcements SET channel_id = ?, payload = ?, send_at = ?, updated_by = ?, updated_at = ?, error = NULL "
            "WHERE id = ?",
            (channel_id, json.dumps(payload, ensure_ascii=False), send_at, updated_by, int(time.time()), announcement_id),
        )


def delete_announcement(announcement_id: int) -> bool:
    with open_db() as connection:
        cursor = connection.execute(
            "DELETE FROM scheduled_announcements WHERE id = ? AND sent_at IS NULL", (announcement_id,)
        )
    return cursor.rowcount == 1


def mark_announcement_sent(announcement_id: int, message_id: int) -> None:
    with open_db() as connection:
        connection.execute(
            "UPDATE scheduled_announcements SET sent_at = ?, message_id = ?, error = NULL WHERE id = ?",
            (int(time.time()), message_id, announcement_id),
        )


def mark_announcement_failed(announcement_id: int, error: str) -> None:
    with open_db() as connection:
        connection.execute("UPDATE scheduled_announcements SET error = ? WHERE id = ?", (error[:500], announcement_id))


def due_announcements(guild_id: int, now: int) -> list[Announcement]:
    with open_db() as connection:
        rows = connection.execute(
            f"SELECT {ANNOUNCEMENT_COLUMNS} FROM scheduled_announcements WHERE guild_id = ? AND sent_at IS NULL "
            "AND error IS NULL AND send_at <= ? ORDER BY send_at",
            (guild_id, now),
        ).fetchall()
    return [Announcement(*row[:3], json.loads(row[3]), *row[4:]) for row in rows]


ANNOUNCEMENT_LOCK = asyncio.Lock()


async def send_due_announcements(guild: discord.Guild | None) -> int:
    """Geplante Ankündigungen senden. Scheitert eine, bleibt sie mit Fehler stehen statt es jede Minute neu zu versuchen."""
    if guild is None:
        return 0
    sent = 0
    async with ANNOUNCEMENT_LOCK:
        for announcement in due_announcements(guild.id, int(time.time())):
            try:
                message = await send_announcement(guild, announcement.channel_id, announcement.payload)
            except (ValueError, discord.HTTPException) as error:
                mark_announcement_failed(announcement.id, str(error))
                log.warning("Geplante Ankündigung %s konnte nicht gesendet werden: %s", announcement.id, error)
                continue
            mark_announcement_sent(announcement.id, message.id)
            sent += 1
            await audit(guild, f"📣 Geplante Ankündigung in <#{announcement.channel_id}> gesendet", discord.Color.from_rgb(110, 70, 255))
    return sent


def collect_problems(loops: dict, now: float) -> dict[str, str | None]:
    """Aktuelle Probleme je Schlüssel; None heißt „in Ordnung“."""
    problems: dict[str, str | None] = {}
    for name, (label, loop) in loops.items():
        status = LOOP_STATUS.get(name, {})
        next_run = loop.next_iteration if loop.is_running() else None
        problem = None
        if status.get("last_run") and not loop.is_running():
            problem = f"Hintergrund-Task „{label}“ ist gestoppt"
            if status.get("last_error"):
                problem += f" (letzter Fehler: {status['last_error']})"
        elif next_run and now - next_run.timestamp() > LOOP_STALL_SECONDS:
            problem = f"Hintergrund-Task „{label}“ hängt, der geplante Lauf ist überfällig"
        problems[f"task:{name}"] = problem
    offline_seconds = now - GATEWAY_STATE["changed_at"]
    problems["gateway"] = None
    if not GATEWAY_STATE["connected"] and offline_seconds >= setting("alert_disconnect_minutes") * 60:
        problems["gateway"] = f"Der Bot ist seit {int(offline_seconds // 60)} Minuten nicht mit Discord verbunden"
    problems["backup"] = f"Das Backup ist fehlgeschlagen: {BACKUP_STATE['last_error']}" if BACKUP_STATE["last_error"] else None
    problems["henrikdev"] = None
    if RANK_CLIENT and RANK_CLIENT.failures_in_row >= setting("alert_henrikdev_failures"):
        problems["henrikdev"] = (
            f"Die HenrikDev-API ist {RANK_CLIENT.failures_in_row}× in Folge fehlgeschlagen "
            f"(zuletzt: {RANK_CLIENT.last_error})"
        )
    return problems


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
                # Neue Riot-ID: alter Rang zählt nicht, sonst gäbe es beim ersten Abgleich einen falschen Glückwunsch.
                **({"riot_name": riot_id[0], "riot_tag": riot_id[1], "region": None, "rank_tier": None, "rank_synced_at": None} if riot_id else {}),
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
    return max(1, min(setting("application_min_votes"), len(staff_ids)))


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
            f"nach {setting('application_retention_days')} Tagen automatisch gelöscht.",
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
        dm_text = bot_text("dm_accepted")
    else:
        dm_text = bot_text("dm_declined")

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
        response += f". Der Eintrag wird nach {setting('application_retention_days')} Tagen automatisch gelöscht"
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


async def clean_up_temp_voice(guild: discord.Guild) -> int:
    # Nach einem Neustart sind leere Kanäle übrig, deren Verlassen der Bot nicht mitbekommen hat.
    removed = 0
    for channel_id in temp_voice_ids(guild.id):
        channel = guild.get_channel(channel_id)
        if channel is None:
            delete_temp_voice(channel_id)
        elif not channel.members:
            try:
                await channel.delete(reason="Temporärer Voice-Channel ist leer")
                delete_temp_voice(channel_id)
                removed += 1
            except discord.HTTPException:
                log.warning("Leerer Voice-Channel %s konnte nicht gelöscht werden", channel_id)
    return removed


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


async def purge_expired_data(guild: discord.Guild | None) -> tuple[int, int]:
    """Wendet die Löschfristen an. Gibt (gelöschte Termine, gelöschte Bewerbungen) zurück."""
    now = int(time.time())
    removed_events = delete_past_events(now - setting("event_retention_days") * 24 * 60 * 60)
    if removed_events:
        log.info("%s vergangene Termine aus der Datenbank entfernt", removed_events)

    staff_channel = find_text_channel(guild, "bewerbungen") if guild else None
    if staff_channel is None:
        return removed_events, 0
    removed_applications = 0
    retention_days = setting("application_retention_days")
    cutoff = now - retention_days * 24 * 60 * 60
    for application in expired_applications(guild.id, cutoff):
        try:
            message = await staff_channel.fetch_message(application.message_id)
            await message.delete()
        except discord.NotFound:
            pass
        delete_application(application.message_id)
        removed_applications += 1
        log.info("Bewerbung nach %s Tagen gelöscht", retention_days)
    return removed_events, removed_applications


BACKUP_PREFIX = "duality-backup-"
BACKUP_PATTERN = re.compile(r"duality-backup-(\d{4}-\d{2}-\d{2})_(\d{6})\.sqlite3")
BACKUP_RETENTION_DAYS = 14
BACKUP_HOUR = 4
BACKUP_STATE = {"last_success": None, "last_error": None, "last_error_at": None}
BACKUP_LOCK = asyncio.Lock()


def create_backup(directory: Path, moment: datetime) -> Path:
    """Konsistente Kopie über die SQLite-Backup-API, nur für den Bot-Nutzer lesbar."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = directory / f"{BACKUP_PREFIX}{moment:%Y-%m-%d_%H%M%S}.sqlite3"
    # Erst fertig schreiben, dann umbenennen: Ein halbes Backup trägt nie den endgültigen Namen.
    partial = target.with_name(target.name + ".part")
    os.close(os.open(partial, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600))
    try:
        with closing(sqlite3.connect(DATA_PATH)) as source, closing(sqlite3.connect(partial)) as destination:
            source.backup(destination)
        os.replace(partial, target)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    return target


def list_backups(directory: Path) -> list[dict]:
    """Vorhandene Backups, neueste zuerst. Andere Dateien im Ordner werden ignoriert."""
    if not directory.is_dir():
        return []
    backups = []
    for path in directory.iterdir():
        match = BACKUP_PATTERN.fullmatch(path.name)
        if not match or not path.is_file():
            continue
        created = datetime.strptime(f"{match[1]} {match[2]}", "%Y-%m-%d %H%M%S").replace(tzinfo=BERLIN)
        backups.append({"name": path.name, "path": path, "size": path.stat().st_size, "created": int(created.timestamp())})
    return sorted(backups, key=lambda backup: backup["created"], reverse=True)


def prune_backups(directory: Path, retention_days: int, now: datetime) -> list[str]:
    cutoff = (now - timedelta(days=retention_days)).timestamp()
    removed = []
    for backup in list_backups(directory):
        if backup["created"] < cutoff:
            backup["path"].unlink()
            removed.append(backup["name"])
    return removed


async def run_backup(directory: Path, retention_days: int) -> dict:
    """Backup erstellen und alte löschen. Ein Fehler bleibt in BACKUP_STATE stehen, bis ein Backup klappt."""
    async with BACKUP_LOCK:
        now = datetime.now(BERLIN)
        try:
            path = await asyncio.to_thread(create_backup, directory, now)
            removed = await asyncio.to_thread(prune_backups, directory, retention_days, now)
        except (OSError, sqlite3.Error) as error:
            BACKUP_STATE.update(last_error=f"{type(error).__name__}: {error}", last_error_at=int(time.time()))
            log.error("Backup fehlgeschlagen: %s", BACKUP_STATE["last_error"])
            raise
        BACKUP_STATE.update(last_success=int(time.time()), last_error=None, last_error_at=None)
        log.info("Backup erstellt: %s, %s alte Backups gelöscht", path.name, len(removed))
        return {"name": path.name, "size": path.stat().st_size, "removed": len(removed)}


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
                content="Sicher? Gelöscht werden Profil (Riot-ID, Rollen, Rang-Sync), Rangverlauf, Teamname, Terminantworten und "
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


RSVP_LABELS = {
    "going": "✅ Dabei",
    "maybe": "🤔 Vielleicht",
    "declined": "❌ Kann nicht",
}
EVENT_COLOR = discord.Color.from_rgb(110, 70, 255)


class EventError(ValueError):
    """Ungültige Termin-Angaben oder -Änderung; die Meldung geht so an die Person, die sie auslöst."""


def event_description(starts_at: int, details: str) -> str:
    when = f"🕒 <t:{starts_at}:F> · <t:{starts_at}:R>"
    return f"{when}\n\n{details}" if details else when


def set_rsvp_fields(embed: discord.Embed, responses: dict[str, list[int]]) -> None:
    embed.clear_fields()
    for rsvp_choice, label in RSVP_LABELS.items():
        member_ids = responses.get(rsvp_choice, [])
        listed = [f"<@{member_id}>" for member_id in member_ids[:35]]
        if len(member_ids) > len(listed):
            listed.append(f"… und {len(member_ids) - len(listed)} weitere")
        embed.add_field(
            name=f"{label} ({len(member_ids)})",
            value="\n".join(listed) or "Noch niemand",
            inline=True,
        )


def event_embed(title: str, details: str, starts_at: int, responses: dict[str, list[int]] | None = None) -> discord.Embed:
    embed = discord.Embed(title=title, description=event_description(starts_at, details), color=EVENT_COLOR)
    set_rsvp_fields(embed, responses or {})
    embed.set_footer(text=f"{EVENT_MARKER}|{starts_at}")
    return embed


def validate_event(title: str, starts_at: datetime, details: str) -> tuple[str, str, int]:
    """Gleiche Regeln für /termin und das Panel. Gibt (Titel, Infos, Zeitstempel) zurück."""
    timestamp = int(starts_at.timestamp())
    if timestamp <= int(time.time()):
        raise EventError("Der Termin muss in der Zukunft liegen.")
    clean_title, clean_details = title.strip(), details.strip()
    if not clean_title or len(clean_title) > 100 or len(clean_details) > 1000:
        raise EventError("Der Titel darf 1–100 und die Zusatzinfo höchstens 1000 Zeichen haben.")
    return clean_title, clean_details, timestamp


async def publish_event(guild: discord.Guild, title: str, details: str, starts_at: int) -> discord.Message:
    """Termin mit Zu-/Absage-Buttons in #spieltermine veröffentlichen; Werte vorher mit validate_event prüfen."""
    event_channel = find_text_channel(guild, "spieltermine")
    if event_channel is None:
        raise EventError("`#spieltermine` wurde nicht gefunden.")
    event_message = await event_channel.send(
        embed=event_embed(title, details, starts_at),
        view=EventRSVPView(),
        allowed_mentions=discord.AllowedMentions.none(),
    )
    save_scheduled_event(event_message.id, guild.id, event_channel.id, starts_at, title, details)
    return event_message


def disabled_rsvp_view() -> discord.ui.View:
    view = EventRSVPView()
    for item in view.children:
        item.disabled = True
    return view


async def fetch_event(guild: discord.Guild, message_id: int) -> tuple[ScheduledEvent, discord.Message]:
    event = get_scheduled_event(message_id)
    if event is None or event.guild_id != guild.id:
        raise EventError("Diesen Termin gibt es nicht.")
    channel = guild.get_channel(event.channel_id)
    if not isinstance(channel, discord.TextChannel):
        raise EventError("Der Kanal dieses Termins existiert nicht mehr.")
    try:
        message = await channel.fetch_message(message_id)
    except discord.NotFound:
        raise EventError("Die Termin-Nachricht wurde in Discord gelöscht.") from None
    return event, message


def stored_event_text(event: ScheduledEvent, message: discord.Message) -> tuple[str, str]:
    """Titel und Infos aus der DB, bei älteren Terminen aus dem Embed."""
    embed = message.embeds[0] if message.embeds else None
    title = event.title or (embed.title if embed else None) or "Teamtermin"
    details = event.details if event.details is not None else ((embed.description or "").partition("\n\n")[2] if embed else "")
    return title, details


async def notify_going(channel: discord.TextChannel, message_id: int, text: str) -> int:
    """Kurze Nachricht an alle Zusagen; erwähnt werden nur diese Personen."""
    attendees = [member for user_id in get_rsvps(message_id).get("going", []) if (member := channel.guild.get_member(user_id))]
    attendees = attendees[:100]
    if not attendees:
        return 0
    await channel.send(
        f"{text}\n{' '.join(member.mention for member in attendees)}",
        allowed_mentions=discord.AllowedMentions(users=attendees, roles=False, everyone=False, replied_user=False),
    )
    return len(attendees)


async def edit_event(
    guild: discord.Guild, message_id: int, title: str, details: str, starts_at: int, notify: bool = False
) -> str:
    """Termin ändern oder verschieben (Werte vorher mit validate_event prüfen)."""
    event, message = await fetch_event(guild, message_id)
    if event.cancelled_at:
        raise EventError("Abgesagte Termine können nicht mehr geändert werden.")
    if event.starts_at <= time.time():
        raise EventError("Der Termin hat schon begonnen und kann nicht mehr geändert werden.")
    lock = RSVP_LOCKS.setdefault(message_id, asyncio.Lock())
    async with lock:
        # Erst Discord, dann die DB: Scheitert Discord, bleibt alles beim Alten.
        await message.edit(
            embed=event_embed(title, details, starts_at, get_rsvps(message_id)),
            view=EventRSVPView(),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        update_scheduled_event(message_id, title, details, starts_at)
    moved = starts_at != event.starts_at
    result = "Termin verschoben; Erinnerungen werden für die neue Zeit erneut gesendet." if moved else "Termin geändert."
    if notify and moved:
        notified = await notify_going(message.channel, message_id, f"📅 **{discord.utils.escape_markdown(title)}** wurde verschoben: jetzt <t:{starts_at}:F> (<t:{starts_at}:R>).")
        result += f" {notified} Zusagen wurden benachrichtigt."
    return result


async def cancel_event(guild: discord.Guild, message_id: int, notify: bool = False) -> str:
    event, message = await fetch_event(guild, message_id)
    if event.cancelled_at:
        raise EventError("Der Termin ist bereits abgesagt.")
    title, details = stored_event_text(event, message)
    lock = RSVP_LOCKS.setdefault(message_id, asyncio.Lock())
    async with lock:
        embed = event_embed(title, details, event.starts_at, get_rsvps(message_id))
        embed.title = f"❌ Abgesagt: {title}"[:256]
        embed.description = f"**Dieser Termin wurde abgesagt.**\n{embed.description}"
        embed.color = discord.Color.dark_grey()
        await message.edit(embed=embed, view=disabled_rsvp_view(), allowed_mentions=discord.AllowedMentions.none())
        cancel_scheduled_event(message_id)
    result = "Termin abgesagt; es gehen keine Erinnerungen mehr raus."
    if notify:
        notified = await notify_going(message.channel, message_id, f"❌ **{discord.utils.escape_markdown(title)}** am <t:{event.starts_at}:F> wurde abgesagt.")
        result += f" {notified} Zusagen wurden benachrichtigt."
    return result


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
    event = get_scheduled_event(interaction.message.id)
    if event and event.cancelled_at:
        await interaction.response.send_message("Dieser Termin wurde abgesagt.", ephemeral=True)
        return

    await interaction.response.defer()
    # Schreiben, Lesen und Bearbeiten nacheinander, damit kein gleichzeitiger
    # Klick das Embed mit einem älteren Stand überschreibt.
    lock = RSVP_LOCKS.setdefault(interaction.message.id, asyncio.Lock())
    async with lock:
        write_rsvp(interaction.message.id, interaction.user.id, choice)
        embed = discord.Embed.from_dict(interaction.message.embeds[0].to_dict())
        set_rsvp_fields(embed, get_rsvps(interaction.message.id))
        await interaction.edit_original_response(embed=embed, view=EventRSVPView())
    await interaction.followup.send(f"Deine Antwort wurde auf „{RSVP_LABELS[choice]}“ gesetzt.", ephemeral=True)


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


REMINDER_LOCK = asyncio.Lock()


def due_reminder(hours: Sequence[int], seconds_until_start: int) -> tuple[str, str] | None:
    """Fällige Erinnerung als (Schlüssel, Überschrift): Fenster von (h−1, h] Stunden vor Beginn.
    Die Schlüssel „24h“ und „1h“ entsprechen den bisher gespeicherten."""
    for hour in hours:
        if (hour - 1) * 3600 < seconds_until_start <= hour * 3600:
            if hour == 24:
                label = "Morgen"
            elif hour == 1:
                label = "Gleich geht's los"
            elif hour % 24 == 0:
                label = f"In {hour // 24} Tagen"
            else:
                label = f"In {hour} Stunden"
            return f"{hour}h", label
    return None


async def send_due_event_reminders(client: discord.Client) -> int:
    # Läuft minütlich und auf Befehl aus dem Panel; der Lock verhindert doppelte Erinnerungen.
    async with REMINDER_LOCK:
        return await _send_due_event_reminders_locked(client)


async def _send_due_event_reminders_locked(client: discord.Client) -> int:
    sent = 0
    now = int(time.time())
    for message_id, guild_id, channel_id, starts_at in get_upcoming_events(now):
        reminder = due_reminder(setting("event_reminder_hours"), starts_at - now)
        if reminder is None:
            continue
        reminder_key, reminder_label = reminder

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
        sent += 1
    return sent


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
            title = message.embeds[0].title
            details = (message.embeds[0].description or "").partition("\n\n")[2]
            if starts_at <= now:
                fill_event_text(message.id, title, details)
                continue

            save_scheduled_event(message.id, guild.id, event_channel.id, starts_at, title, details)
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
        embed.add_field(name="Community", value=bot_text("help_community"), inline=False)
        embed.add_field(name="Valorant-Team", value=bot_text("help_team"), inline=False)
        embed.add_field(name="Bot-Befehle", value=bot_text("help_commands"), inline=False)
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

    @tree.command(name="rangliste", description="Zeigt alle Spieler mit verknüpfter Riot-ID, sortiert nach Rang.")
    async def leaderboard_command(interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("Dieser Befehl funktioniert nur im Server.", ephemeral=True)
            return
        lines = []
        for position, (member, profile, name) in enumerate(rank_leaderboard(interaction.guild), 1):
            rank = profile.rank_tier or "noch kein Abgleich"
            synced = f" · <t:{profile.rank_synced_at}:d>" if profile.rank_synced_at else ""
            lines.append(f"{position}. **{discord.utils.escape_markdown(name)}** – {rank}{synced}")
        value = "\n".join(lines) if lines else "Noch kein Spieler hat seine Riot-ID mit `/riot-id` verknüpft."
        if len(value) > 3900:
            value = value[:3890] + "…"
        embed = discord.Embed(title="Duality Mannheim – Rangliste", description=value, color=discord.Color.from_rgb(110, 70, 255))
        embed.set_footer(text="Ränge werden täglich über die Riot-ID abgeglichen.")
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
        try:
            clean_title, clean_details, event_timestamp = validate_event(title, event_time, details)
        except EventError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        await publish_event(interaction.guild, clean_title, clean_details, event_timestamp)
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
                retention_days = setting("event_retention_days")
                since = max(now - retention_days * 24 * 60 * 60, int(target.joined_at.timestamp()) if target.joined_at else 0)
                going, total = attendance(interaction.guild.id, target.id, since, now)
                quote_text = f"{going} von {total} Terminen zugesagt ({round(100 * going / total)} %)" if total else "Noch keine vergangenen Termine"
                embed.add_field(name=f"Teilnahme (letzte {retention_days} Tage)", value=quote_text, inline=True)
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
        update_profile(
            interaction.guild.id, interaction.user.id,
            riot_name=parsed[0], riot_tag=parsed[1], region=None, rank_tier=None, rank_synced_at=None,
        )
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
        embed = application_message_embed()
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
        rules_embed = rules_message_embed()
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

        welcome_embed = welcome_message_embed()
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


async def main(apply: bool) -> int | None:
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
    webhook_url = os.getenv("ALERT_WEBHOOK_URL", "").strip()
    if webhook_url and not webhook_url.startswith(WEBHOOK_PREFIXES):
        print("ALERT_WEBHOOK_URL ist keine Discord-Webhook-Adresse und wird ignoriert.", flush=True)
        webhook_url = ""
    if apply:
        print(
            "Warnungen gehen an den Webhook aus ALERT_WEBHOOK_URL." if webhook_url
            else "ALERT_WEBHOOK_URL fehlt; Warnungen gehen nach #audit-log, sobald der Bot verbunden ist.",
            flush=True,
        )
    alerts = Alerts(webhook_url or None)
    # Die Env-Variable ist der Standard; im Panel gespeicherte Werte gehen vor.
    SETTING_DEFINITIONS["alert_disconnect_minutes"] = SETTING_DEFINITIONS["alert_disconnect_minutes"]._replace(
        default=env_int("ALERT_DISCONNECT_MINUTES", ALERT_DISCONNECT_MINUTES, 1, 24 * 60)
    )
    backup_dir = Path(os.getenv("BACKUP_DIR", "").strip() or DATA_PATH.with_name("backups"))
    backup_retention = env_int("BACKUP_RETENTION_DAYS", BACKUP_RETENTION_DAYS, 1, 3650)
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
    @tracked("sync_ranks")
    async def sync_ranks():
        guild = client.get_guild(target_id)
        if guild is None or RANK_CLIENT is None:
            return
        await sync_all_ranks(guild)

    @sync_ranks.error
    async def sync_ranks_error(error: Exception):
        log.error("Fehler beim täglichen Rang-Abgleich", exc_info=error)

    @tasks.loop(hours=24)
    @tracked("purge_declined_applications")
    async def purge_declined_applications():
        await purge_expired_data(client.get_guild(target_id))

    @purge_declined_applications.error
    async def purge_declined_applications_error(error: Exception):
        log.error("Fehler bei der Bewerbungs-Löschfrist", exc_info=error)

    @tasks.loop(minutes=1)
    @tracked("event_reminders")
    async def event_reminders():
        await send_due_event_reminders(client)

    @event_reminders.error
    async def event_reminders_error(error: Exception):
        log.error("Fehler bei den Termin-Erinnerungen", exc_info=error)

    @tasks.loop(time=day_time(hour=BACKUP_HOUR, tzinfo=BERLIN))
    @tracked("backups")
    async def daily_backup():
        try:
            await run_backup(backup_dir, backup_retention)
        except (OSError, sqlite3.Error):
            # Steht in BACKUP_STATE; die Warnung kommt über check_alerts. Der Task läuft morgen weiter.
            pass

    @tasks.loop(minutes=1)
    @tracked("announcements")
    async def scheduled_announcements():
        await send_due_announcements(client.get_guild(target_id))

    @scheduled_announcements.error
    async def scheduled_announcements_error(error: Exception):
        log.error("Fehler bei geplanten Ankündigungen", exc_info=error)

    @tasks.loop(minutes=1)
    @tracked("check_alerts")
    async def check_alerts():
        # Darf nie stehen bleiben, sonst meldet niemand mehr Probleme.
        try:
            await alerts.check(
                collect_problems(loops, time.time()),
                client.get_guild(target_id),
                GATEWAY_STATE["connected"] and client.is_ready(),
            )
        except Exception:
            log.exception("Fehler bei der Prüfung auf Warnungen")

    loops = {
        "sync_ranks": ("Rang-Abgleich", sync_ranks),
        "purge_declined_applications": ("Löschfristen", purge_declined_applications),
        "event_reminders": ("Termin-Erinnerungen", event_reminders),
        "backups": ("Backups", daily_backup),
        "announcements": ("Geplante Ankündigungen", scheduled_announcements),
        "check_alerts": ("Warnungen prüfen", check_alerts),
    }

    setup_complete = False
    commands_synced = False
    existing_events_registered = False

    @client.event
    async def on_connect():
        set_gateway_connected(True)
        print("Gateway-Verbindung hergestellt; Discord authentifiziert den Bot ...", flush=True)

    @client.event
    async def on_disconnect():
        set_gateway_connected(False)
        print("Gateway-Verbindung getrennt; discord.py versucht ggf. erneut zu verbinden.", flush=True)

    @client.event
    async def on_resumed():
        set_gateway_connected(True)
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
        if apply and member.guild.id == target_id:
            notify_listeners("voice")
            if before.channel != after.channel:
                await handle_temp_voice(member, before, after)

    @client.event
    async def on_ready():
        nonlocal setup_complete, commands_synced, existing_events_registered
        set_gateway_connected(True)
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
            if not daily_backup.is_running():
                daily_backup.start()
            if not scheduled_announcements.is_running():
                scheduled_announcements.start()
            print("Onboarding-Bot läuft. Mit Ctrl+C beenden.")
        else:
            await client.close()

    restart_requested = False

    async def request_restart() -> None:
        nonlocal restart_requested
        restart_requested = True
        log.warning("Neustart über das Web-Panel angefordert")
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
                    sync_ranks=sync_ranks_now,
                    send_reminders=lambda: send_due_event_reminders(client),
                    purge=purge_expired_data,
                    cleanup_voice=clean_up_temp_voice,
                    restart=request_restart,
                    started_at=STARTED_AT,
                    gateway=GATEWAY_STATE,
                    loops=loops,
                    loop_status=LOOP_STATUS,
                    rank_client=lambda: RANK_CLIENT,
                    find_role=find_role,
                    staff_members=staff_members,
                    alerts=alerts,
                    run_backup=lambda: run_backup(backup_dir, backup_retention),
                    list_backups=lambda: list_backups(backup_dir),
                    backup_state=BACKUP_STATE,
                    listeners=STATE_LISTENERS,
                    temp_voice_owners=temp_voice_owners,
                    find_text_channel=find_text_channel,
                    list_applications=list_applications,
                    application_vote_counts=application_vote_counts,
                    application_labels=APPLICATION_STATUS_LABELS,
                    reopen_applications=reopen_applications,
                    list_profiles=list_profiles,
                    list_tickets=list_tickets,
                    list_events=list_events,
                    event_rsvps=event_rsvp_overview,
                    event_reminders=event_reminder_overview,
                    validate_event=validate_event,
                    publish_event=publish_event,
                    edit_event=edit_event,
                    cancel_event=cancel_event,
                    attendance_events=attendance_events,
                    summarize_attendance=summarize_attendance,
                    rank_leaderboard=rank_leaderboard,
                    rank_histories=rank_histories,
                    rank_value=rank_value,
                    attendance_rules=lambda: {
                        "threshold": setting("attendance_threshold_percent"),
                        "missing_limit": setting("attendance_missing_limit"),
                        "retention_days": setting("event_retention_days"),
                    },
                    announcement_channels=announcement_channels,
                    announcement_ping_roles=ANNOUNCEMENT_PING_ROLES,
                    validate_announcement=validate_announcement,
                    send_announcement=send_announcement,
                    edit_sent_announcement=edit_sent_announcement,
                    create_announcement=create_announcement,
                    get_announcement=get_announcement,
                    list_announcements=list_announcements,
                    update_announcement=update_announcement,
                    delete_announcement=delete_announcement,
                    mark_announcement_sent=mark_announcement_sent,
                    texts_overview=texts_overview,
                    text_history=text_history,
                    save_text=save_text,
                    reset_text=reset_text,
                    refresh_text_message=refresh_text_message,
                    settings_overview=settings_overview,
                    save_setting=save_setting,
                    reset_setting=reset_setting,
                ))
            else:
                print("PANEL_ENABLED ist gesetzt, das Web-Panel läuft aber nur mit --apply.", flush=True)
        if apply:
            # Läuft schon vor dem Login, damit auch ein Bot ohne Verbindung gemeldet wird.
            check_alerts.start()
        async with client:
            print("Verbinde mit Discord ...", flush=True)
            await client.start(token)
    finally:
        check_alerts.cancel()
        await alerts.close()
        if panel_runner:
            await panel_runner.cleanup()
        if RANK_CLIENT:
            await RANK_CLIENT.close()
    if restart_requested:
        print("Bot beendet sich für den Neustart; systemd startet ihn wieder.", flush=True)
        return RESTART_EXIT_CODE
    return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Duality Mannheim Discord-Setup und Mitglieder-Onboarding")
    parser.add_argument("--apply", action="store_true", help="Setup anwenden und Onboarding-Bot online halten")
    parser.add_argument("--debug", action="store_true", help="Zusätzliche Gateway-Verbindungsdetails ausgeben")
    args = parser.parse_args()
    if args.debug:
        logging.getLogger("discord.gateway").setLevel(logging.DEBUG)
        print("Gateway-Diagnose ist aktiviert.", flush=True)
    try:
        sys.exit(asyncio.run(main(args.apply)))
    except KeyboardInterrupt:
        print("Bot beendet.")
