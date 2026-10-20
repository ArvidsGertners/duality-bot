"""Set up Duality Mannheim's Discord and run its member onboarding bot.

Preview: python duality_discord_setup.py
Apply and keep the bot online: python duality_discord_setup.py --apply

The bot token can come from an environment variable or a hidden terminal
prompt. The guild ID defaults to Duality Mannheim. Never put a token in this
file. --apply creates missing items and updates managed permissions.
"""

import argparse
import asyncio
import sqlite3
import getpass
import logging
import os
import time
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from urllib.parse import urlparse

import discord
from discord import app_commands
from discord.ext import tasks


logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("discord").setLevel(logging.INFO)


VALORANT_RANKS = [
    "Iron", "Bronze", "Silver", "Gold", "Platinum", "Diamond",
    "Ascendant", "Immortal", "Radiant",
]
VALORANT_ROLES = ("Duelist", "Initiator", "Controller", "Sentinel")
ROLE_NAMES = [
    "Owner", "Admin", "Coach", "Spieler", "Bewerber",
    "Ping Valorant", "Ping Events", *VALORANT_RANKS,
]
STAFF_ROLES = ("Owner", "Admin", "Coach")
TEAM_ROLES = (*STAFF_ROLES, "Spieler")
APPLICATION_RETENTION_DAYS = 30
APPLICATION_PENDING_PREFIX = "DUALITY_APPLICATION_PENDING|"
APPLICATION_DECISION_PREFIX = "DUALITY_APPLICATION_"
EVENT_MARKER = "DUALITY_EVENT_V1"
DATA_PATH = Path(__file__).with_name("duality_bot_data.sqlite3")
APPLICATION_LOCKS: dict[int, asyncio.Lock] = {}
APPLICATION_SUBMIT_LOCKS: dict[int, asyncio.Lock] = {}

CATEGORIES = [
    {
        "name": "👋 EINGANGSHALLE",
        "roles": None,
        "channels": [("text", "willkommen"), ("text", "ankündigungen"), ("text", "social-media"), ("text", "regeln")],
    },
    {
        "name": "💬 LOUNGE",
        "roles": None,
        "channels": [("text", "allgemein"), ("voice", "Community Voice")],
    },
    {
        "name": "🎯 VALORANT",
        "roles": None,
        "channels": [
            ("text", "valo-chat"),
            ("voice", "Valorant 1"),
            ("voice", "Valorant 2"),
            ("voice", "Valorant 3"),
            ("text", "bewerbung"),
        ],
    },
    {
        "name": "🔎 TEAM-SUCHE",
        "roles": ("Bewerber", *STAFF_ROLES),
        "channels": [("text", "looking-for-team")],
    },
    {
        "name": "🎮 TEAM",
        "roles": TEAM_ROLES,
        "channels": [("text", "team-chat"), ("text", "spieltermine")],
    },
    {
        "name": "🔒 STAFF",
        "roles": STAFF_ROLES,
        "channels": [("text", "bewerbungen"), ("text", "staff-chat"), ("text", "orga"), ("voice", "Staff Voice")],
    },
]

APPLICATION_MARKER = "DUALITY_APPLICATION_V3"
RULES_MARKER = "DUALITY_RULES_V1"
WELCOME_MARKER = "DUALITY_WELCOME_V1"
DEFAULT_GUILD_ID = "1562056155558056027"


def initialize_storage() -> None:
    with sqlite3.connect(DATA_PATH) as connection:
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


def write_rsvp(message_id: int, user_id: int, choice: str) -> None:
    with sqlite3.connect(DATA_PATH) as connection:
        connection.execute(
            "INSERT INTO event_rsvps (message_id, user_id, choice) VALUES (?, ?, ?) "
            "ON CONFLICT(message_id, user_id) DO UPDATE SET choice = excluded.choice",
            (message_id, user_id, choice),
        )


def get_rsvps(message_id: int) -> dict[str, list[int]]:
    with sqlite3.connect(DATA_PATH) as connection:
        rows = connection.execute(
            "SELECT choice, user_id FROM event_rsvps WHERE message_id = ? ORDER BY user_id",
            (message_id,),
        ).fetchall()
    result = {"going": [], "maybe": [], "declined": []}
    for choice, user_id in rows:
        result.setdefault(choice, []).append(user_id)
    return result


def save_scheduled_event(message_id: int, guild_id: int, channel_id: int, starts_at: int) -> None:
    with sqlite3.connect(DATA_PATH) as connection:
        connection.execute(
            "INSERT INTO scheduled_events (message_id, guild_id, channel_id, starts_at) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(message_id) DO UPDATE SET "
            "guild_id = excluded.guild_id, channel_id = excluded.channel_id, starts_at = excluded.starts_at",
            (message_id, guild_id, channel_id, starts_at),
        )


def get_upcoming_events(now: int) -> list[tuple[int, int, int, int]]:
    with sqlite3.connect(DATA_PATH) as connection:
        return connection.execute(
            "SELECT message_id, guild_id, channel_id, starts_at FROM scheduled_events "
            "WHERE starts_at > ? ORDER BY starts_at",
            (now,),
        ).fetchall()


def event_reminder_was_sent(message_id: int, reminder_key: str) -> bool:
    with sqlite3.connect(DATA_PATH) as connection:
        row = connection.execute(
            "SELECT 1 FROM event_reminders_sent WHERE message_id = ? AND reminder_key = ?",
            (message_id, reminder_key),
        ).fetchone()
    return row is not None


def mark_event_reminder_sent(message_id: int, reminder_key: str) -> None:
    with sqlite3.connect(DATA_PATH) as connection:
        connection.execute(
            "INSERT OR IGNORE INTO event_reminders_sent (message_id, reminder_key, sent_at) VALUES (?, ?, ?)",
            (message_id, reminder_key, int(time.time())),
        )


def delete_scheduled_event(message_id: int) -> None:
    with sqlite3.connect(DATA_PATH) as connection:
        connection.execute("DELETE FROM scheduled_events WHERE message_id = ?", (message_id,))
        connection.execute("DELETE FROM event_reminders_sent WHERE message_id = ?", (message_id,))


def save_roster_name(guild_id: int, user_id: int, public_name: str | None) -> None:
    with sqlite3.connect(DATA_PATH) as connection:
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
    with sqlite3.connect(DATA_PATH) as connection:
        rows = connection.execute(
            "SELECT user_id, public_name FROM player_roster_names WHERE guild_id = ?",
            (guild_id,),
        ).fetchall()
    return {user_id: public_name for user_id, public_name in rows}


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
    # Member overwrites come after role overwrites, so staff who also have
    # the Spieler or Bewerber role can still review applications.
    for member in guild.members:
        member_role_names = {role.name for role in member.roles}
        if member_role_names.intersection(STAFF_ROLES) and member_role_names.intersection({"Spieler", "Bewerber"}):
            overwrites[member] = discord.PermissionOverwrite(
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


def has_member_choice(member: discord.Member, roles: dict[str, discord.Role]) -> bool:
    choice_names = {"Bewerber", "Spieler"}
    return any(role.name in choice_names for role in member.roles)


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


class TrackerLinkModal(discord.ui.Modal, title="Mitspielen – Tracker.gg-Profil"):
    age_confirmation = discord.ui.TextInput(
        label="Bist du mindestens 16 Jahre alt?",
        placeholder="Bitte bestätige mit: Ja",
        style=discord.TextStyle.short,
        max_length=20,
        required=True,
    )
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
        age_confirmation = str(self.age_confirmation).strip().casefold()
        if age_confirmation not in {"ja", "ja, ich bin mindestens 16", "ich bin mindestens 16"}:
            await interaction.response.send_message(
                "Bitte bestätige, dass du mindestens 16 Jahre alt bist. Bewerbungen sind erst ab 16 möglich.",
                ephemeral=True,
            )
            return
        if not is_tracker_profile(str(self.tracker_url)):
            await interaction.response.send_message(
                "Bitte füge einen HTTPS-Link zu deinem Valorant-Profil auf tracker.gg ein.", ephemeral=True
            )
            return

        roles = {role.name: role for role in interaction.guild.roles}
        if has_member_choice(member, roles):
            await interaction.response.send_message("Du hast bereits eine Zugangsoption gewählt.", ephemeral=True)
            return

        draft = {
            "guild_id": interaction.guild.id,
            "member_id": member.id,
            "real_name": str(self.real_name).strip(),
            "course": str(self.course).strip(),
            "tracker_url": str(self.tracker_url).strip(),
        }
        await interaction.response.send_message(
            "Deine Angaben bleiben privat. Wähle im nächsten Schritt deine typische Spielzeit für jeden passenden Abend und deine Main-Rolle. Markiere außerdem alle Rollen, die du im Team spielen kannst, einschließlich deiner Main-Rolle. Die Auswahl läuft nach 10 Minuten ab.",
            view=ApplicationSelectionView(member.id, draft),
            ephemeral=True,
        )


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
        if interaction.user.id != view.member_id or interaction.guild_id != view.draft.get("guild_id"):
            await interaction.response.send_message("Diese Bewerbungsangaben gehören einem anderen Mitglied.", ephemeral=True)
            return
        setattr(view, self.state_key, list(self.values))
        await interaction.response.defer()


class ApplicationSelectionView(discord.ui.View):
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
        self.availability: list[str] = []
        self.main_role: list[str] = []
        self.playable_roles: list[str] = []
        self.add_item(ApplicationChoiceSelect(
            state_key="availability",
            placeholder="Spielzeiten auswählen: Tag und Zeitfenster (Mehrfachauswahl)",
            options=[
                discord.SelectOption(
                    label=f"{short_day} · {window}",
                    value=f"{day}|{window}",
                    description=f"Typische Spielzeit am {day}",
                )
                for day, short_day in self.DAYS
                for window in self.TIME_WINDOWS
            ],
            minimum=1,
            maximum=len(self.DAYS) * len(self.TIME_WINDOWS),
            row=0,
        ))
        self.add_item(ApplicationChoiceSelect(
            state_key="main_role",
            placeholder="Deine Main-Rolle auswählen",
            options=[discord.SelectOption(label=role, value=role) for role in VALORANT_ROLES],
            minimum=1,
            maximum=1,
            row=1,
        ))
        self.add_item(ApplicationChoiceSelect(
            state_key="playable_roles",
            placeholder="Welche Rollen kannst du spielen? (Main-Rolle mit auswählen)",
            options=[discord.SelectOption(label=role, value=role) for role in VALORANT_ROLES],
            minimum=1,
            maximum=len(VALORANT_ROLES),
            row=2,
        ))

    async def on_timeout(self):
        self.draft.clear()

    @discord.ui.button(label="Bewerbung abschicken", style=discord.ButtonStyle.success, row=3)
    async def submit_application(self, interaction: discord.Interaction, button: discord.ui.Button):
        if (
            not interaction.guild
            or not isinstance(interaction.user, discord.Member)
            or interaction.user.id != self.member_id
            or interaction.guild.id != self.draft.get("guild_id")
        ):
            await interaction.response.send_message("Diese Bewerbung gehört einem anderen Mitglied oder Server.", ephemeral=True)
            return

        member = interaction.user
        roles = {role.name: role for role in interaction.guild.roles}
        if has_member_choice(member, roles):
            await interaction.response.send_message("Du hast bereits eine Zugangsoption gewählt.", ephemeral=True)
            return
        if not self.availability or not self.main_role:
            await interaction.response.send_message(
                "Bitte wähle deine Spielzeiten und Main-Rolle aus.",
                ephemeral=True,
            )
            return
        if self.main_role[0] not in self.playable_roles:
            await interaction.response.send_message(
                "Bitte wähle bei den spielbaren Rollen auch deine Main-Rolle aus.", ephemeral=True
            )
            return
        applicant_role = roles.get("Bewerber")
        staff_channel = discord.utils.get(interaction.guild.text_channels, name="bewerbungen")
        if not applicant_role or not staff_channel:
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
            if has_member_choice(member, roles):
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
            application_embed.add_field(name="Main-Rolle", value=self.main_role[0], inline=True)
            application_embed.add_field(
                name="Spielbare Rollen (inkl. Main-Rolle)", value=", ".join(self.playable_roles), inline=True
            )
            short_days = dict(self.DAYS)
            selected_times = [tuple(value.split("|", 1)) for value in self.availability]
            selected_times.sort(
                key=lambda item: (
                    next(index for index, (day, _) in enumerate(self.DAYS) if day == item[0]),
                    self.TIME_WINDOWS.index(item[1]),
                )
            )
            availability_text = "\n".join(f"{short_days[day]} · {window}" for day, window in selected_times)
            application_embed.add_field(name="Spielzeiten (Mi–So)", value=availability_text, inline=False)
            application_embed.add_field(name="Tracker.gg", value=str(self.draft["tracker_url"])[:1024], inline=False)
            application_embed.set_footer(text=f"{APPLICATION_PENDING_PREFIX}{member.id}")
            try:
                await staff_channel.send(
                    embed=application_embed,
                    view=ApplicationReviewView(),
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            except discord.HTTPException:
                try:
                    await member.remove_roles(applicant_role, reason="Bewerbung konnte nicht an den Staff übermittelt werden")
                except discord.HTTPException:
                    logging.exception("Bewerberrolle konnte nach fehlgeschlagener Übermittlung nicht zurückgenommen werden")
                await interaction.followup.send(
                    "Die Bewerbung konnte gerade nicht an den Staff übermittelt werden. Bitte versuche es später erneut oder kontaktiere Staff.",
                    ephemeral=True,
                )
                return

            await interaction.followup.send(
                "Danke! Deine Bewerbung wurde an den Staff geschickt. Du hast jetzt die Rolle `Bewerber`, Zugang zu `#looking-for-team` und kannst `#bewerbung` nicht mehr sehen oder erneut nutzen. Der Staff meldet sich nach der Prüfung bei dir.",
                ephemeral=True,
            )
            self.draft.clear()
            self.stop()


class OnboardingView(discord.ui.View):
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
        roles = {role.name: role for role in interaction.guild.roles}
        if has_member_choice(interaction.user, roles):
            await interaction.response.send_message("Du hast bereits eine Team-Rolle.", ephemeral=True)
            return
        await interaction.response.send_modal(TrackerLinkModal())


async def review_application(interaction: discord.Interaction, accepted: bool) -> None:
    if not interaction.message:
        await interaction.response.send_message("Diese Entscheidung ist nur über eine Bewerbungsnachricht möglich.", ephemeral=True)
        return
    lock = APPLICATION_LOCKS.setdefault(interaction.message.id, asyncio.Lock())
    async with lock:
        await _review_application_locked(interaction, accepted)


async def _review_application_locked(interaction: discord.Interaction, accepted: bool) -> None:
    if not interaction.guild or not isinstance(interaction.user, discord.Member) or not interaction.message:
        await interaction.response.send_message("Diese Entscheidung ist nur im Server möglich.", ephemeral=True)
        return

    staff_role_names = {role.name for role in interaction.user.roles}
    if not staff_role_names.intersection(STAFF_ROLES):
        await interaction.response.send_message("Nur Coach/Admin kann Bewerbungen entscheiden.", ephemeral=True)
        return

    if not interaction.message.embeds:
        await interaction.response.send_message("Diese Nachricht ist keine gültige Bewerbung.", ephemeral=True)
        return
    embed = discord.Embed.from_dict(interaction.message.embeds[0].to_dict())
    footer = embed.footer.text or ""
    if not footer.startswith(APPLICATION_PENDING_PREFIX):
        await interaction.response.send_message("Diese Bewerbung wurde bereits bearbeitet.", ephemeral=True)
        return

    try:
        applicant_id = int(footer.removeprefix(APPLICATION_PENDING_PREFIX))
    except ValueError:
        await interaction.response.send_message("Die Bewerber-ID in dieser Nachricht ist ungültig.", ephemeral=True)
        return

    roles = {role.name: role for role in interaction.guild.roles}
    player_role = roles.get("Spieler")
    applicant_role = roles.get("Bewerber")
    if not player_role or not applicant_role:
        await interaction.response.send_message("Die benötigten Rollen fehlen. Bitte Staff prüfen lassen.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True, thinking=True)
    applicant = interaction.guild.get_member(applicant_id)
    if applicant is None:
        try:
            applicant = await interaction.guild.fetch_member(applicant_id)
        except discord.NotFound:
            await interaction.followup.send(
                "Die Person ist nicht mehr auf dem Server. Die Bewerbung wurde nicht automatisch entschieden.",
                ephemeral=True,
            )
            return

    if accepted:
        await applicant.add_roles(player_role, reason=f"Bewerbung angenommen von {interaction.user}")
        await applicant.remove_roles(applicant_role, reason="Bewerbung angenommen")
        decision = "ACCEPTED"
        decision_text = f"Angenommen von {interaction.user.mention} · <t:{int(time.time())}:F>"
        dm_text = "Deine Bewerbung bei Duality Mannheim wurde angenommen. Du hast jetzt die Rolle `Spieler` und Zugang zum Teambereich. Willkommen im Team!"
    else:
        if applicant_role not in applicant.roles:
            await applicant.add_roles(applicant_role, reason="Bewerberstatus nach Ablehnung beibehalten")
        decision = "DECLINED"
        decision_text = f"Abgelehnt von {interaction.user.mention} · <t:{int(time.time())}:F>"
        dm_text = "Danke für deine Bewerbung bei Duality Mannheim. Der Staff kann dir aktuell keinen Teamplatz anbieten. Deine Rolle `Bewerber` bleibt bestehen, daher kannst du über `#bewerbung` keine weitere Bewerbung einreichen. Wenn du Rückfragen hast, wende dich bitte direkt an den Staff."

    dm_sent = True
    try:
        await applicant.send(dm_text)
    except (discord.Forbidden, discord.HTTPException):
        dm_sent = False

    decided_at = int(time.time())
    embed.add_field(name="Entscheidung", value=decision_text, inline=False)
    embed.color = discord.Color.green() if accepted else discord.Color.red()
    embed.set_footer(text=f"{APPLICATION_DECISION_PREFIX}{decision}|{applicant_id}|{interaction.user.id}|{decided_at}")
    await interaction.message.edit(embed=embed, view=None)

    response = "Bewerbung angenommen" if accepted else "Bewerbung abgelehnt; Bewerberrolle bleibt bestehen und sperrt eine erneute Bewerbung"
    if not dm_sent:
        response += ". Die DM konnte nicht zugestellt werden"
    if not accepted:
        response += f". Der Eintrag wird nach {APPLICATION_RETENTION_DAYS} Tagen automatisch gelöscht"
    await interaction.followup.send(response + ".", ephemeral=True)


class ApplicationReviewView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Annehmen", style=discord.ButtonStyle.success, custom_id="duality:application:accept")
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        await review_application(interaction, accepted=True)

    @discord.ui.button(label="Ablehnen", style=discord.ButtonStyle.danger, custom_id="duality:application:decline")
    async def decline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await review_application(interaction, accepted=False)


async def toggle_notification_role(interaction: discord.Interaction, role_name: str) -> None:
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        await interaction.response.send_message("Bitte nutze den Button auf dem Server.", ephemeral=True)
        return
    role = discord.utils.get(interaction.guild.roles, name=role_name)
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


class NotificationView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ValorantRankSelect())

    @discord.ui.button(
        label="Valorant-Updates",
        style=discord.ButtonStyle.secondary,
        custom_id="duality:notify:valorant",
        row=1,
    )
    async def valorant(self, interaction: discord.Interaction, button: discord.ui.Button):
        await toggle_notification_role(interaction, "Ping Valorant")

    @discord.ui.button(
        label="Community-Events",
        style=discord.ButtonStyle.secondary,
        custom_id="duality:notify:events",
        row=1,
    )
    async def events(self, interaction: discord.Interaction, button: discord.ui.Button):
        await toggle_notification_role(interaction, "Ping Events")


class ValorantRankSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label=rank, value=rank, description=f"Aktueller Rang: {rank}")
            for rank in VALORANT_RANKS
        ]
        super().__init__(
            placeholder="Deinen Valorant-Rang optional auswählen",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="duality:rank:set",
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("Bitte nutze die Rang-Auswahl direkt auf dem Server.", ephemeral=True)
            return

        selected_name = self.values[0]
        role_by_name = {role.name: role for role in interaction.guild.roles}
        selected_role = role_by_name.get(selected_name)
        if selected_name not in VALORANT_RANKS or selected_role is None:
            await interaction.response.send_message(
                "Diese Rangrolle ist noch nicht eingerichtet. Bitte informiere den Staff.", ephemeral=True
            )
            return

        previous_ranks = [
            role for role in interaction.user.roles
            if role.name in VALORANT_RANKS and role.id != selected_role.id
        ]
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if selected_role not in interaction.user.roles:
                await interaction.user.add_roles(selected_role, reason="Valorant-Rang selbst ausgewählt")
            if previous_ranks:
                await interaction.user.remove_roles(*previous_ranks, reason="Valorant-Rang geändert")
        except discord.Forbidden:
            await interaction.followup.send(
                "Der Bot darf die Rangrolle gerade nicht vergeben. Bitte prüfe, ob seine Rolle über den Rangrollen steht.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            await interaction.followup.send(
                "Dein Rang konnte gerade nicht gespeichert werden. Bitte versuche es später erneut.", ephemeral=True
            )
            return

        await interaction.followup.send(
            f"Dein angegebener Valorant-Rang ist jetzt **{selected_name}**. Du kannst ihn jederzeit hier ändern.",
            ephemeral=True,
        )


async def update_event_rsvps(interaction: discord.Interaction, choice: str) -> None:
    if not interaction.guild or not interaction.message or not interaction.message.embeds:
        await interaction.response.send_message("Dieser Termin ist ungültig.", ephemeral=True)
        return
    if not (interaction.message.embeds[0].footer.text or "").startswith(EVENT_MARKER):
        await interaction.response.send_message("Das ist kein Termin des Duality-Bots.", ephemeral=True)
        return

    write_rsvp(interaction.message.id, interaction.user.id, choice)
    responses = get_rsvps(interaction.message.id)
    embed = discord.Embed.from_dict(interaction.message.embeds[0].to_dict())
    embed.clear_fields()
    labels = {
        "going": "✅ Dabei",
        "maybe": "🤔 Vielleicht",
        "declined": "❌ Kann nicht",
    }
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
    await interaction.response.defer(ephemeral=True, thinking=True)
    await interaction.message.edit(embed=embed, view=EventRSVPView())
    await interaction.followup.send(f"Deine Antwort wurde auf „{labels[choice]}“ gesetzt.", ephemeral=True)


class EventRSVPView(discord.ui.View):
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
            reminder_label = "Training/Scrim steht bald an"
        elif 0 < seconds_until_start <= 60 * 60:
            reminder_key = "1h"
            reminder_label = "Training/Scrim beginnt bald"
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
                print(f"Erinnerung für Termin {message_id} konnte Kanal nicht laden: {error}", file=sys.stderr)
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
            print(f"Erinnerung für Termin {message_id} konnte Termin nicht laden: {error}", file=sys.stderr)
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
            print(f"Erinnerung für Termin {message_id} konnte nicht gesendet werden: {error}", file=sys.stderr)
            continue

        mark_event_reminder_sent(message_id, reminder_key)


async def register_existing_scheduled_events(guild: discord.Guild) -> int:
    event_channel = discord.utils.get(guild.text_channels, name="spieltermine")
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
        print(f"Vorhandene Termine konnten nicht für Erinnerungen eingelesen werden: {error}", file=sys.stderr)

    return imported


def register_app_commands(tree: app_commands.CommandTree) -> None:
    @tree.command(name="help", description="Zeigt die wichtigsten Server- und Bot-Aktionen.")
    async def help_command(interaction: discord.Interaction):
        embed = discord.Embed(
            title="Duality Mannheim – Hilfe",
            description="Hier findest du die wichtigsten Wege durch den Server.",
            color=discord.Color.from_rgb(110, 70, 255),
        )
        embed.add_field(
            name="Community",
            value="Lies `#regeln`, stell dich in `#allgemein` vor und nutze die Buttons in `#willkommen`, um Benachrichtigungen auszuwählen.",
            inline=False,
        )
        embed.add_field(
            name="Valorant-Team",
            value="`#bewerbung` ist für alle sichtbar. Starte dort über den Button das Teamformular. Nach dem Absenden erhältst du `Bewerber` und kannst den Bewerbungskanal nicht erneut nutzen. Deinen Rang kannst du optional im Dropdown in `#willkommen` angeben.",
            inline=False,
        )
        embed.add_field(
            name="Bot-Befehle",
            value="`/report` meldet ein Problem privat an Staff · `/feedback` sendet eine Idee · `/team` zeigt den aktuellen Kader · `/teamname` setzt optional deinen Namen für den Kader · `/termin` erstellt einen Teamtermin (Staff).",
            inline=False,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @tree.command(name="report", description="Meldet ein Problem vertraulich an den Staff.")
    @app_commands.describe(details="Was ist passiert? Bitte keine unnötigen privaten Daten eintragen.", member="Betroffenes Mitglied, falls relevant")
    async def report_command(
        interaction: discord.Interaction,
        details: str,
        member: discord.Member | None = None,
    ):
        if not interaction.guild:
            await interaction.response.send_message("Dieser Befehl funktioniert nur im Server.", ephemeral=True)
            return
        text = details.strip()
        if len(text) < 10 or len(text) > 1500:
            await interaction.response.send_message("Bitte beschreibe das Problem mit 10 bis 1500 Zeichen.", ephemeral=True)
            return
        staff_channel = discord.utils.get(interaction.guild.text_channels, name="orga")
        if staff_channel is None:
            await interaction.response.send_message("Der Staff-Eingang ist noch nicht eingerichtet.", ephemeral=True)
            return
        embed = discord.Embed(
            title="Vertraulicher Mitgliederbericht",
            description=text,
            color=discord.Color.orange(),
            timestamp=discord.utils.utcnow(),
        )
        embed.add_field(name="Von", value=f"{interaction.user} (`{interaction.user.id}`)", inline=True)
        embed.add_field(name="Betroffenes Mitglied", value=str(member) if member else "Nicht angegeben", inline=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        await staff_channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        await interaction.followup.send(
            "Danke. Dein Bericht wurde vertraulich an Staff gesendet. Staff kann deinen Discord-Namen sehen.",
            ephemeral=True,
        )

    @tree.command(name="feedback", description="Sendet eine Idee oder Rückmeldung privat an den Staff.")
    async def feedback_command(interaction: discord.Interaction, suggestion: str):
        if not interaction.guild:
            await interaction.response.send_message("Dieser Befehl funktioniert nur im Server.", ephemeral=True)
            return
        text = suggestion.strip()
        if len(text) < 5 or len(text) > 1500:
            await interaction.response.send_message("Bitte schreibe 5 bis 1500 Zeichen.", ephemeral=True)
            return
        staff_channel = discord.utils.get(interaction.guild.text_channels, name="orga")
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
        player_role = discord.utils.get(interaction.guild.roles, name="Spieler")
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
        player_role = discord.utils.get(interaction.guild.roles, name="Spieler")
        if player_role is None or player_role not in interaction.user.roles:
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
    @app_commands.describe(
        title="Name des Termins",
        when="Datum und Uhrzeit im Format TT.MM.JJJJ HH:MM, deutsche Zeit",
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
        if not {role.name for role in interaction.user.roles}.intersection(STAFF_ROLES):
            await interaction.response.send_message("Nur Coach/Admin kann Teamtermine erstellen.", ephemeral=True)
            return
        event_channel = discord.utils.get(interaction.guild.text_channels, name="spieltermine")
        if event_channel is None:
            await interaction.response.send_message("`#spieltermine` wurde nicht gefunden.", ephemeral=True)
            return
        if interaction.channel_id != event_channel.id:
            await interaction.response.send_message("Bitte nutze `/termin` in `#spieltermine`.", ephemeral=True)
            return
        try:
            event_time = datetime.strptime(when.strip(), "%d.%m.%Y %H:%M").replace(tzinfo=ZoneInfo("Europe/Berlin"))
        except ValueError:
            await interaction.response.send_message("Nutze das Format `TT.MM.JJJJ HH:MM`, zum Beispiel `25.10.2026 19:30`.", ephemeral=True)
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


async def setup_guild(guild: discord.Guild, apply: bool) -> None:
    print(f"Server: {guild.name} ({guild.id})")
    category_renames = {
        "📌 START HIER": "👋 EINGANGSHALLE",
        "💬 COMMUNITY": "💬 LOUNGE",
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

    roles = {role.name: role for role in guild.roles}
    for name in ROLE_NAMES:
        if name in roles:
            print(f"  Rolle vorhanden: {name}")
        elif apply:
            role = await guild.create_role(name=name, reason="Duality Mannheim Setup")
            roles[name] = role
            print(f"  Rolle erstellt: {name}")
        else:
            print(f"  Rolle würde erstellt: {name}")

    legacy_lft_role = roles.get("LFT")
    applicant_role = roles.get("Bewerber")
    player_role = roles.get("Spieler")
    if legacy_lft_role and applicant_role:
        old_lft_members = list(legacy_lft_role.members)
        if apply:
            for member in old_lft_members:
                if applicant_role not in member.roles and player_role not in member.roles:
                    await member.add_roles(applicant_role, reason="Bisherige LFT-Bewerbung auf Bewerber umgestellt")
                await member.remove_roles(legacy_lft_role, reason="Duality Mannheim verwendet keine LFT-Rolle mehr")
            print(f"  Alte LFT-Zuweisungen auf Bewerber umgestellt oder entfernt: {len(old_lft_members)}")
        elif old_lft_members:
            print(f"  Alte LFT-Zuweisungen würden auf Bewerber umgestellt oder entfernt: {len(old_lft_members)}")

    category_lookup = {category.name: category for category in guild.categories}
    old_announcements = next(
        (
            channel
            for channel in guild.text_channels
            if "".join(character for character in channel.name.casefold() if character.isalnum()) == "announcements"
        ),
        None,
    )
    if old_announcements:
        if apply:
            try:
                await old_announcements.delete(
                    reason="Duality Mannheim verwendet #ankündigungen statt #announcements"
                )
                print("  Überflüssigen Kanal entfernt: #announcements")
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
        else:
            print("  Kanal würde entfernt: #announcements")

    old_valorant_announcements = discord.utils.get(guild.text_channels, name="valo-announcements")
    if old_valorant_announcements:
        if apply:
            await old_valorant_announcements.delete(
                reason="Duality Mannheim nutzt den zentralen Ankündigungskanal"
            )
            print("  Überflüssigen Kanal entfernt: #valo-announcements")
        else:
            print("  Kanal würde entfernt: #valo-announcements")

    old_tryout_category = category_lookup.get("🧪 TRYOUTS")
    if old_tryout_category:
        if apply:
            for old_channel in list(old_tryout_category.channels):
                await old_channel.delete(reason="Duality Mannheim entfernt Tryout-Kanäle")
                print(f"  Alten Kanal entfernt: {old_channel.name}")
            await old_tryout_category.delete(reason="Duality Mannheim entfernt Tryout-Kategorie")
            print("  Alte Kategorie entfernt: 🧪 TRYOUTS")
        else:
            print("  Alte Tryout-Kategorie und alle darin liegenden Kanäle würden entfernt")

    old_training_category = category_lookup.get("🏆 TRAINING & SCRIMS")
    if old_training_category:
        if apply:
            for old_channel in list(old_training_category.channels):
                await old_channel.delete(reason="Duality Mannheim entfernt Training- und Scrim-Bereich")
                print(f"  Alten Training-/Scrim-Kanal entfernt: {old_channel.name}")
            await old_training_category.delete(reason="Duality Mannheim entfernt Training- und Scrim-Bereich")
            print("  Alte Kategorie entfernt: 🏆 TRAINING & SCRIMS")
        else:
            print("  Training-/Scrim-Kategorie und alle darin enthaltenen Kanäle würden entfernt")

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

    for config in CATEGORIES:
        name = config["name"]
        category = category_lookup.get(name)
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
            print(f"  Kategorie erstellt: {name}")
        elif apply:
            await category.edit(overwrites=expected_overwrites, reason="Duality Mannheim Rollen-Zugänge")
            print(f"  Kategorie-Berechtigungen gesetzt: {name}")
        else:
            print(f"  Kategorie vorhanden: {name}; Zugänge würden aktualisiert")

        existing = {channel.name: channel for channel in category.channels}
        if name == "💬 LOUNGE" and "general" in existing:
            old_general = existing["general"]
            if "allgemein" not in existing:
                if apply:
                    await old_general.edit(name="allgemein", reason="Duality Mannheim: allgemeiner Chat auf Deutsch")
                    existing.pop("general")
                    existing["allgemein"] = old_general
                    print("    Kanal umbenannt: general → allgemein")
                else:
                    print("    Kanal würde umbenannt: general → allgemein")
            elif apply:
                await old_general.delete(reason="Duality Mannheim entfernt doppelten #general-Kanal")
                existing.pop("general")
                print("    Doppelter Kanal entfernt: general")
            else:
                print("    Doppelter Kanal general würde entfernt")

        for kind, channel_name in config["channels"]:
            channel = existing.get(channel_name)
            if channel:
                if apply and channel_name == "bewerbung":
                    await channel.edit(
                        overwrites=application_overwrites(guild, roles),
                        reason="Bewerbung öffentlich lesbar; Spieler und Bewerber werden ausgeschlossen",
                    )
                    print("    Bewerbungszugriff gesetzt: alle sehen den Kanal; Bewerber und Spieler nicht")
                elif apply and channel_name == "regeln":
                    await channel.edit(
                        overwrites=rules_overwrites(guild, roles),
                        reason="Regeln-Kanal für Mitglieder lesbar und nur für Staff beschreibbar",
                    )
                    print("    Regeln-Kanal auf Lesen beschränkt")
                elif apply and not channel.permissions_synced:
                    await channel.edit(sync_permissions=True, reason="Duality Mannheim Kategorie-Zugänge")
                    print(f"    Kanal an Kategorie-Zugänge angepasst: {channel_name}")
                else:
                    print(f"    Kanal vorhanden: {channel_name}")
            elif apply:
                if kind == "voice":
                    await guild.create_voice_channel(channel_name, category=category, reason="Duality Mannheim Setup")
                elif channel_name == "bewerbung":
                    await guild.create_text_channel(
                        channel_name,
                        category=category,
                        overwrites=application_overwrites(guild, roles),
                        reason="Duality Mannheim Bewerbung öffentlich lesbar einrichten",
                    )
                elif channel_name == "regeln":
                    await guild.create_text_channel(
                        channel_name,
                        category=category,
                        overwrites=rules_overwrites(guild, roles),
                        reason="Duality Mannheim Regelkanal",
                    )
                else:
                    await guild.create_text_channel(channel_name, category=category, reason="Duality Mannheim Setup")
                print(f"    {kind.capitalize()}-Kanal erstellt: {channel_name}")
            else:
                print(f"    {kind.capitalize()}-Kanal würde erstellt: {channel_name}")

    application_channel = discord.utils.get(guild.text_channels, name="bewerbung")
    if apply and application_channel:
        embed = discord.Embed(
            title="Bewerbung für Duality Mannheim",
            description=(
                "Du möchtest für Duality Mannheim Valorant spielen? Dieser Kanal ist für alle Mitglieder sichtbar. "
                "Starte hier über den Button deine Bewerbung. Nach dem Absenden erhältst du die Rolle `Bewerber`; "
                "danach ist dieser Kanal für dich nicht mehr sichtbar und du kannst keine weitere Bewerbung einreichen.\n\n"
                "Du brauchst deinen Tracker.gg-Profillink und bestätigst, dass du mindestens 16 bist. "
                "Außerdem wählst du deine Main-Rolle (Duelist, Initiator, Controller oder Sentinel), alle Rollen, "
                "die du spielen kannst, sowie deine Verfügbarkeit pro Abend von Mittwoch bis Sonntag aus. "
                "Du kannst zum Beispiel Mittwoch 19–22 Uhr und Freitag 20–23 Uhr getrennt auswählen. "
                "Wir fragen auch deinen bevorzugten Namen und optional Kurs/Ausbildung ab.\n\n"
                "Deine Angaben werden privat an den Staff geschickt. Nach dem Absenden erhältst du Zugang zu `#looking-for-team`. "
                "Auch bei einer Ablehnung bleibt die Rolle `Bewerber` bestehen. Wenn du Rückfragen hast, wende dich direkt an den Staff. "
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

    rules_channel = discord.utils.get(guild.text_channels, name="regeln")
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

    welcome = discord.utils.get(guild.text_channels, name="willkommen")
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
                "Nach dem Absenden erhältst du die Rolle `Bewerber` und kannst den Kanal weder sehen noch erneut nutzen. "
                "Bewerbungen sind "
                "ab 16 Jahren möglich; besonders wichtig sind Spielzeiten Mittwoch bis Sonntag ab 19 Uhr. "
                "Deinen aktuellen Valorant-Rang kannst du optional über das Dropdown unter dieser Nachricht angeben "
                "und später jederzeit ändern. Der Rang wird nicht in der Bewerbung abgefragt.\n\n"
                "Nach dem Absenden prüft der Staff deine Bewerbung. Zugang zum Teambereich gibt es erst nach einer Annahme.\n\n"
                "**Benachrichtigungen**\n"
                "Nutze die Buttons unter dieser Nachricht, um freiwillig Pings für Valorant-Updates oder Community-Events zu erhalten."
                " Dein aktueller Valorant-Rang ist optional und kann über das Rang-Dropdown hier geändert werden."
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
    if apply:
        initialize_storage()
    intents = discord.Intents.none()
    intents.guilds = True
    intents.members = True
    client = discord.Client(intents=intents)
    tree = app_commands.CommandTree(client)
    register_app_commands(tree)
    client.add_view(OnboardingView())
    client.add_view(ApplicationReviewView())
    client.add_view(NotificationView())
    client.add_view(EventRSVPView())

    @tasks.loop(hours=24)
    async def purge_declined_applications():
        guild = client.get_guild(target_id)
        if guild is None:
            return
        staff_channel = discord.utils.get(guild.text_channels, name="bewerbungen")
        if staff_channel is None:
            return
        cutoff = int(time.time()) - APPLICATION_RETENTION_DAYS * 24 * 60 * 60
        async for message in staff_channel.history(limit=None):
            if not message.embeds:
                continue
            footer = message.embeds[0].footer.text or ""
            if not footer.startswith(f"{APPLICATION_DECISION_PREFIX}DECLINED|"):
                continue
            try:
                decided_at = int(footer.split("|")[3])
            except (IndexError, ValueError):
                continue
            if decided_at <= cutoff:
                await message.delete()
                print(f"  Abgelehnte Bewerbung nach {APPLICATION_RETENTION_DAYS} Tagen gelöscht")

    @purge_declined_applications.error
    async def purge_declined_applications_error(error: Exception):
        print(f"Fehler bei der Bewerbungs-Löschfrist: {error}", file=sys.stderr)

    @tasks.loop(minutes=1)
    async def event_reminders():
        await send_due_event_reminders(client)

    @event_reminders.error
    async def event_reminders_error(error: Exception):
        print(f"Fehler bei den Termin-Erinnerungen: {error}", file=sys.stderr)

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
    async def on_ready():
        nonlocal setup_complete, commands_synced, existing_events_registered
        print(f"Mit Discord verbunden als {client.user}.", flush=True)
        guild = client.get_guild(target_id)
        if guild is None:
            print("Bot nicht im Server oder Server-ID falsch. Prüfe die Server-ID und lade den Bot in den Server ein.", file=sys.stderr, flush=True)
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
            if not purge_declined_applications.is_running():
                purge_declined_applications.start()
            if not event_reminders.is_running():
                event_reminders.start()
            print("Onboarding-Bot läuft. Mit Ctrl+C beenden.")
        else:
            await client.close()

    async with client:
        print("Verbinde mit Discord ...", flush=True)
        await client.start(token)


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
