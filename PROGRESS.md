# Fortschritt: Web-Panel und neue Features

Gedächtnis für die Arbeit nach `PANEL_PROMPT.md`. Vor jeder Arbeit lesen, nach jedem Feature aktualisieren.
Status: `offen` · `in Arbeit` · `fertig (<Commit>)`

## Phasen und Features

### Vorbereitung
- [x] `update-mac.sh`: PROGRESS.md, PANEL_PROMPT.md, README.md in `FILES`, Prüfung `-f` → `-e` — fertig (746936f)

### Phase 1 – Grundgerüst (A1, A2)
- [x] aiohttp-Server in derselben Event-Loop + Healthcheck `/healthz` — fertig (4a8fd5a)
- [x] Discord-OAuth2-Login (Scope `identify`, `state`-Prüfung) — fertig (dto.)
- [x] Signierte Session-Cookies (HMAC-SHA256, 12 h, HttpOnly/Secure/SameSite=Lax) — fertig (dto.)
- [x] Rechte-Stufen Coach/Admin/Owner, live bei jeder Anfrage — fertig (dto.)
- [x] CSRF-Schutz (Token + Origin) für POST (4a8fd5a) und WebSocket (058f13a)
- [x] Rate-Limit Login 10/5 min pro IP (4a8fd5a), Konsolenbefehle 20/min pro Person (058f13a)
- [x] Log-Handler mit Ringpuffer (1000) + `print`-Ausgaben im Panel — fertig (058f13a)
- [x] WebSocket-Konsole `/ws` mit Whitelist-Befehlen help, sync-ranks, reminders, purge, cleanup-voice, say, restart — fertig (058f13a); `backup` kommt mit B2
- [x] Dashboard (A1) + Befehle `status`/`tasks` + `/api/status` — fertig (6c7a2f7); „Letztes Backup“ und „Letzte Warnungen“ kommen mit B1/B2
- [x] Tests ohne Discord-Verbindung: Session, CSRF, Rate-Limit, Konfiguration, Rechte, HTTP-Endpunkte, Parser, Log-Puffer, WebSocket-Befehle, Dashboard-Status, Task-Tracking, HenrikDev-Status; ab Phase 2 Bot-Tests in `tests/test_bot.py`

### Phase 2 – Betrieb
- [x] B1 Fehler-Benachrichtigung (Webhook, Entprellung, „wieder in Ordnung“, Dashboard-Karte) — fertig (fc281ab)
- [x] B2 Datenbank-Backups (SQLite-Backup-API, Aufbewahrung, Download für Owner, Konsolenbefehl `backup`, Warnung `backup`) — fertig (6323545)
- [x] B4 Live-Ansicht der Voice-Kanäle — fertig (1aae0e4)
- [x] A3 Bewerbungen lesen + „Neue Saison“ — fertig (5d05767)
- [x] A4 Spieler & Profile + „Rang jetzt abgleichen“ — fertig (d8e3a91)
- [x] A5 Tickets — fertig (a770e37); Phase 2 auf dem VPS seit 21.10.2026

### Phase 3 – Team
- [x] Gemeinsame Termin-Funktion für `/termin` und Panel — fertig (20f2037)
- [x] C2 Termine im Panel (anlegen, bearbeiten/verschieben, absagen) — fertig (260e713)
- [x] C1 Anwesenheitsübersicht + CSV — fertig (e7aa165)
- [x] C3 Glückwunsch bei Rangaufstieg — fertig (8899488)
- [x] C4 `/rangliste` + Panel-Ansicht + `rank_history` — fertig (aa72e6d)

### Phase 4 – Konfiguration & Kommunikation
- [x] D3 Einstellungen (`bot_settings`, `setting()`-Helfer mit Cache) — fertig (283261a)
- [x] D2 Texte bearbeiten (`bot_texts`, Verlauf, Discord-Nachrichten aktualisieren) — fertig (08bf42d)
- [x] D1 Ankündigungs-Editor (sofort/zeitgesteuert, `scheduled_announcements`) — fertig (ce5de97)
- [x] E1 Datenschutz-Seite (Auskunft/Löschung) — fertig (e5499f4)

### Phase 5 – Setup-Diff
- [x] B3 `setup_guild` liefert strukturierten Diff, Panel-Seite, Anwenden mit Lock — fertig (7f87f7d)

### Phase 6 – Scrim-Suche (ZURÜCKGESTELLT am 21.10.2026 auf Wunsch von Arvids, nichts umgesetzt)
- [x] Recherche-Tabelle vorgelegt (21.10.2026); Entscheidung: Scrim-Feature vorerst komplett weglassen
  - Ergebnis: Keine der Scrim-Börsen (Curry.gg, Ascend, Scrimps, Esports Planner, Gankster) hat eine öffentliche API oder einen Feed. Ascend verbietet Scraping und automatische Anfragen ausdrücklich (AGB Abschnitt 08); bei Curry.gg/Scrimps waren die AGB nicht abrufbar → nicht automatisieren.
  - Uniliga (Hochschulliga DE, passt am besten): Anmeldung über OPLeague (früher Toornament), Kontakt über den Uniliga-Discord, keine API gefunden.
  - Toornament hat eine offizielle API (Viewer/Organizer, API-Key) → könnte Teilnehmerlisten von Turnieren liefern, nur sinnvoll, falls eine Liga dort läuft.
  - Discord-Scrim-Server (Valorant Scrim DE, VERSUS VALORANT, Valorant Europe): Der Bot kann sie nicht lesen (nur mit Einladung durch deren Admins), Self-Bots verboten → nur manuell posten.
  - FACEIT Data API (offiziell, Key) ohne Scrim-Börse; Riot/HenrikDev ohne Scrim-Daten.
  - Vorschlag: manuelle Scrim-Börse + Gesuch-Text zum Kopieren + Anfragen-Tracking + Termin + Einladungslink, keine Plattform anbinden (Option A).
  - Nachtrag (Hinweis von Arvids): **PRACC.COM** (stonefire GmbH, Kassel) hat Valorant-Scrimsearch und einen **offiziellen Discord-Bot**: Team-Seite auf pracc.com mit dem eigenen Server verknüpfen → Bot legt Kanäle an (Post-LFS, My-LFS, matching-LFS, Team-Chats, Match-Kanäle mit Angeboten). Keine öffentliche API. AGB: Kontoinhaber ≥ 18 (4.1), keine Eingriffe in die Funktion (4.5) → inoffizielle Automatisierungs-Erweiterungen (z. B. pracc-valorant-request-bot) nicht nutzen. Preis für Scrimsearch auf der Seite nicht angegeben. Damit ist PRACC die beste erlaubte Quelle; Vorschlag: PRACC-Bot nutzen, eigene Phase 6 verkleinern (Scrim-Protokoll + „bestätigt → Termin“ + Gast-Einladungslink, ggf. Anfrage-Formular), keine eigene Börse.
  - Weitere offizielle APIs geprüft: start.gg, Challengermode, Toornament, FACEIT (nur Turnier-/Teamdaten, keine Scrim-Gesuche). HenrikDev hat Premier-Endpunkte (Rangliste nach Konferenz/Division) als mögliche Vorschlagsliste.
- [ ] F Scrim-Börse, eigenes Gesuch, `scrim_requests`, Termin aus Scrim, Einladungslink — zurückgestellt (bei Wiederaufnahme: PRACC-Bot als Quelle prüfen, `scrim_requests` in Auskunft/Löschung aufnehmen)

### Phase 7 – Abschluss
- [x] README (Web-Panel, Env-Variablen, systemd, Caddy + nginx wie auf dem VPS, SSH-Tunnel, Backup zurückspielen, Webhook, Datenschutz, Deploy) — fertig (f24331b)
- [x] Update-Skripte geprüft: `update-mac.sh` bricht jetzt vor dem Commit ab, wenn `py_compile` oder ein Test scheitert; `update-vps.sh` prüft nach dem Pull die Syntax und startet sonst nicht neu — fertig (dto.)
- [x] `/help` nennt `/rangliste` (seit aa72e6d, Text im Panel änderbar) — fertig

## Entscheidungen und Annahmen
- **VPS-Zugriff erlaubt** (Arvids, 21.10.2026), Ausnahme von „VPS nicht anfassen“ für die Panel-Einrichtung. Login per SSH-Schlüssel als root@217.154.173.183 (nie per Passwort). Vor jeder Änderung am laufenden Bot fragen.
- Commits ohne Co-Author-/Claude-Zeile (Wunsch von Arvids). 746936f hat sie noch; Verlauf wird nicht umgeschrieben.
- Tests mit `unittest` + `aiohttp.test_utils` (keine neue Abhängigkeit). Aufruf: `python -m unittest discover -s tests`.
- `panel.py` wird nur mit `--apply` und `PANEL_ENABLED=1` importiert (lazy in `main()`); ohne das ändert sich nichts am Start.
- `main.py` übergibt alles in `PanelHooks` (Dataclass in `panel.py`); `panel.py` importiert `main.py` nie.
- Rechte-Stufe kommt aus `main.staff_level()` (Owner 3, Admin 2, Coach 1, sonst 0), geprüft bei jeder Anfrage über `guild.get_member`.
- Session-Cookie ist zustandslos signiert (Payload uid/sid/csrf/exp + HMAC-SHA256). Vorteil: Login überlebt einen Bot-Neustart. Abmelden sperrt die `sid` im Speicher bis zum Ablauf.
- Discord-Access-Token wird nur für `/users/@me` genutzt und danach sofort widerrufen, nirgends gespeichert.
- Access-Log von aiohttp ist aus, weil der OAuth-Callback `code` und `state` in der URL trägt.
- CSRF-Prüfung als Middleware für jede nicht-GET-Anfrage (Origin muss exakt `PANEL_BASE_URL` sein + Header `X-CSRF-Token`).
- `PANEL_BASE_URL` ohne Pfad; `http://` nur für localhost (SSH-Tunnel), sonst HTTPS. Secure-Cookie nur bei HTTPS.
- Rate-Limit nutzt `X-Forwarded-For` nur, wenn die Anfrage von 127.0.0.1/::1 kommt (Reverse-Proxy).
- `aiohttp==3.14.5` in `requirements.txt` gepinnt (lokal getestete Version, passt zu discord.py 2.7.1).
- `print()` → Panel über schmalen stdout-Wrapper (`ConsoleStdout`) statt Umstellung auf `log.info`: keine der ~120 `print`-Zeilen ändert sich, Terminal-/Journal-Ausgabe bleibt identisch (wichtig für B3), nur eine kleine Klasse. Wird nur mit aktivem Panel installiert und beim Beenden zurückgesetzt.
- Kein token-ähnlicher Text im Repo (GitHub-Push-Schutz hat einen Fake-Bot-Token im Test blockiert). Der Schwärzungstest für Bot-Tokens liest `DISCORD_BOT_TOKEN` aus der Umgebung oder aus `.env` (gitignored) und wird ohne Token übersprungen. Fehlermeldung gibt den Token nie aus. Der blockierte lokale Commit wurde per `git reset --soft` ersetzt (war nie gepusht).
- Log-Handler hängt an den Loggern `duality` und `discord`. Bei Fehlern geht nur „Typ: Text“ ans Panel, kein Stacktrace (der bleibt im Journal). Token-, Webhook-, Bearer- und HDEV-Muster werden vor dem Anzeigen geschwärzt.
- Logik aus den Loops in gemeinsame Funktionen gezogen: `sync_all_ranks`/`sync_ranks_now` (mit `RANK_SYNC_LOCK`), `purge_expired_data`, `send_due_event_reminders` (jetzt mit `REMINDER_LOCK`, gibt Anzahl zurück), `clean_up_temp_voice` gibt Anzahl zurück.
- Rechte der Befehle: Coach nur `help` (+ später `status`/`tasks`), Admin alle steuernden, Owner zusätzlich `restart`. `say` und `restart` brauchen Bestätigung, der Server lehnt sie ohne `confirm` ab.
- Steuernde Befehle: vor dem Ausführen Log-Eintrag + Audit-Eintrag „🖥️ Panel-Befehl von @Person: <Befehlszeile>“.
- `restart`: `client.close()`, Panel und HenrikDev-Client werden sauber beendet, Prozess endet mit Exit-Code 75 (`RESTART_EXIT_CODE`), damit systemd bei `Restart=always` und `Restart=on-failure` neu startet.
- Dashboard: Daten kommen beim Verbinden im `hello` und danach alle 5 s als `status` über denselben WebSocket (ein gemeinsamer Takt mit der Zugriffsprüfung). Uptime tickt im Browser.
- Task-Status über Dekorator `tracked(name)` in `main.py` (`LOOP_STATUS`: letzter Lauf, letzter Fehler); nächster Lauf aus `Loop.next_iteration`. Gateway-Zustand in `GATEWAY_STATE` über on_connect/on_disconnect/on_resumed/on_ready. Beides wird auch B1 nutzen.
- HenrikDev-Status direkt im `HenrikDevClient`: `failures_in_row`, `last_error(_at)`, `rate_limited_until`. 404 („Riot-ID nicht gefunden“) zählt nicht als API-Störung.
- Git-Commit wird beim Panel-Start einmal per `git rev-parse --short HEAD` gelesen (`create_subprocess_exec`, feste Argumente, keine Shell); ohne Git „unbekannt“.
- Echter Bot-Token liegt lokal in `.env` (gitignored, Rechte 600). Er wird nur vom Schwärzungstest gelesen. Den Bot starte ich damit nicht mit `--apply`, weil das eine zweite Instanz neben dem VPS wäre und das Setup auf dem echten Server ausführen würde.
- Offene Konsolen werden alle 5 s geprüft; bei Rollenentzug, Abmelden oder Session-Ablauf schließt der Server mit Code 4403, das Frontend lädt dann neu. Sonst verbindet es sich mit Backoff (1–15 s) neu und lädt den Puffer neu.

- **B1 Warnungen:** `Alerts` in `main.py`, läuft ohne Panel. Schlüssel je Problem (`task:<name>`, `gateway`, `henrikdev`, später `backup`). Gemeldet wird beim Auftreten und beim Verschwinden („wieder in Ordnung“), **keine** Wiederholung alle 30 min für dauerhafte Probleme (wäre bei täglichen Tasks Spam); kommt ein Problem innerhalb von 30 min nach der letzten Warnung wieder, wird es erst nach Ablauf gemeldet, und eine Entwarnung gibt es nur für verschickte Warnungen.
- Task „hängt“ = läuft, aber `next_iteration` > 5 min überfällig; „gestoppt“ = lief schon mal und läuft nicht mehr. HenrikDev ab 3 Fehlern in Folge. Gateway ab `ALERT_DISCONNECT_MINUTES` (5) getrennt.
- Versand: Webhook aus `ALERT_WEBHOOK_URL` (nur `https://discord.com/api/webhooks/…` u. ä., sonst ignoriert), `allowed_mentions: parse []`, Fehler ohne URL geloggt. Ohne Webhook oder wenn er scheitert: `#audit-log` über `audit()`, bei fehlender Verbindung in einer Warteschlange, die beim nächsten Verbinden **vor** neuen Meldungen nachgereicht wird.
- Prüf-Loop `check_alerts` (1 min) startet mit `--apply` schon vor dem Login, damit auch ein Bot, der nie verbindet, gemeldet wird; fängt eigene Fehler ab. Dashboard zeigt aktive Probleme und die letzten 10 Meldungen mit Zustellweg.

- **B2 Backups:** täglich 04:00 Uhr Berlin (`tasks.loop(time=…)`, damit Neustarts keine Extra-Backups erzeugen), Name `duality-backup-JJJJ-MM-TT_HHMMSS.sqlite3`, erst `.part` (600), dann `os.replace`; Ordner wird mit 700 angelegt. Aufräumen nach Datum im Namen, nur Dateien mit genau diesem Muster. Fehler → `BACKUP_STATE.last_error` → Warnung `backup`, bis ein Backup klappt; der Loop läuft weiter.
- Backup-Seite und Download nur Owner (Download aus der eigenen Liste, nie Pfad aus der Anfrage, Audit-Eintrag), „Jetzt sichern“ (POST) und Konsolenbefehl `backup` ab Admin. Dashboard zeigt letztes Backup für alle.
- Frontend: `api()` leitet nur bei 401 zur Startseite; 403 zeigt die Meldung (z. B. Admin auf Owner-Endpunkt). Beim Wiederverbinden führt 403 zur Startseite.
- Backup zurückspielen (für README, Phase 7): `systemctl stop duality-bot`, aktuelle DB wegsichern, Backup nach `/home/duality-bot/app/duality_bot_data.sqlite3` kopieren, `chown dualitybot:dualitybot` + `chmod 600`, `systemctl start duality-bot`.

- **B4 Voice:** `main.STATE_LISTENERS` + `notify_listeners(topic)`; `on_voice_state_update` meldet jetzt jede Voice-Änderung („voice“), Temp-Voice-Logik weiter nur bei Kanalwechsel. Panel trägt sich beim Start ein, entprellt 0,3 s und schickt nur, wenn sich die Momentaufnahme geändert hat; der 5-s-Takt fängt Kanal-Änderungen ohne Voice-Ereignis ab. Nur Anzeige, keine Moderation. Besitzer temporärer Kanäle aus `temp_voice_channels` (neu: `temp_voice_owners()`).

- **A3 Bewerbungen:** `/api/applications?status=…` (Coach+) aus `applications` + `application_vote_counts()` (neu), Namen/Avatare nur aus dem Cache (`Panel.person`), Personen ohne Mitgliedschaft als „Nicht mehr auf dem Server (ID)“. Link `discord.com/channels/<guild>/<#bewerbungen>/<message>`. „Neue Saison“ = `reopen_applications`, POST mit `{"confirm": true}` Pflicht, Admin+, Audit vor dem Ausführen. Bestätigungsdialog im Frontend ist jetzt allgemein (`confirmAction`), Elemente mit `data-level` werden für zu niedrige Stufen ausgeblendet.

- **A4 Spieler:** `/api/players?filter=players|all` (Coach+) aus `list_profiles()` (neu). Rolle in der Anzeige: Staff-Stufe, sonst Spieler/Bewerber/Mitglied, sonst „Nicht mehr auf dem Server“. Standard nur Spieler, Suche im Browser. „Rang abgleichen“ = `POST /api/players/<id>/sync-rank`, Admin+ (steuernd, wie in der Konsole), Audit, nutzt `sync_ranks_now`.

- **A5 Tickets:** `/api/tickets?state=open|closed|all` (Coach+) aus `list_tickets()` (neu), nur meldende Person, Zeiten und Thread-Link, keine Inhalte.
- Phase 2 im Browser mit Beispieldaten geprüft: Dashboard (Backups, Warnungen), Voice, Bewerbungen, Spieler (Rang-Knopf), Tickets, Backups; als Coach sind Backups, „Neue Saison“ und Rang-Knöpfe ausgeblendet.

- **Termine (gemeinsam):** `validate_event()` (Zukunft, Titel 1–100, Infos ≤ 1000), `publish_event()` (Embed, `EventRSVPView`, `save_scheduled_event`), `event_embed()`/`set_rsvp_fields()`/`RSVP_LABELS` statt doppeltem Code in `/termin` und `update_event_rsvps`. `scheduled_events` hat neue Spalten `title`, `details`, `cancelled_at` (per `ALTER TABLE` in `initialize_storage`, keine neue Tabelle, alte Zeilen bleiben). `save_scheduled_event` behält einen gespeicherten Titel; `update_scheduled_event` ändert ihn und löscht gesendete Erinnerungen nur bei neuer Uhrzeit. Titel alter Termine trägt `register_existing_scheduled_events` beim Start aus dem Embed nach. Abgesagte Termine: keine Erinnerungen, nicht in `attendance()`, Zu-/Absagen werden abgelehnt.

- **C2 Termine:** `/api/events?range=upcoming|past|all` mit Zu-/Absagen (Namen aus dem Cache) und gesendeten Erinnerungen; Anlegen/Bearbeiten/Absagen ab Coach über `validate_event`/`publish_event`/`edit_event`/`cancel_event` (main.py). Uhrzeit im Panel als `datetime-local`, Server liest sie als Berliner Zeit. `edit_event`: nur kommende, nicht abgesagte Termine; erst Discord-Embed (unter dem RSVP-Lock), dann DB; neue Uhrzeit → Erinnerungen zurückgesetzt; optional Nachricht an Zusagen, die nur diese erwähnt (`AllowedMentions(users=…)`). `cancel_event`: Embed „❌ Abgesagt“, Buttons deaktiviert, `cancelled_at` gesetzt, optional Zusagen benachrichtigen (auch bei Absage, nicht nur beim Verschieben). Audit bei Terminen erst nach Erfolg. `EventError` erbt von `ValueError`, das Panel fängt `ValueError` → 400 mit Meldung, `discord.HTTPException` → 502. Kalender: Monatsansicht ohne Bibliothek, zeigt alle Termine.

- **C1 Anwesenheit:** `attendance_events()` (stattgefundene, nicht abgesagte Termine im Zeitraum + Antworten) und `summarize_attendance()` (rein, getestet) in main.py. Zählt nur Termine ab dem Beitritt (wie `/profil`). Markiert bei Quote < `ATTENDANCE_THRESHOLD_PERCENT` (50) oder ≥ `ATTENDANCE_MISSING_LIMIT` (2) Terminen ohne Reaktion; beides wird in D3 einstellbar. Zeitraum 1–90 Tage (Löschfrist). Spieler = Mitglieder mit Rolle „Spieler“. CSV mit `;` und BOM für Excel, Namen gegen Formel-Injection geschützt (`spreadsheet_safe`), Export wird geloggt (kein Audit, da nicht steuernd).

- **C3 Glückwunsch:** `rank_value()` ('Gold 2' → (3, 2), Unrated → None), `is_rank_promotion()` nur bei höherer Hauptstufe. In `sync_member_rank` nach dem Speichern; Bedingung: vorheriger `rank_synced_at` vorhanden. Damit das „nicht beim ersten Abgleich nach dem Verknüpfen“ stimmt, setzen `/riot-id` und das Bewerbungsformular beim Verknüpfen `rank_tier`/`rank_synced_at` auf NULL. **Annahme:** Kanal `#team-chat` (`RANK_CONGRATS_CHANNEL`), nur Rolle „Spieler“, Erwähnung nur der Person. An/aus über `RANK_CONGRATS_ENABLED`, wird in D3 eine Einstellung. Sendefehler werden nur geloggt, der Abgleich läuft weiter.

- **C4 Rangliste:** neue Tabelle `rank_history` (guild_id, user_id, rank_tier, recorded_at, Index), Eintrag in `sync_member_rank` nur bei Rangänderung (`record_rank`). In `collect_user_data` („rangverlauf“) und `delete_user_data` aufgenommen, `/meine-daten`-Löschtext nennt den Rangverlauf. `rank_leaderboard()` gemeinsam für `/rangliste` (ephemeral wie `/team`, für alle) und `/api/ranking`: Spieler mit Riot-ID, Rang absteigend, ohne Abgleich am Ende, gleicher Rang nach Name; Name aus `player_roster_names` wie `/team`. Panel zeigt den Verlauf als SVG-Linie ohne Bibliothek. `/help` nennt `/rangliste` (Feldlänge ≤ 1024 per Test geprüft).
- Phase 3 im Browser mit Beispieldaten geprüft: Termine (Liste, Kalender, Anlegen, Bearbeiten-Dialog, Absage-Bestätigung), Anwesenheit (Sortierung, Markierung), Rangliste (Verlauf).

- **D3 Einstellungen:** `SETTING_DEFINITIONS` (Typ int/bool/hours, Bereich, Beschreibung) in main.py, Tabelle `bot_settings` (key, value JSON, updated_by, updated_at). `setting(key)` liest mit Cache, `save_setting`/`reset_setting` leeren ihn → wirkt sofort. Ungültige gespeicherte Werte → Standard + Warnung im Log. Einstellbar: Stimmen für Bewerbungen, Löschfristen Bewerbungen/Termine, Erinnerungszeitpunkte (Stunden, max. 4), Pause beim Rang-Abgleich, Rang-Glückwunsch an/aus, Anwesenheits-Schwelle und -Limit, Warnung nach Minuten ohne Verbindung und nach HenrikDev-Fehlern. `ALERT_DISCONNECT_MINUTES` ist jetzt nur noch der Standard, ein gespeicherter Wert geht vor. Erinnerungen allgemein über `due_reminder()` (Fenster (h−1, h] Stunden, Schlüssel `<h>h`, kompatibel zu `24h`/`1h`). Panel-Seite ab Admin, Audit mit altem → neuem Wert. Tests setzen `SETTINGS_CACHE = {}` (nur Standardwerte), damit nie die echte DB gelesen wird.

- **D2 Texte:** `TEXT_DEFINITIONS` (Willkommen, Regeln, Bewerbungs-Infotext, DM Annahme/Ablehnung, /help in drei Feldern) mit den bisherigen Texten als Standard (per Skript verschoben und gegen die alten Ausdrücke geprüft: wortgleich). Grenzen 4096/2000/1024, geprüft inkl. eingesetzter Platzhalter. Platzhalter `{loeschfrist}` im Bewerbungstext = Einstellung `application_retention_days`. Tabelle `bot_texts` mit Verlauf: neuester Eintrag je Schlüssel gilt, `value NULL` = Standard (Zurücksetzen bleibt im Verlauf sichtbar), höchstens 20 Fassungen je Text. `bot_text(key)` mit Cache. Gemeinsame Embed-Bauer `welcome_/rules_/application_message_embed()` für `setup_guild` und Panel; `refresh_text_message()` bearbeitet die feste Nachricht (nur `embed=…`, Buttons/custom_ids bleiben). Panel-Seite ab Admin mit vereinfachter Discord-Vorschau, Zeichenzähler, Verlauf („In den Editor laden“), Audit ohne Textinhalt. Hinweis: Eine geänderte Löschfrist erscheint im #bewerbung-Text erst beim nächsten Speichern des Texts oder Neustart.

- **D1 Ankündigungen:** `validate_announcement()` (Titel 256, Text 4096, Fußzeile 2048, ≤ 10 Felder à 256/1024, gesamt ≤ 6000, Farbe #RRGGBB, Bilder nur https), `announcement_embed()`, `send_announcement()` (nur Kanäle mit view/send/embed_links, Erwähnung höchstens der gewählten Rolle aus `ANNOUNCEMENT_PING_ROLES` = Ping Valorant/Ping Events, nie @everyone/@here), `edit_sent_announcement()` (nur Embed, Kanal und Ping bleiben, kein neuer Ping). Tabelle `scheduled_announcements` (sofort gesendete werden auch gespeichert, damit man sie später bearbeiten kann; `sent_at NULL` = offen). Minuten-Task `announcements` sendet fällige; Fehler bleiben als `error` stehen statt minütlicher Wiederholung, Bearbeiten löscht den Fehler. Panel-Seite ab Admin mit Live-Vorschau, Bestätigung vor „sofort senden“, Liste mit Bearbeiten/Löschen (nur ungesendete). CSP `img-src` erlaubt jetzt `https:` für die Bildvorschau.

- **E1 Datenschutz:** `build_data_export()` gemeinsam für `/meine-daten` und Panel (inkl. Angaben aus der Bewerbungsnachricht, ohne sie zu speichern). `collect_user_data` um `geaenderte_einstellungen`, `geaenderte_texte`, `ankuendigungen` erweitert (`rangverlauf` seit C4); `delete_user_data` anonymisiert dort `updated_by`/`created_by` (Inhalte gehören dem Server). Panel ab Admin: Suche nach Name/Nutzername oder User-ID (auch Ausgetretene), Auskunft ansehen/als JSON laden, Löschung mit Pflichtfeld „Grund / Anfrage vom“ (3–300 Zeichen) + Bestätigung; scheitert das Löschen der Bewerbungsnachrichten, wird nichts weiter gelöscht. Audit: nur dass, wer, für welche ID, bei Löschung mit Grund – nie Daten. Nach der Löschung wird die Auskunft nicht neu geladen (sonst zweiter Audit-Eintrag). Hinweis in Panel und `/meine-daten`: Daten in Backups verschwinden erst mit deren Aufbewahrungsfrist. **Für Phase 6:** `scrim_requests` und Scrim-Börse-Kontakte dann ebenfalls in Auskunft/Löschung prüfen.
- Handy-Ansicht korrigiert: Mit 16 Menüpunkten zog die Navigationsleiste die Seite auf 1320 px; jetzt `minmax(0, 1fr)`, die Leiste scrollt seitlich und der aktive Punkt wird sichtbar gescrollt.
- Phase 4 im Browser geprüft (Wegwerf-DB, Discord simuliert): Einstellungen (inkl. abgelehnter Eingabe), Texte (Speichern, Platzhalter, Verlauf), Ankündigungen (Vorschau, Planen, Bearbeiten, Löschen), Datenschutz (Suche, Auskunft), Handy-Layout.

- **B3 Setup-Diff:** `setup_guild(guild, apply, echo=True)` gibt eine Liste `SetupChange(action create/change/delete/info, kind, target, text)` zurück. Alle `print` laufen über `SetupPlan.say/note`; die Terminal-Ausgabe ist zeichengleich (Vergleich alt/neu mit nachgebauten Servern in 8 Läufen, Skript im Scratchpad). Zusätzlich nur im Diff (ohne Ausgabe): einzelne Löschungen in alten Kategorien, Rechte-Änderungen an Kanälen in der Vorschau, feste Nachrichten (veröffentlichen/aktualisieren), alte Bewerbungsnachricht in #willkommen. Eine nur umbenannte Kategorie erscheint nicht zusätzlich als „neu“ (bekannte Restungenauigkeit: `bewerber-chat` erscheint als neu, obwohl aus `looking-for-team` umbenannt). `SETUP_LOCK` + `run_setup()` für den Start; das Panel hält den Lock selbst über Vorschau + Anwenden. Panel: Vorschau ab Admin (`echo=False`, damit die Konsole sauber bleibt), Anwenden nur Owner mit `{"confirm": true, "deletions": n}`; der Server rechnet die Vorschau unter dem Lock neu und bricht mit 409 ab, wenn sich die Zahl der Löschungen geändert hat. Beim Anwenden `echo=True` (Ausgabe in der Konsole), Audit mit Zahlen, Fehler → 502 + Audit „brach ab“. Nach dem Anwenden bleiben immer die 4 Nachrichten-Aktualisierungen übrig (so wie bei jedem Start).

## VPS (Stand 21.10.2026)
- Ubuntu 24.04, Dienst `duality-bot` läuft als `dualitybot`, `Restart=on-failure` (passt zu Exit-Code 75), Env in `/etc/duality-bot.env` (root, 600).
- Python 3.14.8, discord.py 2.7.1, aiohttp 3.14.5.
- **Port 8080 belegt** (YouTrack-Docker) → Panel auf `PANEL_PORT=8090`, `PANEL_BASE_URL=http://localhost:8090`, Zugriff per `ssh -L 8090:127.0.0.1:8090 root@217.154.173.183`.
- nginx läuft auf 80/443 (für späteren HTTPS-Proxy), ufw ist aus, YouTrack auf 8080 öffentlich (nicht von mir angefasst).
- Panel-Einträge in `/etc/duality-bot.env` gesetzt (Sicherung `/etc/duality-bot.env.bak-20261021-114849`), `PANEL_SECRET_KEY` auf dem Server erzeugt, `DISCORD_CLIENT_ID`/`DISCORD_CLIENT_SECRET` per stdin übertragen.
- Code auf ee3a763 deployt, Bot läuft, Panel lauscht auf 127.0.0.1:8090, `/healthz` → 200.
- **Öffentlich:** https://duality.217-154-173-183.sslip.io (vorläufig, bis es eine eigene Domain gibt). nginx-Seite `/etc/nginx/sites-available/duality.217-154-173-183.sslip.io`: Port 80 → 301 HTTPS, 443 → `127.0.0.1:8090` mit WebSocket-Headern, `X-Forwarded-For $remote_addr`, Zugriffslog ohne `/auth/callback`. Zertifikat per `certbot certonly --nginx` (läuft bis 19.01.2027, certbot.timer erneuert). `PANEL_BASE_URL=https://duality.217-154-173-183.sslip.io` (Sicherung `/etc/duality-bot.env.bak-20261021-121043`). Redirect-URI im Developer Portal: `https://duality.217-154-173-183.sslip.io/auth/callback`.
- Wechsel auf eigene Domain später: A-Record → 217.154.173.183, nginx-Seite kopieren/umbenennen, `certbot certonly --nginx -d <domain>`, `PANEL_BASE_URL` ändern, Redirect-URI ergänzen, Bot neu starten.
- Arvids hat Root-Passwort und Client-Secret im Chat gepostet → Empfehlung: Root-Passwort ändern, Secret später neu erzeugen und nur über `.env` geben. `update-vps.sh` scheiterte beim ersten Lauf aus `/root` an `uv` (Leserechte) → behoben, das Skript wechselt jetzt in `$APP_DIR`.
- Alte root-eigene Kopie `/home/duality-bot/main.py` liegt ungenutzt herum (nicht angefasst).
- **21.10.2026, 12:29 UTC: Phase 2 deployt (a770e37)**, `update-vps.sh` Exit 0, Dienst aktiv, keine Fehler im Log, `/healthz` lokal und öffentlich 200, API ohne Login 401. Test-Backup als `dualitybot` erstellt (`/home/duality-bot/app/backups/`, Ordner 700, Datei 600), `git status` sauber. Kein `ALERT_WEBHOOK_URL` gesetzt → Warnungen nach `#audit-log`.
- **21.10.2026, 12:52 UTC: Phase 3 deployt (aa72e6d)**, vorher Backup `duality-backup-2026-10-21_145223.sqlite3`. `update-vps.sh` Exit 0, Dienst aktiv, 0 Fehler im Log, Schema migriert (`scheduled_events` + `title`/`details`/`cancelled_at`, `rank_history` da), `/rangliste` bei Discord registriert, Panel lokal/öffentlich ok. Hinweise: ein verknüpftes Profil gehört zu jemandem, der den Server verlassen hat (wird beim Abgleich übersprungen); der gespeicherte Termin am 24.10. 17:00 hat eine in Discord gelöschte Nachricht (kein Titel, verschwindet beim Erinnerungs-Check 24 h vorher von selbst).
- **21.10.2026, 13:12 UTC: Phase 4 deployt (e5499f4)**, vorher Backup `duality-backup-2026-10-21_151242.sqlite3`. `update-vps.sh` Exit 0, Dienst aktiv, 0 Fehler, neue Tabellen da, Standardwerte aktiv, feste Nachrichten mit den (unveränderten) Standardtexten neu gesetzt, Panel lokal/öffentlich ok.
- **21.10.2026, 13:21 UTC: Phase 5 deployt (7f87f7d)**, vorher Backup `duality-backup-2026-10-21_152155.sqlite3`. Exit 0, Dienst aktiv, 0 Fehler, Panel ok. Setup-Vorschau gegen den echten Server noch nicht angesehen (geht nur über das Panel).
- **21.10.2026, 15:06 UTC: Abschluss deployt (f24331b)**, vorher Backup `duality-backup-2026-10-21_170647.sqlite3`. Exit 0, Dienst aktiv, 0 Fehler, Syntaxprüfung in `update-vps.sh` vorhanden, `git status` sauber, Panel lokal/öffentlich ok.
- **21.10.2026, 15:19 UTC: Design-Update deployt (c827c7f)**, vorher Backup `duality-backup-2026-10-21_171937.sqlite3`. Exit 0, Dienst aktiv, 0 Neustarts, 0 Fehler, `git status` sauber, Panel lokal/öffentlich ok, neue Dateien `daisyui.css` und `theme.js` werden ausgeliefert.
- **21.10.2026, 15:21 UTC: gzip für `/static/` in nginx** (nur Duality-Server-Block, API bleibt unkomprimiert wegen BREACH), vorher Sicherung `/root/nginx-duality-2026-10-21_152111.conf`. `nginx -t` ok, neu geladen; `daisyui.css` jetzt 29 KB statt 437 KB, Sicherheits-Header unverändert, andere Seiten auf dem VPS unberührt (`map.cbs-minecraft.de` gab schon vorher 502). README-Beispiel angepasst.
- **21.10.2026, 15:30 UTC: Navigation in Kategorien deployt (241b998)**, vorher Backup `duality-backup-2026-10-21_173025.sqlite3`. Exit 0, Dienst aktiv, 0 Neustarts, 0 Fehler, `git status` sauber, Panel lokal/öffentlich ok, neue Navigation (5 Gruppen) wird ausgeliefert, gzip weiter aktiv.
- **21.10.2026, 16:02 UTC: Design nach Variant-Entwurf deployt (46e9a8a)**, vorher Backup `duality-backup-2026-10-21_180201.sqlite3`. Exit 0, Dienst aktiv, 0 Neustarts, 0 Fehler, `git status` sauber, Panel lokal/öffentlich ok, neuer Stil (Theme-Knopf, dunkle Seitenleiste, Überzeilen) wird ausgeliefert, gzip aktiv.
- **21.10.2026, 16:07 UTC: Duality-Logo deployt (b169451)**, vorher Backup `duality-backup-2026-10-21_180709.sqlite3`. Exit 0, Dienst aktiv, 0 Neustarts, 0 Fehler, `git status` sauber, Panel lokal/öffentlich ok, Logo auf Anmeldeseite und im Panel, Favicon wird als `image/svg+xml` ausgeliefert.
- **21.10.2026, 16:38 UTC: Statistiken + Server-Wachstum deployt (e17879c)**, vorher Backup `duality-backup-2026-10-21_183845.sqlite3`. Exit 0, Dienst aktiv, 0 Neustarts, 0 Fehler, `git status` sauber, Panel lokal/öffentlich ok, neue Endpunkte ohne Login 401. Erster Match-Abruf beim Start: 2 Spieler mit je 20 Competitive-Matches (der dritte verknüpfte Account gehört jemandem, der den Server verlassen hat, wird übersprungen), Werte plausibel (gesamt K/D 1,11, HS 28,4 %, ACS 243, ADR 162). Wachstum: Zählbeginn 21.10. mit 9 Mitgliedern, 6 Beitritte vom 20.10. nachgetragen.

## Design-Update (Wunsch von Arvids, 21.10.2026)
- [x] Panel in den Farben der Palette (`palette.ase`: `#eb1b29`, Schwarz, Weiß), Themes `duality-dark`/`duality-light`, Umschalter System/Hell/Dunkel in der Seitenleiste (Wahl nur im Browser gespeichert, `panel_static/theme.js`) — fertig (c827c7f)
- Statt shadcn (braucht React + Build, verboten) auf Wunsch daisyUI 5.7.50: nur die genutzten Komponenten als `panel_static/daisyui.css`, unverändert von jsDelivr zusammengefügt, ohne Tailwind und ohne Build. Eigene Klassen nur für Layout und Discord-Vorschau.
- Kontrast: Primärrot `#eb1b29` mit weißer Schrift hat 4,45:1 (knapp unter 4,5); roter Text nutzt daher den Akzent (`#ff4d58` dunkel, `#c8101c` hell). Fehler-Knöpfe sind umrandet, damit sie sich vom roten Primärknopf abheben.
- Standardfarbe neuer Ankündigungen ist jetzt `#eb1b29`.
- [x] Navigation in Kategorien: Übersicht (Dashboard, Voice), Team (Termine, Anwesenheit, Rangliste, Spieler), Community (Bewerbungen, Tickets, Ankündigungen), Inhalte & Regeln (Texte, Einstellungen, Datenschutz), System (Konsole, Server-Setup, Backups). Gruppen ohne erlaubte Seite werden ausgeblendet; auf dem Handy eine Zeile mit Trennern statt Überschriften — fertig (241b998)
- [x] Stil nach Arvids' Variant-Entwurf: schwarze Seitenleiste (auch im hellen Theme), eckige Flächen, Seitentitel in Großbuchstaben mit rotem „//“ und Überzeile aus der Navigationsgruppe, Kartenköpfe mit rotem Strich, Daten/Tabellen/Felder in Monospace, gefüllte Status-Badges, Punktraster im Hintergrund, Theme-Knopf (System → Hell → Dunkel). Deutsche Texte und echte Funktionen bleiben; Beispielinhalte aus dem Entwurf (Annehmen/Ablehnen, Auto-Moderation, Reset) bewusst nicht übernommen — fertig (46e9a8a)
- [x] Duality-Logo (DM-Zeichen aus `Duality-DM.svg`/`Duality-DM-LightBG.svg`) statt Raute: als Inline-SVG in Seitenleiste und Anmeldeseite, linker Teil folgt der Textfarbe (weiß auf dunkel, schwarz auf hell), rechter Teil rot; dazu `favicon.svg` auf schwarzem Feld — fertig (b169451)

## Neue Features (Wunsch von Arvids, 21.10.2026)
- [x] Spieler-Statistiken: HenrikDev `stored-matches` beim täglichen Rang-Abgleich, Tabelle `match_stats` (90 Tage, in Auskunft/Löschung, neue Riot-ID löscht alte Matches), Seite „Statistiken“ (Team-Tabelle sortierbar, Detail mit Kennzahlen, Agents, Maps, letzten Matches), „Bewerber prüfen“ ohne Speichern (6/min je Staff), „Jetzt aktualisieren“ ab Admin, Einstellungen `stats_match_count` (20) und `stats_only_competitive` (an) — fertig (6f4919e)
- [x] Server-Wachstum: Tabelle `member_flow` nur mit Tageszahlen (keine User-IDs, 400 Tage), Zählung bei Beitritt/Abgang, beim Start Mitgliederzahl + Nachtrag der Beitritte vor Zählbeginn aus `joined_at`; Dashboard-Karte mit Kacheln und SVG-Balkendiagramm (12 Wochen, Beitritte grün, Abgänge rot) — fertig (e17879c)
- [x] Bewerbung, Schritt 2: Spielzeiten als Button-Raster statt Dropdowns (Zeilen = Stundenblöcke 19–20, 20–21, 21–22, 22–23 ohne Überlappung, Spalten = Mi–So, grün = gewählt, letzte Zeile Zurück/Abschicken); zusammenhängende Stunden erscheinen in der Bewerbung als ein Zeitraum (z. B. „Mi · 19:00–22:00“). Standardtext in #bewerbung angepasst (ein im Panel geänderter Text bleibt) — fertig (Commit folgt)

## Neue Env-Variablen
| Variable | Standard | Zweck |
|---|---|---|
| `PANEL_ENABLED` | – | `1` schaltet das Panel ein (nur mit `--apply`) |
| `PANEL_HOST` | `127.0.0.1` | Bind-Adresse |
| `PANEL_PORT` | `8080` | Port |
| `PANEL_BASE_URL` | – | Öffentliche Adresse ohne Pfad, Redirect-URI = `<PANEL_BASE_URL>/auth/callback` |
| `PANEL_SECRET_KEY` | – | ≥ 32 Zeichen, signiert Session-Cookies |
| `DISCORD_CLIENT_ID` | – | OAuth2-Client-ID der Discord-Application |
| `DISCORD_CLIENT_SECRET` | – | OAuth2-Client-Secret |
| `ALERT_WEBHOOK_URL` | – | Discord-Webhook für Warnungen; ohne: `#audit-log` |
| `ALERT_DISCONNECT_MINUTES` | `5` | Ab so vielen Minuten ohne Gateway gibt es eine Warnung |
| `BACKUP_DIR` | `backups/` neben `main.py` | Zielordner der Backups (gitignored) |
| `BACKUP_RETENTION_DAYS` | `14` | Ältere Backups werden gelöscht |

## Neue Tabellen und Spalten
- `match_stats` (Statistiken), `member_flow` (Server-Wachstum)
- `rank_history` (C4)
- `bot_settings` (D3)
- `bot_texts` (D2, mit Verlauf)
- `scheduled_announcements` (D1)
- `scheduled_events`: neue Spalten `title`, `details`, `cancelled_at` (per `ALTER TABLE`, C2)

## Offene Fragen an Arvids
- Soll ich für `ALERT_WEBHOOK_URL` einen Webhook in einem Staff-Kanal anlegen (z. B. `#staff-chat` oder `#audit-log`), oder legt Arvids ihn selbst an? Ohne Webhook gehen Warnungen nach `#audit-log`, aber nur, solange der Bot verbunden ist.

## Bekannte Probleme
- Lokal (Mac, python.org-Python) schlägt die Verbindung zu discord.com mit `CERTIFICATE_VERIFY_FAILED` fehl; betrifft nur Testläufe hier, nicht den VPS. Echter Discord-Login lokal daher nicht getestet.

## Nächster Schritt
Phase 2 läuft (OK von Arvids, Login über https://duality.217-154-173-183.sslip.io klappt). Phase 3 läuft (OK von Arvids). Phase 4 läuft (OK von Arvids). Alle Phasen außer Phase 6 (zurückgestellt) sind fertig. VPS läuft mit dem Abschluss-Stand (f24331b). Mögliche nächste Schritte: eigene Domain statt sslip.io, `ALERT_WEBHOOK_URL` setzen, Scrim-Suche später mit PRACC.

## Abschluss (Stand 21.10.2026)
- **Geänderte/neue Dateien:** `main.py`, `panel.py` (neu), `panel_static/` (neu: index.html, panel.css, panel.js), `tests/` (neu: test_panel.py, test_bot.py, test_setup.py), `requirements.txt` (+ aiohttp), `.gitignore` (+ backups/), `update-mac.sh`, `update-vps.sh`, `README.md`, `PROGRESS.md`, `PANEL_PROMPT.md`.
- **Neue Tabellen/Spalten:** `rank_history`, `bot_settings`, `bot_texts`, `scheduled_announcements`; `scheduled_events` + `title`, `details`, `cancelled_at`.
- **Neue Env-Variablen:** siehe Tabelle oben bzw. README.
- **Nicht getestet (nur mit simuliertem Discord):** Termin bearbeiten/absagen an echten Nachrichten, Rang-Glückwunsch im echten Teamchat, Ankündigung senden, Text-Änderung an fester Nachricht, Bewerbungsnachrichten löschen (Datenschutz), Setup-Vorschau/-Anwenden am echten Server, Warnung per Webhook, Panel-`restart` mit systemd. Echt getestet: Login über Discord, Bot-Start/Deploy, Backups auf dem VPS, Slash-Befehle registriert.
- **Bekannte Einschränkungen:** Diff zeigt `bewerber-chat` bei sehr alten Servern als neu statt umbenannt; geänderte Bewerbungs-Löschfrist erscheint im #bewerbung-Text erst beim nächsten Speichern/Neustart; Panel unter sslip.io hängt von fremdem DNS ab; der gelöschte Termin am 24.10. verschwindet erst beim Erinnerungs-Check. Phase 4 beginnt mit D3 Einstellungen (macht u. a. `RANK_CONGRATS_ENABLED` und Anwesenheits-Schwellen einstellbar). Phase 3 beginnt mit der gemeinsamen Termin-Funktion für `/termin` und Panel (C2). Dann: B1 Fehler-Benachrichtigung (nutzt `LOOP_STATUS`, `GATEWAY_STATE`, `HenrikDevClient.failures_in_row`).
