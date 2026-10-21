# Fortschritt: Web-Panel und neue Features

Gedächtnis für die Arbeit nach `PANEL_PROMPT.md`. Vor jeder Arbeit lesen, nach jedem Feature aktualisieren.
Status: `offen` · `in Arbeit` · `fertig (<Commit>)`

## Phasen und Features

### Vorbereitung
- [x] `update-mac.sh`: PROGRESS.md, PANEL_PROMPT.md, README.md in `FILES`, Prüfung `-f` → `-e` — fertig (siehe Commit „Fortschrittsdatei und Update-Skript für Ordner“)

### Phase 1 – Grundgerüst (A1, A2)
- [ ] aiohttp-Server in derselben Event-Loop + Healthcheck — offen
- [ ] Discord-OAuth2-Login (Scope `identify`, `state`-Prüfung) — offen
- [ ] Signierte Session-Cookies (HMAC-SHA256, 12 h, HttpOnly/Secure/SameSite=Lax) — offen
- [ ] Rechte-Stufen Coach/Admin/Owner, live bei jeder Anfrage — offen
- [ ] CSRF-Schutz (Token + Origin) für POST und WebSocket — offen
- [ ] Rate-Limit für Login und Konsolenbefehle — offen
- [ ] Log-Handler mit Ringpuffer + `print`-Ausgaben im Panel — offen
- [ ] WebSocket-Konsole mit Whitelist-Befehlen (A2) — offen
- [ ] Dashboard (A1) — offen
- [ ] Tests ohne Discord-Verbindung (Session, CSRF, Parser, Rechte) — offen

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
- Tests mit `unittest` aus der Standardbibliothek (keine neue Abhängigkeit wie pytest). Aufruf: `python -m unittest discover -s tests`.

## Neue Env-Variablen
_(noch keine)_

## Neue Tabellen
_(noch keine)_

## Offene Fragen an Arvids
_(noch keine)_

## Bekannte Probleme
_(noch keine)_

## Nächster Schritt
Phase 1 beginnen: `panel.py` mit aiohttp-Server, Healthcheck, OAuth-Login, Sessions, Rechten und CSRF.
