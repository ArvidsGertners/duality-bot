# Duality-Bot

Discord-Bot von **Duality Mannheim**, dem Valorant-Hochschulteam der DHBW Mannheim. Er richtet den Server ein (Rollen, Kanäle, Rechte, feste Nachrichten), führt durch Bewerbungen, verwaltet Termine, Tickets, Ränge über die Riot-ID und Join-to-Create-Voice. Dazu gehört ein **Web-Panel** für den Staff.

- `main.py`: Bot und Server-Setup
- `panel.py` + `panel_static/`: Web-Panel (läuft im selben Prozess)
- `tests/`: Tests ohne Discord-Verbindung
- Daten: SQLite in `duality_bot_data.sqlite3` neben `main.py` (nicht im Git)

## Starten

Python 3.14, Abhängigkeiten aus `requirements.txt` (`discord.py`, `aiohttp`).

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Vorschau, ändert nichts am Server:

```bash
DISCORD_BOT_TOKEN=… .venv/bin/python main.py
```

Server einrichten und Bot online halten:

```bash
DISCORD_BOT_TOKEN=… .venv/bin/python main.py --apply
```

Ohne `DISCORD_BOT_TOKEN` fragt der Bot den Token verdeckt im Terminal ab. `--debug` zeigt zusätzliche Gateway-Details.

## Umgebungsvariablen

Secrets gehören nie in den Code, sondern in die Umgebung (auf dem VPS in `/etc/duality-bot.env`, Rechte `600`, Besitzer `root`).

| Variable | Standard | Zweck |
|---|---|---|
| `DISCORD_BOT_TOKEN` | – (Pflicht) | Bot-Token |
| `DISCORD_GUILD_ID` | Duality-Server | Server-ID |
| `HENRIKDEV_API_KEY` | – | Rangabfrage und Match-Statistiken über die Riot-ID; ohne Schlüssel sind beide aus |
| `ALERT_WEBHOOK_URL` | – | Discord-Webhook für Warnungen; ohne Webhook gehen sie nach `#audit-log` |
| `ALERT_DISCONNECT_MINUTES` | `5` | Standard für „Warnung nach so vielen Minuten ohne Verbindung“ (im Panel änderbar) |
| `BACKUP_DIR` | `backups/` neben `main.py` | Zielordner der Datenbank-Backups |
| `BACKUP_RETENTION_DAYS` | `14` | Ältere Backups werden gelöscht |
| `PANEL_ENABLED` | – | `1` schaltet das Web-Panel ein (nur mit `--apply`) |
| `PANEL_HOST` | `127.0.0.1` | Bind-Adresse des Panels; nicht öffentlich machen |
| `PANEL_PORT` | `8080` | Port des Panels (auf dem VPS `8090`, weil 8080 belegt ist) |
| `PANEL_BASE_URL` | – | Öffentliche Adresse ohne Pfad, z. B. `https://panel.example.de`; `http://` nur für `localhost` |
| `PANEL_SECRET_KEY` | – | Mindestens 32 zufällige Zeichen, signiert die Login-Cookies |
| `DISCORD_CLIENT_ID` | – | OAuth2-Client-ID der Discord-Application |
| `DISCORD_CLIENT_SECRET` | – | OAuth2-Client-Secret der Discord-Application |

Fehlt eine Panel-Variable, startet der Bot normal ohne Panel und schreibt den Grund ins Log.

## Web-Panel

Das Panel ist ein aiohttp-Server im Bot-Prozess. Es zeigt Dashboard, Live-Konsole, Voice-Kanäle, Termine, Anwesenheit, Rangliste, Statistiken, Bewerbungen, Spieler, Tickets, Ankündigungen, Texte, Einstellungen, Server-Setup, Backups und Datenschutz. Anmelden geht nur mit Discord und nur mit einer Staff-Rolle; die Rolle wird bei jeder Anfrage neu geprüft.

| Rolle | Darf |
|---|---|
| Coach | alles ansehen, Termine anlegen/verschieben/absagen, Anwesenheit inkl. CSV, Riot-ID von Bewerbern auswerten (höchstens 6 pro Minute, nichts gespeichert), Konsole `help`/`status`/`tasks` |
| Admin | zusätzlich steuernde Konsolenbefehle (`sync-ranks`, `reminders`, `purge`, `cleanup-voice`, `backup`, `say`), Rang abgleichen, Matches neu abholen, neue Saison, Ankündigungen, Texte, Einstellungen, Datenschutz, Setup-Vorschau |
| Owner | zusätzlich `restart`, Backup-Download, Server-Setup anwenden |

Jede steuernde Aktion landet mit Name im Audit-Kanal.

**Statistiken:** Beim täglichen Rang-Abgleich holt der Bot zusätzlich die letzten Matches jedes verknüpften Spielers (`/valorant/v1/stored-matches`, Standard: 20 Competitive-Matches, einstellbar) und speichert Map, Agent, K/D/A, Treffer, Schaden, Punkte und Ergebnis in `match_stats`. Matches älter als 90 Tage löscht er; eine neue oder gelöste Riot-ID löscht die Matches des alten Kontos. Die Daten sind Teil von Auskunft und Löschung.

**Server-Wachstum:** Das Dashboard zeigt Beitritte und Abgänge der letzten 12 Wochen. Der Bot speichert dafür nur Tageszahlen in `member_flow` (keine User-IDs, 400 Tage). Beim Start trägt er Beitritte aus den Beitrittsdaten der aktuellen Mitglieder nach; Abgänge zählt er erst ab dem ersten Start mit dieser Version.

### 1. Discord-Application vorbereiten

1. [Discord Developer Portal](https://discord.com/developers/applications) → die Bot-Application → **OAuth2**.
2. Unter **Redirects** `<PANEL_BASE_URL>/auth/callback` eintragen, z. B. `https://panel.example.de/auth/callback` oder für den SSH-Tunnel `http://localhost:8080/auth/callback`. Die Adresse muss Zeichen für Zeichen stimmen.
3. **Client ID** und **Client Secret** in die Env-Datei schreiben. Ein neues Secret ändert den Bot-Token nicht.

### 2. Env-Datei und systemd

`/etc/duality-bot.env` (Rechte `600`):

```ini
DISCORD_BOT_TOKEN=…
DISCORD_GUILD_ID=…
HENRIKDEV_API_KEY=…
PANEL_ENABLED=1
PANEL_HOST=127.0.0.1
PANEL_PORT=8090
PANEL_BASE_URL=https://panel.example.de
PANEL_SECRET_KEY=…
DISCORD_CLIENT_ID=…
DISCORD_CLIENT_SECRET=…
```

Einen passenden Schlüssel erzeugt:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

Unit `/etc/systemd/system/duality-bot.service`:

```ini
[Unit]
Description=Duality Mannheim Discord Bot
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=dualitybot
Group=dualitybot
WorkingDirectory=/home/duality-bot/app
EnvironmentFile=/etc/duality-bot.env
ExecStart=/home/duality-bot/.venv/bin/python -u /home/duality-bot/app/main.py --apply
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`restart` im Panel beendet den Bot sauber mit Exit-Code 75. systemd startet ihn danach neu, das klappt mit `Restart=on-failure` und mit `Restart=always`. Mit `Restart=no` bliebe der Bot nach `restart` aus.

Nach Änderungen an Env-Datei oder Unit:

```bash
systemctl daemon-reload && systemctl restart duality-bot
```

### 3. Öffentlich erreichbar machen (HTTPS)

Das Panel lauscht nur auf `127.0.0.1`. Öffentlich geht es ausschließlich über einen Reverse-Proxy mit HTTPS. Der Proxy muss WebSockets durchreichen (`/ws` für Konsole und Live-Daten).

**Caddy** (holt das Zertifikat selbst):

```caddyfile
panel.example.de {
    reverse_proxy 127.0.0.1:8080
}
```

Caddy reicht WebSockets und `X-Forwarded-For` automatisch weiter.

**nginx** (so läuft es auf dem VPS, Zertifikat per `certbot certonly --nginx -d panel.example.de`):

```nginx
map $http_upgrade $duality_connection_upgrade {
    default upgrade;
    ''      close;
}
# Der OAuth-Callback trägt den einmaligen Discord-Code in der URL und wird nicht geloggt.
map $request_uri $duality_loggable {
    ~^/auth/callback 0;
    default 1;
}

server {
    listen 80;
    listen [::]:80;
    server_name panel.example.de;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    listen [::]:443 ssl;
    server_name panel.example.de;

    ssl_certificate /etc/letsencrypt/live/panel.example.de/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/panel.example.de/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    access_log /var/log/nginx/access.log combined if=$duality_loggable;

    # Nur statische Dateien komprimieren: API-Antworten tragen Geheimnisse (BREACH).
    location /static/ {
        proxy_pass http://127.0.0.1:8090;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        gzip on;
        gzip_vary on;
        gzip_proxied any;
        gzip_comp_level 6;
        gzip_min_length 1024;
        gzip_types text/css application/javascript text/javascript;
    }

    location / {
        proxy_pass http://127.0.0.1:8090;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $duality_connection_upgrade;
        proxy_read_timeout 120s;
    }
}
```

Danach `PANEL_BASE_URL` auf die neue Adresse setzen, die Redirect-URI im Developer Portal ergänzen und den Bot neu starten.

Ohne eigene Domain geht vorübergehend eine sslip.io-Adresse wie `panel.217-154-173-183.sslip.io`, die automatisch auf die IP zeigt. Sie hängt allerdings von einem fremden DNS-Dienst ab.

### 4. Ohne Domain: SSH-Tunnel

Für den Anfang oder für Notfälle reicht ein Tunnel vom eigenen Rechner:

```bash
ssh -N -L 8080:127.0.0.1:8090 root@<vps>
```

Dann `http://localhost:8080` im Browser öffnen. Dafür muss `PANEL_BASE_URL=http://localhost:8080` gesetzt und `http://localhost:8080/auth/callback` als Redirect eingetragen sein.

## Warnungen (Alarm-Webhook)

Der Bot meldet dem Staff gestoppte oder hängende Hintergrund-Tasks, fehlende Gateway-Verbindung, wiederholte Fehler der HenrikDev-API und fehlgeschlagene Backups. Er meldet auch, wenn sich ein Problem wieder erledigt hat; dieselbe Warnung kommt höchstens alle 30 Minuten.

Ohne Webhook landen Warnungen in `#audit-log`, aber nur, solange der Bot verbunden ist. Ein Webhook meldet auch einen Bot ohne Verbindung:

1. In Discord: Kanaleinstellungen eines Staff-Kanals (z. B. `#staff-chat`) → **Integrationen** → **Webhooks** → **Neuer Webhook** → URL kopieren.
2. `ALERT_WEBHOOK_URL=https://discord.com/api/webhooks/…` in die Env-Datei schreiben und den Bot neu starten.

Die Webhook-URL enthält ein Geheimnis. Sie steht nie im Log; wer sie hat, kann in den Kanal schreiben.

## Backups

Jeden Tag um 4:00 Uhr (Berliner Zeit) sichert der Bot die Datenbank über die SQLite-Backup-API nach `BACKUP_DIR`, als `duality-backup-JJJJ-MM-TT_HHMMSS.sqlite3` mit Rechten `600`. Backups älter als `BACKUP_RETENTION_DAYS` werden gelöscht. Zusätzlich gibt es den Konsolenbefehl `backup` und den Knopf auf der Backup-Seite. Herunterladen kann nur der Owner, jeder Download steht im Audit-Log. Der Ordner `backups/` ist in `.gitignore`, denn die Backups enthalten personenbezogene Daten.

### Backup zurückspielen

Zurückspielen geht absichtlich nur auf dem Server, nicht im Panel:

```bash
systemctl stop duality-bot
cd /home/duality-bot/app
cp -p duality_bot_data.sqlite3 duality_bot_data.sqlite3.vor-restore
cp backups/duality-backup-JJJJ-MM-TT_HHMMSS.sqlite3 duality_bot_data.sqlite3
chown dualitybot:dualitybot duality_bot_data.sqlite3
chmod 600 duality_bot_data.sqlite3
systemctl start duality-bot
```

Danach im Panel oder mit `journalctl -u duality-bot -n 50` prüfen, ob der Bot normal läuft. Hat alles geklappt, die Datei `*.vor-restore` löschen.

## Datenschutz

- Mitglieder sehen, exportieren und löschen ihre Daten mit `/meine-daten`.
- Der Staff macht dasselbe im Panel unter **Datenschutz**, auch für Personen, die den Server verlassen haben (Suche per User-ID).
- Ins Audit-Log kommt nur, dass und von wem eine Auskunft oder Löschung erfolgte, nie die Daten.
- Bewerbungsinhalte (Name, Kurs, Tracker-Link) liegen nur in der Discord-Nachricht, nicht in der Datenbank.
- Tickets und Staff-Stimmen bleiben als Nachweis erhalten. Bei Änderungen im Panel (Einstellungen, Texte, Ankündigungen) wird bei einer Löschung nur der Name entfernt.
- In Backups stehen gelöschte Daten, bis das Backup nach `BACKUP_RETENTION_DAYS` gelöscht wird.

## Entwicklung und Deploy

Tests und Syntaxprüfung, beides muss vor jedem Commit fehlerfrei sein:

```bash
.venv/bin/python -m py_compile main.py panel.py
.venv/bin/python -m unittest discover -s tests
```

Ein Test prüft zusätzlich, dass ein echter Bot-Token geschwärzt wird. Er liest `DISCORD_BOT_TOKEN` aus der Umgebung oder aus einer lokalen `.env` (gitignored) und wird ohne Token übersprungen.

Auf dem Mac committen und pushen, mit der Nachricht als Argument. `update-mac.sh` committet nur die Dateien und Ordner aus `FILES`; neue Dateien gehören dort hinein:

```bash
bash update-mac.sh "Kurze Beschreibung der Änderung"
```

Auf dem VPS als root aktualisieren. Das Skript holt den neuen Stand, installiert die Abhängigkeiten und startet den Dienst neu:

```bash
bash /home/duality-bot/app/update-vps.sh
```

Vorher ist ein Backup sinnvoll, etwa mit dem Konsolenbefehl `backup` im Panel. Beim Neustart führt `--apply` das Setup erneut aus. Es legt nur Fehlendes an und aktualisiert die festen Nachrichten. Die Setup-Vorschau im Panel zeigt vorher, was passieren würde.
