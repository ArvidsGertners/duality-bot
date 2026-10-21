# Prompt: Web-Panel-Konsole und neue Features für den Duality-Bot

> Diesen Text komplett als Aufgabe an Claude Code (oder ein anderes Coding-Modell) geben, im Repo `duality-bot`.

---

## Kontext

Du arbeitest am **Duality-Bot**, dem Discord-Bot von Duality Mannheim (Valorant-Hochschulteam der DHBW Mannheim). Das Projekt ist bewusst klein gehalten:

- **Eine Datei:** `main.py` (~3.250 Zeilen), Python 3.14, `discord.py==2.7.1` (bringt `aiohttp` mit). Keine weiteren Abhängigkeiten.
- **Start:** `python main.py --apply` hält den Bot online; ohne `--apply` läuft nur eine Vorschau und der Bot beendet sich.
- **Konfiguration nur über Umgebungsvariablen:** `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID`, `HENRIKDEV_API_KEY`. Niemals Secrets im Code.
- **Daten:** SQLite in `duality_bot_data.sqlite3` neben `main.py`, Zugriff über `open_db()`, Schema in `initialize_storage()`. Tabellen: `applications`, `application_votes`, `player_profiles`, `scheduled_events`, `event_rsvps`, `event_reminders_sent`, `tickets`, `temp_voice_channels`, `player_roster_names`, `managed_ids`, `setup_migrations`.
- **Hintergrund-Tasks** in `main()`: `sync_ranks` (24 h), `purge_declined_applications` (24 h), `event_reminders` (1 min).
- **Rollen:** `STAFF_ROLES = ("Owner", "Admin", "Coach")`, Helfer `is_staff(member)`. Rangrollen aus `VALORANT_RANKS` (Reihenfolge = Rangfolge). Audit-Log-Kanal über `audit(guild, text, color)`.
- **Vorhandene Bausteine, die du wiederverwenden sollst:**
  - Ränge: `HenrikDevClient` / `RANK_CLIENT`, `sync_member_rank()`, `linked_profiles()`, `RankLookupError`
  - Termine: Slash-Befehl `/termin` in `register_app_commands()` (Embed mit `EVENT_MARKER`, `EventRSVPView`, `save_scheduled_event()`), `parse_event_time()`, `BERLIN`
  - Anwesenheit: `attendance(guild_id, user_id, since, until)`
  - Bewerbungen: `reopen_applications()`, `required_votes()`, `APPLICATION_MIN_VOTES`
  - Datenschutz: `collect_user_data()`, `delete_user_data()`, `delete_my_applications()`
  - Server-Setup: `setup_guild(guild, apply)` mit Vorschau-Modus (`apply=False`)
  - Temp-Voice: `TEMP_VOICE_CREATORS`, `temp_voice_ids()`, `clean_up_temp_voice()`
- **Deployment:** VPS mit systemd-Dienst `duality-bot`, User `dualitybot`, Code in `/home/duality-bot/app`, venv in `/home/duality-bot/.venv`, Updates über `update-mac.sh` (commit + push, mit fester Dateiliste `FILES=(...)`) und `update-vps.sh` (pull + Neustart).
- **Sprache:** Alle Texte für Nutzer sind Deutsch, Code-Kommentare ebenfalls Deutsch und sparsam. Logger heißt `log = logging.getLogger("duality")`.
- `REVIEW.md` enthält ein früheres Code-Review. Lies es, damit du keine dort behobenen Fehler wieder einbaust.

Lies `main.py` vollständig, bevor du etwas änderst, und halte dich an Stil, Namensgebung und Struktur des bestehenden Codes.

## Ziel

1. Ein **Web-Panel mit Konsole**, über das der Staff den laufenden Bot im Browser überwachen und steuern kann, ohne per SSH auf den VPS zu müssen.
2. Mehrere **neue Features** für Panel und Bot (Überwachung, Backups, Team-Verwaltung, Kommunikation, Datenschutz, Ränge, Scrim-Suche).

Umsetzung in **Phasen** (siehe unten). Nach jeder Phase anhalten, zusammenfassen und auf meine Freigabe warten.

## Architektur (verbindlich)

1. **Gleicher Prozess, gleiche Event-Loop.** Das Panel ist ein `aiohttp.web`-Server, der in `main()` neben dem Discord-Client gestartet wird (`web.AppRunner` + `web.TCPSite`) und beim Beenden sauber heruntergefahren wird. So hat das Panel direkten Zugriff auf `client`, die Guild und die Tasks: keine IPC, keine zweite Bot-Instanz.
2. **Keine neuen Abhängigkeiten** außer, wenn wirklich unvermeidbar (dann begründen). Kein Frontend-Build-Schritt: statisches HTML + CSS + Vanilla-JS, ausgeliefert vom aiohttp-Server. Diagramme ohne Bibliothek (SVG) oder über eine einzelne, versionsgepinnte Datei, die im Repo liegt.
3. **Eigenes Modul** `panel.py` (plus Ordner `panel_static/` für HTML/CSS/JS). `main.py` importiert nur eine Start-Funktion, z. B. `start_panel(client, target_id) -> web.AppRunner`. Zirkuläre Imports vermeiden: Was `panel.py` aus `main.py` braucht, wird als Parameter oder über ein kleines Kontextobjekt übergeben. Wenn `main.py` für ein Feature Logik aus einem Slash-Befehl braucht (z. B. Termin anlegen), zieh sie in eine gemeinsame Funktion heraus, die Befehl und Panel beide nutzen. Keine Logik doppelt.
4. **Nur im `--apply`-Modus** und nur, wenn `PANEL_ENABLED=1` gesetzt ist. Fehlt die Konfiguration, startet der Bot normal ohne Panel und gibt eine klare Meldung aus. Bot-Features, die kein Panel brauchen (Rang-Glückwunsch, `/rangliste`, Fehler-Benachrichtigung, Backups), laufen auch ohne Panel.
5. **Bindet standardmäßig an `127.0.0.1`** (`PANEL_HOST`, `PANEL_PORT`, Default `8080`). Öffentlich erreichbar nur über einen Reverse-Proxy mit HTTPS (Caddy oder nginx), dafür eine Beispielkonfiguration in die README schreiben.
6. **Neue Tabellen** nur über `initialize_storage()` mit `CREATE TABLE IF NOT EXISTS`, bestehende Daten dürfen nicht verloren gehen.

## Login & Sicherheit (nicht verhandelbar)

- **Login über Discord OAuth2** (Scope `identify`). Neue Env-Variablen: `DISCORD_CLIENT_ID`, `DISCORD_CLIENT_SECRET`, `PANEL_BASE_URL` (für die Redirect-URI), `PANEL_SECRET_KEY` (zum Signieren der Session-Cookies, z. B. HMAC-SHA256 mit `hmac`/`hashlib` aus der Standardbibliothek).
- **Zugriff nur für Staff:** Nach dem Login wird geprüft, ob die Discord-User-ID Mitglied der Guild ist und eine Rolle aus `STAFF_ROLES` hat, live über `guild.get_member(...)` und bei jeder Anfrage erneut (Rollenentzug wirkt sofort).
- **Rechte-Stufen:**
  - `Coach`: lesen, Termine anlegen/bearbeiten, Anwesenheit sehen, Scrim-Anfragen verwalten
  - `Admin`: zusätzlich steuernde Konsolenbefehle, Ankündigungen, Texte und Einstellungen bearbeiten, Datenschutz-Auskunft/-Löschung
  - `Owner`: zusätzlich `restart`, Backup-Download, Setup anwenden
- Session-Cookie: `HttpOnly`, `Secure` (außer bei `127.0.0.1` ohne HTTPS), `SameSite=Lax`, Ablauf nach 12 h.
- **CSRF-Schutz** für alle POST-Anfragen und den WebSocket (Token in der Session, Origin-Prüfung).
- `state`-Parameter beim OAuth-Flow prüfen.
- **Kein `eval`/`exec`, keine Shell.** Die Konsole führt ausschließlich fest definierte Befehle aus (Whitelist, siehe unten).
- Einfaches Rate-Limit für Login und Konsolenbefehle.
- **Jede steuernde Aktion** wird in den Discord-Audit-Kanal geschrieben (`audit(...)`, mit Panel-Kennzeichnung und wer es war) und ins Log.
- Keine Secrets, Tokens oder Session-IDs in Logs oder im Frontend.
- Alle Nachrichten, die der Bot im Auftrag des Panels sendet, mit `discord.AllowedMentions.none()`, außer eine Erwähnung ist ausdrücklich gewollt (dann nur die nötige).
- **Datenschutz:** Das Panel speichert keine zusätzlichen personenbezogenen Daten von Mitgliedern. Es zeigt nur, was der Bot ohnehin speichert. Bewerbungsinhalte (Name, Kurs, Tracker-Link) liegen bewusst nur in der Discord-Nachricht. Das Panel verlinkt auf die Nachricht, kopiert die Inhalte aber nicht in die DB.

## Funktionen

### A. Panel-Grundlagen

#### A1. Dashboard
- Bot-Status: online/verbunden, Gateway-Latenz, Uptime, Python-/discord.py-Version, aktueller Git-Commit (`git rev-parse --short HEAD`, einmal beim Start gelesen).
- Guild: Name, Mitgliederzahl, Anzahl Spieler/Bewerber/Staff.
- Status aller Hintergrund-Tasks (`is_running()`, letzter Lauf, nächster Lauf, letzter Fehler).
- HenrikDev-API: aktiv ja/nein, letzter Fehler, ob gerade ein Rate-Limit aktiv ist.
- Letztes Backup (Zeitpunkt, Größe).
- Aktualisiert sich automatisch über denselben WebSocket wie die Konsole.

#### A2. Live-Konsole
- **Log-Stream:** Ein `logging.Handler` schreibt alle Logeinträge (Logger `duality` und `discord`) in einen Ringpuffer (z. B. `collections.deque(maxlen=1000)`) und an alle verbundenen WebSocket-Clients. Die bestehenden `print(..., flush=True)`-Ausgaben sollen ebenfalls im Panel landen, entweder durch Umstellen auf `log.info` oder durch einen schmalen Wrapper. Entscheide, was am wenigsten Code verändert, und begründe es.
- Beim Öffnen werden die letzten Einträge aus dem Puffer nachgeladen.
- Filter nach Level (DEBUG/INFO/WARNING/ERROR) und Freitextsuche, Auto-Scroll mit Pause-Knopf, farbige Level.
- **Befehlszeile** mit Autovervollständigung und Verlauf (Pfeiltasten). Whitelist-Befehle, jeder mit Rechteprüfung:
  - `help`: listet die Befehle
  - `status`: Kurzstatus wie im Dashboard
  - `sync-ranks [@user|user_id]`: Rang-Abgleich sofort (alle oder einer), mit `RANK_SYNC_DELAY_SECONDS` zwischen den Abfragen
  - `reminders`: Termin-Erinnerungen sofort prüfen
  - `purge`: Löschfrist-Aufgabe sofort ausführen
  - `cleanup-voice`: `clean_up_temp_voice` ausführen
  - `backup`: sofort ein Backup erstellen
  - `say #kanal <text>`: Nachricht als Bot senden (Admin+, Bestätigung im UI)
  - `tasks`: Status aller Loops
  - `restart`: Bot beendet sich sauber, systemd startet ihn neu (`Restart=always` in der Unit prüfen/dokumentieren); nur Owner, mit Bestätigungsdialog
- Unbekannte Befehle geben eine freundliche Fehlermeldung, nie einen Stacktrace ans Frontend.

#### A3. Bewerbungen
- Tabelle aus `applications` + `application_votes`: Bewerber (Anzeigename + Avatar aus dem Cache), Status, Datum, Stimmen 👍/👎, Entscheider, Link zur Discord-Nachricht.
- Filter nach Status. **Keine** Annehmen/Ablehnen-Buttons im Panel (die Entscheidung bleibt in Discord mit der bestehenden Abstimmungslogik).
- Button „Neue Saison“ = gleiche Logik wie `/neue-saison` (`reopen_applications`), mit Bestätigungsdialog, Admin+.

#### A4. Spieler & Profile
- Liste aus `player_profiles`: Riot-ID, Rang, letzter Abgleich, Rollen; Button „Rang jetzt abgleichen“ pro Person.

#### A5. Tickets
- Offene/geschlossene Tickets aus `tickets` mit Link zum Thread. Inhalte werden nicht angezeigt.

### B. Überwachung & Betrieb

#### B1. Benachrichtigung bei Fehlern
- Benachrichtigt den Staff, wenn:
  - ein Hintergrund-Task mit Fehler endet oder stehen bleibt,
  - der Bot länger als `ALERT_DISCONNECT_MINUTES` (Default 5) vom Gateway getrennt ist,
  - HenrikDev wiederholt mit 429/Rate-Limit oder Fehlern antwortet (z. B. 3× in Folge),
  - ein Backup fehlschlägt.
- **Kanal:** Ein Discord-Webhook aus `ALERT_WEBHOOK_URL` (funktioniert per HTTP auch, wenn die Gateway-Verbindung weg ist). Ohne Webhook: Nachricht in den Audit- oder Staff-Kanal, sobald der Bot wieder verbunden ist.
- **Entprellung:** Gleiche Warnung höchstens einmal pro 30 min, plus eine „wieder in Ordnung“-Meldung, wenn sich das Problem auflöst.
- Läuft unabhängig vom Panel. Die letzten Warnungen werden im Dashboard angezeigt.

#### B2. Datenbank-Backups
- Täglicher Task, der ein konsistentes Backup über die SQLite-Backup-API (`sqlite3.Connection.backup`) erstellt, nicht per Dateikopie.
- Zielordner `BACKUP_DIR` (Default: `backups/` neben `main.py`), Dateiname mit Datum, Dateirechte `600`. Aufbewahrung `BACKUP_RETENTION_DAYS` (Default 14), ältere Backups werden gelöscht.
- `backups/` in `.gitignore` aufnehmen (die Backups enthalten personenbezogene Daten).
- Im Panel: Liste der Backups und Download-Knopf (nur Owner, Download wird im Audit-Log vermerkt). Kein Wiederherstellen über das Panel; wie man ein Backup zurückspielt, kommt in die README.

#### B3. Setup-Vorschau als Diff
- `setup_guild` so umbauen, dass es im Vorschau-Modus eine **strukturierte Liste der geplanten Änderungen** zurückgibt (Rollen anlegen/umbenennen, Kanäle anlegen/löschen/verschieben, Rechte ändern, Nachrichten neu posten), statt sie nur zu drucken. Die bestehende Terminal-Ausgabe muss gleich bleiben.
- Panel-Seite „Server-Setup“: zeigt den Diff gruppiert und farbig (neu / ändern / löschen). Löschungen deutlich hervorheben.
- Button „Anwenden“ nur für Owner, mit Bestätigungsdialog, der die Zahl der Löschungen nennt. Danach Ergebnis und Audit-Eintrag.
- Vorschau und Anwenden dürfen nicht parallel laufen (Lock).

#### B4. Live-Ansicht der Voice-Kanäle
- Alle Voice-Kanäle mit den Mitgliedern darin (Name, Avatar, stumm/taub, Streaming), temporäre Kanäle aus `temp_voice_channels` markiert, inklusive Besitzer.
- Aktualisierung live über `on_voice_state_update` und den WebSocket.
- Nur ansehen, keine Moderationsaktionen.

### C. Team & Coaching

#### C1. Anwesenheitsübersicht
- Pro Spieler (Rolle `Spieler`): Zusagen, Vielleicht, Absagen und keine Reaktion für einen wählbaren Zeitraum (Default: letzte 30 Tage), auf Basis von `attendance()`, `event_rsvps` und `scheduled_events`.
- Quote in Prozent, sortierbar. Spieler unter einer Schwelle (Default 50 %) oder mit mehreren Terminen ohne Reaktion werden markiert.
- Detailansicht pro Spieler: Liste der Termine mit Reaktion.
- Export als CSV (Coach+).
- Termin-Daten werden weiterhin nach `EVENT_RETENTION_DAYS` gelöscht; die Übersicht reicht nur so weit zurück. Das im UI erwähnen.

#### C2. Termine im Panel
- Kalender- und Listenansicht aller kommenden und vergangenen Termine mit Zusagen/Vielleicht/Absagen und gesendeten Erinnerungen.
- **Anlegen:** Titel, Datum/Uhrzeit, Infos. Nutzt dieselbe Logik wie `/termin` (vorher in eine gemeinsame Funktion herausziehen): gleiches Embed, `EventRSVPView`, `save_scheduled_event()`, gleiche Validierung.
- **Bearbeiten/Verschieben:** Embed in Discord aktualisieren, `starts_at` anpassen, bereits gesendete Erinnerungen für die neue Zeit zurücksetzen. Optional Hinweis an alle Zusagen.
- **Absagen:** Embed als „Abgesagt“ markieren, Buttons deaktivieren, keine Erinnerungen mehr.
- Coach+.

#### C3. Glückwunsch bei Rangaufstieg
- Beim Rang-Abgleich (`sync_member_rank`, täglich und manuell) den alten mit dem neuen Rang vergleichen. Reihenfolge nach `VALORANT_RANKS` plus Unterstufe (z. B. „Gold 2“ < „Gold 3“ < „Platinum 1“).
- Bei einem Aufstieg der Hauptstufe (z. B. Gold → Platinum) postet der Bot einen Glückwunsch im Teamkanal (Kanalname als Konstante, Kanal über `find_text_channel`). Nur für Mitglieder mit Rolle `Spieler`. Erwähnung nur der Person selbst.
- Kein Glückwunsch beim ersten Abgleich nach dem Verknüpfen der Riot-ID und nicht bei Abstiegen.
- Abschaltbar über eine Einstellung (siehe D3).

#### C4. Internes Rang-Leaderboard
- Neuer Slash-Befehl `/rangliste`: alle Spieler mit verknüpfter Riot-ID, sortiert nach Rang, mit Anzeigename (bzw. Name aus `player_roster_names`, wie bei `/team`), Rang und Datum des letzten Abgleichs.
- Gleiche Ansicht im Panel, zusätzlich mit Verlauf, falls du eine kleine Rang-Historie speicherst (neue Tabelle `rank_history`: guild_id, user_id, rank_tier, recorded_at). Die Historie wird in `collect_user_data()`/`delete_user_data()` aufgenommen.

### D. Kommunikation & Konfiguration

#### D1. Ankündigungs-Editor
- Formular für ein Embed: Titel, Text (Markdown), Farbe, Bild-URL, Thumbnail, Footer, optional Felder. Zielkanal aus einer Auswahl der Textkanäle, in denen der Bot schreiben darf.
- **Live-Vorschau** im Discord-Stil im Panel.
- Optionale Erwähnung einer Benachrichtigungsrolle (die vorhandenen Rollen aus `NotificationView`), sonst keine Erwähnungen. `@everyone`/`@here` nicht anbieten.
- **Sofort senden** oder **zeitgesteuert**: neue Tabelle `scheduled_announcements`, ein Task prüft jede Minute. Geplante Ankündigungen können bis zum Versand bearbeitet oder gelöscht werden.
- Bereits gesendete Ankündigungen bearbeiten (Nachricht in Discord editieren).
- Admin+, jede Aktion im Audit-Log.

#### D2. Texte bearbeiten
- Die festen Texte aus `main.py` in eine Tabelle `bot_texts` (key, value, updated_by, updated_at) auslagern. Mindestens: Willkommen, Regeln, Bewerbungs-Infotext, DM bei Annahme, DM bei Ablehnung, `/help`.
- Die aktuellen Texte im Code bleiben als **Standardwerte**. Ist in der DB nichts gespeichert, wird der Standard verwendet.
- Panel: Editor mit Vorschau, Zeichenlimits von Discord prüfen (Embed-Beschreibung 4096, Feldwerte 1024, Nachricht 2000), Button „Auf Standard zurücksetzen“, Änderungsverlauf (letzte 20 Versionen pro Text).
- Texte, die als feste Nachricht im Server stehen (Willkommen, Regeln, Bewerbung), werden nach dem Speichern in Discord aktualisiert, ohne die Nachricht neu zu posten (Buttons und `custom_id`s bleiben erhalten).
- Admin+.

#### D3. Einstellungen bearbeiten
- Tabelle `bot_settings` (key, value als JSON, updated_by, updated_at). Die heutigen Konstanten bleiben Standardwerte. Mindestens:
  - `APPLICATION_MIN_VOTES`
  - `APPLICATION_RETENTION_DAYS`, `EVENT_RETENTION_DAYS`
  - Zeitpunkte der Termin-Erinnerungen
  - `RANK_SYNC_DELAY_SECONDS`
  - Rang-Glückwunsch an/aus, Anwesenheits-Schwelle, Alarm-Schwellen
- Jede Einstellung hat Typ, erlaubten Bereich und Beschreibung. Ungültige Werte werden abgelehnt.
- Der Code liest die Werte über einen kleinen Helfer (z. B. `setting("application_min_votes")`) mit Cache, der beim Speichern invalidiert wird. Änderungen wirken ohne Neustart.
- Admin+, jede Änderung im Audit-Log mit altem und neuem Wert.

### E. Datenschutz

#### E1. Auskunft und Löschung durch den Staff
- Panel-Seite „Datenschutz“: Mitglied per Name oder User-ID suchen (auch Personen, die den Server schon verlassen haben, über die User-ID).
- **Auskunft:** zeigt und exportiert als JSON, was `collect_user_data()` liefert (erweitert um alle neuen Tabellen wie `rank_history`).
- **Löschung:** nutzt `delete_user_data()` und `delete_my_applications()`, mit Pflichtfeld „Grund / Anfrage vom“ und Bestätigungsdialog.
- Im Audit-Log wird nur vermerkt, *dass* eine Auskunft/Löschung durch wen erfolgte, nicht die Daten selbst.
- Admin+.
- Prüfe, dass alle neuen Tabellen aus diesem Prompt in `collect_user_data()` und `delete_user_data()` berücksichtigt sind.

### F. Scrim-Suche

**Ziel:** Teams finden, die gerade Scrims suchen, und sie bequem anfragen oder einladen können.

**Wichtig vorab:** Viele Scrim-Plattformen haben keine offizielle API, und Scraping oder automatisches Anschreiben verstößt oft gegen deren Nutzungsbedingungen. Discord-Bots können außerdem nur Server lesen, in die sie eingeladen wurden, und Self-Bots (automatisierte Nutzerkonten) sind verboten. Deshalb:

1. **Recherche zuerst:** Finde heraus, welche Plattformen und Quellen für Valorant-Scrims (EU/DE, Hochschul- und Amateurniveau) es gibt und welche davon eine **offizielle API, einen RSS-Feed oder eine erlaubte Integration** anbieten. Prüfe die Nutzungsbedingungen. Lege mir eine Tabelle vor (Plattform, Zugang, was abrufbar ist, Bedingungen) und **warte auf meine Entscheidung**, bevor du eine Plattform anbindest.
2. **Nur erlaubte Anbindungen:** Kein Scraping gegen Nutzungsbedingungen, keine Self-Bots, keine automatischen Nachrichten an fremde Teams.
3. **Kontakt bleibt menschlich:** Der Bot schickt nichts automatisch an andere Teams. Er bereitet die Anfrage vor (Text mit Datum, Uhrzeit, Rang-Durchschnitt, Server-Einladungslink), und ein Staff-Mitglied sendet sie selbst über die jeweilige Plattform oder kopiert sie. Ausnahme: Plattformen, deren offizielle API Anfragen ausdrücklich vorsieht.

**Funktionen (unabhängig von der Plattform):**
- **Scrim-Börse im Panel:** Einträge aus angebundenen Quellen plus manuell eingetragene Teams (Teamname, Rang-Spanne, Region, Kontakt, Quelle/Link, gesuchte Zeiten). Filter nach Rang, Zeit, Region.
- **Eigenes Gesuch erstellen:** Zeiten, Rang-Spanne, Maps/Format. Der Bot erzeugt daraus einen Text zum Kopieren für die Plattformen und kann ihn in einem eigenen Kanal im Server posten.
- **Anfragen verfolgen:** neue Tabelle `scrim_requests` mit Status (geplant, angefragt, bestätigt, abgesagt, gespielt), Gegner, Zeitpunkt, Notizen.
- **Bestätigter Scrim wird zum Termin:** ein Klick legt über die gemeinsame Termin-Funktion (C2) einen Termin in `#spieltermine` an, damit Zusagen und Erinnerungen wie gewohnt laufen.
- **Einladungslink:** Der Bot kann einen zeitlich begrenzten Einladungslink für einen Scrim-Gastkanal erzeugen (z. B. 24 h, begrenzte Nutzungen), statt einen dauerhaften Link zu verbreiten.
- Gespeichert werden nur Team-Kontakte, die die Teams selbst öffentlich angegeben haben. Alte Einträge werden nach einer Frist gelöscht (Einstellung in D3).
- Coach+.

## UI

- Dunkles, schlichtes Design im Duality-Lila (`rgb(110, 70, 255)` wie im `/team`-Embed). Funktioniert auch auf dem Handy (Sidebar wird zur Leiste).
- Alle Texte auf Deutsch, Zeiten in `Europe/Berlin` (`BERLIN` aus `main.py`).
- Eine Seite mit Navigation (kein SPA-Framework): Dashboard, Konsole, Voice, Termine, Anwesenheit, Rangliste, Bewerbungen, Spieler, Tickets, Ankündigungen, Scrims, Texte, Einstellungen, Server-Setup, Backups, Datenschutz. Menüpunkte ohne Berechtigung ausblenden.
- Daten über JSON-Endpunkte unter `/api/...`, Live-Daten über `/ws`.
- Barrierearm: echte Buttons, Labels, ausreichender Kontrast, per Tastatur bedienbar.
- Gefährliche Aktionen (Löschen, Setup anwenden, Neustart) immer mit Bestätigungsdialog, der beschreibt, was passiert.

## Integration in das bestehende Projekt

- `update-mac.sh`: `FILES=(...)` um alle neuen Dateien und Ordner erweitern (`panel.py`, `panel_static/`, Tests, `README.md`). Achtung: Das Skript prüft heute mit `[[ ! -f "$file" ]]`, das schlägt bei Ordnern fehl. Prüfung auf `-e` umstellen, damit Ordner funktionieren. Was nicht in `FILES` steht, wird nicht committet.
- `requirements.txt`: `aiohttp` explizit eintragen, wenn es direkt importiert wird (Version passend zu `discord.py==2.7.1`).
- `.gitignore`: `backups/` aufnehmen. Nichts freigeben, was Secrets enthalten könnte.
- **README**-Abschnitt „Web-Panel“: Discord-Application anlegen (Redirect-URI), alle neuen Env-Variablen mit Standardwerten, systemd-`Environment=`/`EnvironmentFile=`-Beispiel, Caddy-Beispiel (`reverse_proxy 127.0.0.1:8080` inkl. WebSocket), Alternative per SSH-Tunnel (`ssh -L 8080:127.0.0.1:8080 ...`) für den Anfang ohne Domain, Backup zurückspielen, Alarm-Webhook einrichten.
- Neue Slash-Befehle (`/rangliste`) in `/help` aufnehmen.
- Bestehendes Verhalten darf sich **nicht** ändern, wenn `PANEL_ENABLED` nicht gesetzt ist, abgesehen von den ausdrücklich gewollten Bot-Features (B1, B2, C3, C4).

## Git & GitHub

Alles läuft ganz normal über das GitHub-Repo `ArvidsGertners/duality-bot`, Branch `main`, wie bisher.

- **Du darfst nach jedem fertigen Feature und nach jeder Phase `update-mac.sh` selbst ausführen.** Das Skript committet die Dateien aus `FILES` und pusht nach `main`.
- Die Commit-Nachricht immer als Argument übergeben, damit das Skript nicht interaktiv nachfragt: `bash update-mac.sh "Panel: Live-Konsole mit Log-Stream und Befehlen"`.
- **Ein Commit pro Feature**, Nachricht auf Deutsch im Stil der bisherigen Commits (kurz, sagt was sich ändert, z. B. „Termine im Panel anlegen, verschieben und absagen“).
- **Vor jedem Ausführen:**
  1. `python -m py_compile` und alle Tests laufen ohne Fehler. Schlägt etwas fehl, wird nicht gepusht.
  2. Neue Dateien und Ordner stehen in `FILES` von `update-mac.sh`, sonst fehlen sie im Commit.
  3. `git status` prüfen: Keine Secrets, keine `.env`, keine Datenbank und keine Backups im Commit.
  4. Der Bot startet ohne `PANEL_ENABLED` weiterhin normal, damit `main` jederzeit auf den VPS kann.
- Schlägt `update-mac.sh` fehl (z. B. Push abgelehnt), nicht mit `--force` oder anderen Tricks weitermachen, sondern anhalten und mir die Ausgabe zeigen.
- **Den VPS aktualisierst du nicht.** `update-vps.sh` läuft als root auf dem Server, das mache ich selbst. Sag mir nach jeder Phase, ob ein VPS-Update sinnvoll ist und welche neuen Env-Variablen ich dort vorher setzen muss.
- Keine anderen Git-Befehle, die Verlauf umschreiben (`rebase`, `reset --hard`, `push --force`, `commit --amend` auf gepushte Commits).

## Fortschritt (`PROGRESS.md`)

- `PROGRESS.md` im Repo-Root ist das Gedächtnis dieser Arbeit. Vor jeder Arbeit lesen, nach jedem Feature aktualisieren (vor dem Commit, damit es im selben Commit landet).
- Inhalt: Status jeder Phase und jedes Features (offen / in Arbeit / fertig + Commit-Hash), getroffene Entscheidungen und Annahmen, neue Env-Variablen und Tabellen, offene Fragen an mich, bekannte Probleme, nächster konkreter Schritt.
- Was dort als fertig steht, wird nicht noch einmal gebaut. Im Zweifel mit `git log` und dem Code abgleichen.
- `PROGRESS.md` und `PANEL_PROMPT.md` stehen in `FILES` von `update-mac.sh`.

## Phasen

Nach jedem Feature: testen, dann mit `update-mac.sh` committen und pushen. Nach jeder Phase: anhalten, kurz zusammenfassen (was geändert, was getestet, was offen, welche Commits gepusht, ob ein VPS-Update nötig ist) und auf meine Freigabe für die nächste Phase warten.

1. **Grundgerüst:** aiohttp-Server + Healthcheck, OAuth-Login, Rechte-Stufen, CSRF, Log-Handler, WebSocket-Konsole mit Befehlen, Dashboard (A1, A2).
2. **Betrieb:** Fehler-Benachrichtigung (B1), Backups (B2), Voice-Ansicht (B4), Bewerbungen/Spieler/Tickets lesen (A3–A5).
3. **Team:** gemeinsame Termin-Funktion + Termine im Panel (C2), Anwesenheit (C1), Rang-Glückwunsch (C3), `/rangliste` (C4).
4. **Konfiguration & Kommunikation:** Einstellungen (D3), Texte (D2), Ankündigungen (D1), Datenschutz-Seite (E1).
5. **Setup-Diff** (B3), weil es `setup_guild` umbaut.
6. **Scrim-Suche** (F): erst Recherche und meine Entscheidung, dann Umsetzung.
7. **Abschluss:** README, Update-Skripte, `/help`.

## Vorgehen & Prüfen

1. Lies `main.py`, `REVIEW.md` und die beiden Update-Skripte. Fasse kurz zusammen, wo du eingreifst.
2. Stell mir Rückfragen nur, wenn etwas wirklich unklar ist (z. B. Domain vorhanden oder nicht, Name des Teamkanals für Glückwünsche). Sonst triff vernünftige Annahmen und nenne sie.
3. **Prüfen in jeder Phase:**
   - `python -m py_compile main.py panel.py`
   - Panel lokal mit `PANEL_ENABLED=1` starten und im Browser testen: Login-Flow, Rechte für Coach vs. Admin vs. Owner, WebSocket-Reconnect nach Neustart, CSRF-Ablehnung, Befehl ohne Rechte.
   - Bot ohne `PANEL_ENABLED` starten: Verhalten wie vorher.
   - Kleine Tests ohne Discord-Verbindung für: Session-Signierung, CSRF-Prüfung, Befehls-Parser, Rang-Vergleich (C3), Einstellungs-Validierung (D3), Anwesenheits-Berechnung (C1), Backup + Aufräumen (B2), Entprellung der Alarme (B1).
4. Nach jedem Feature mit `update-mac.sh` committen und pushen (siehe „Git & GitHub“).
5. Am Ende: Liste der neuen Env-Variablen und Tabellen, geänderte Dateien, gepushte Commits, was du nicht testen konntest, offene Punkte.

## Nicht machen

- Keinen zweiten Prozess, keine zweite Bot-Instanz, kein Flask/Django/FastAPI, kein React/Node-Build.
- Keine beliebige Code-Ausführung, kein Datei-Browser, keine Bearbeitung von `.env` über das Panel.
- Keine Annehmen/Ablehnen-Entscheidungen für Bewerbungen im Panel.
- Kein Wiederherstellen von Backups über das Panel.
- Kein Scraping gegen Nutzungsbedingungen, keine Self-Bots, keine automatischen Nachrichten an fremde Teams.
- Keine `@everyone`/`@here`-Erwähnungen aus dem Panel.
- Nicht pushen, wenn Tests oder `py_compile` fehlschlagen.
- Nicht auf den VPS zugreifen und `update-vps.sh` nicht ausführen.
