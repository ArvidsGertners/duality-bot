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
- [x] A5 Tickets — fertig (Commit „Panel: Tickets …“)

### Phase 3 – Team
- [ ] Gemeinsame Termin-Funktion für `/termin` und Panel — offen
- [ ] C2 Termine im Panel (anlegen, bearbeiten/verschieben, absagen) — offen
- [ ] C1 Anwesenheitsübersicht + CSV — offen
- [ ] C3 Glückwunsch bei Rangaufstieg — offen
- [ ] C4 `/rangliste` + Panel-Ansicht + `rank_history` — offen

### Phase 4 – Konfiguration & Kommunikation
- [ ] D3 Einstellungen (`bot_settings`, `setting()`-Helfer mit Cache) — offen
- [ ] D2 Texte bearbeiten (`bot_texts`, Verlauf, Discord-Nachrichten aktualisieren) — offen
- [ ] D1 Ankündigungs-Editor (sofort/zeitgesteuert, `scheduled_announcements`) — offen
- [ ] E1 Datenschutz-Seite (Auskunft/Löschung) — offen

### Phase 5 – Setup-Diff
- [ ] B3 `setup_guild` liefert strukturierten Diff, Panel-Seite, Anwenden mit Lock — offen

### Phase 6 – Scrim-Suche
- [ ] Recherche-Tabelle vorlegen und auf Entscheidung warten — offen
- [ ] F Scrim-Börse, eigenes Gesuch, `scrim_requests`, Termin aus Scrim, Einladungslink — offen

### Phase 7 – Abschluss
- [ ] README (Web-Panel, Env-Variablen, systemd, Caddy, SSH-Tunnel, Backup zurückspielen, Webhook) — offen
- [ ] Update-Skripte prüfen — offen
- [ ] `/help` um neue Befehle ergänzen — offen

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

## Neue Tabellen
_(noch keine)_

## Offene Fragen an Arvids
- Soll ich für `ALERT_WEBHOOK_URL` einen Webhook in einem Staff-Kanal anlegen (z. B. `#staff-chat` oder `#audit-log`), oder legt Arvids ihn selbst an? Ohne Webhook gehen Warnungen nach `#audit-log`, aber nur, solange der Bot verbunden ist.

## Bekannte Probleme
- Lokal (Mac, python.org-Python) schlägt die Verbindung zu discord.com mit `CERTIFICATE_VERIFY_FAILED` fehl; betrifft nur Testläufe hier, nicht den VPS. Echter Discord-Login lokal daher nicht getestet.

## Nächster Schritt
Phase 2 läuft (OK von Arvids, Login über https://duality.217-154-173-183.sslip.io klappt). Phase 2 ist fertig. Auf Arvids' OK für Phase 3 warten; VPS-Update vorschlagen (vorher fragen). Phase 3 beginnt mit der gemeinsamen Termin-Funktion für `/termin` und Panel (C2). Dann: B1 Fehler-Benachrichtigung (nutzt `LOOP_STATUS`, `GATEWAY_STATE`, `HenrikDevClient.failures_in_row`).
