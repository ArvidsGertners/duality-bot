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
- [x] CSRF-Schutz (Token + Origin) für POST (4a8fd5a) und WebSocket (Konsolen-Commit)
- [x] Rate-Limit Login 10/5 min pro IP (4a8fd5a), Konsolenbefehle 20/min pro Person (Konsolen-Commit)
- [x] Log-Handler mit Ringpuffer (1000) + `print`-Ausgaben im Panel — fertig (Commit „Panel: Live-Konsole …“)
- [x] WebSocket-Konsole `/ws` mit Whitelist-Befehlen help, sync-ranks, reminders, purge, cleanup-voice, say, restart — fertig (dto.); `status`/`tasks` kommen mit dem Dashboard, `backup` mit B2
- [ ] Dashboard (A1) — offen
- [x] Tests ohne Discord-Verbindung: Session, CSRF, Rate-Limit, Konfiguration, Rechte, HTTP-Endpunkte, Parser, Log-Puffer, WebSocket-Befehle (47 Tests)

### Phase 2 – Betrieb
- [ ] B1 Fehler-Benachrichtigung (Webhook, Entprellung, „wieder in Ordnung“) — offen
- [ ] B2 Datenbank-Backups (SQLite-Backup-API, Aufbewahrung, Download für Owner, Konsolenbefehl `backup`) — offen
- [ ] B4 Live-Ansicht der Voice-Kanäle — offen
- [ ] A3 Bewerbungen lesen + „Neue Saison“ — offen
- [ ] A4 Spieler & Profile + „Rang jetzt abgleichen“ — offen
- [ ] A5 Tickets — offen

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
- Offene Konsolen werden alle 5 s geprüft; bei Rollenentzug, Abmelden oder Session-Ablauf schließt der Server mit Code 4403, das Frontend lädt dann neu. Sonst verbindet es sich mit Backoff (1–15 s) neu und lädt den Puffer neu.

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

## Neue Tabellen
_(noch keine)_

## Offene Fragen an Arvids
- Wie ist `Restart=` in der systemd-Unit `duality-bot` gesetzt? Der Neustart aus dem Panel braucht `Restart=always` oder `Restart=on-failure` (Exit-Code 75).

## Bekannte Probleme
- Lokal (Mac, python.org-Python) schlägt die Verbindung zu discord.com mit `CERTIFICATE_VERIFY_FAILED` fehl; betrifft nur Testläufe hier, nicht den VPS. Echter Discord-Login lokal daher nicht getestet.

## Nächster Schritt
Phase 1: Dashboard (A1) mit Task-Status (letzter/nächster Lauf, letzter Fehler), Gateway-Status, HenrikDev-Status, Live-Update per WebSocket; Befehle `status` und `tasks`.
