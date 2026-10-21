# Fortschritt: Web-Panel und neue Features

Gedächtnis für die Arbeit nach `PANEL_PROMPT.md`. Vor jeder Arbeit lesen, nach jedem Feature aktualisieren.
Status: `offen` · `in Arbeit` · `fertig (<Commit>)`

## Phasen und Features

### Vorbereitung
- [x] `update-mac.sh`: PROGRESS.md, PANEL_PROMPT.md, README.md in `FILES`, Prüfung `-f` → `-e` — fertig (746936f)

### Phase 1 – Grundgerüst (A1, A2)
- [x] aiohttp-Server in derselben Event-Loop + Healthcheck `/healthz` — fertig (Commit „Panel: Grundgerüst …“)
- [x] Discord-OAuth2-Login (Scope `identify`, `state`-Prüfung) — fertig (dto.)
- [x] Signierte Session-Cookies (HMAC-SHA256, 12 h, HttpOnly/Secure/SameSite=Lax) — fertig (dto.)
- [x] Rechte-Stufen Coach/Admin/Owner, live bei jeder Anfrage — fertig (dto.)
- [x] CSRF-Schutz (Token + Origin) für POST — fertig (dto.); WebSocket folgt mit der Konsole
- [x] Rate-Limit Login (10 Versuche / 5 min pro IP) — fertig (dto.); Konsolenbefehle folgen mit der Konsole
- [ ] Log-Handler mit Ringpuffer + `print`-Ausgaben im Panel — offen
- [ ] WebSocket-Konsole mit Whitelist-Befehlen (A2), CSRF + Rate-Limit für WS — offen
- [ ] Dashboard (A1) — offen
- [x] Tests ohne Discord-Verbindung: Session, CSRF, Rate-Limit, Konfiguration, Rechte, HTTP-Endpunkte — fertig (dto.); Parser-Tests folgen mit der Konsole

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
_(noch keine)_

## Bekannte Probleme
- Lokal (Mac, python.org-Python) schlägt die Verbindung zu discord.com mit `CERTIFICATE_VERIFY_FAILED` fehl; betrifft nur Testläufe hier, nicht den VPS. Echter Discord-Login lokal daher nicht getestet.

## Nächster Schritt
Phase 1: Live-Konsole (Log-Handler + Ringpuffer, stdout-Tee, WebSocket `/ws` mit CSRF, Whitelist-Befehle, Rate-Limit).
