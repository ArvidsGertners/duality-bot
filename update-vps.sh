#!/usr/bin/env bash
set -Eeuo pipefail

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
    echo "Bitte als root ausführen, zum Beispiel: bash /home/duality-bot/app/update-vps.sh"
    exit 1
fi

BOT_USER="dualitybot"
BOT_HOME="/home/duality-bot"
APP_DIR="$BOT_HOME/app"
VENV_PYTHON="$BOT_HOME/.venv/bin/python"
UV="/usr/local/bin/uv"
SERVICE="duality-bot"
EXPECTED_REMOTE="git@github.com:ArvidsGertners/duality-bot.git"

run_as_bot() {
    runuser -u "$BOT_USER" -- env HOME="$BOT_HOME" "$@"
}

if [[ ! -d "$APP_DIR/.git" ]]; then
    echo "Fehler: Git-Repo nicht gefunden unter $APP_DIR"
    exit 1
fi

if [[ ! -x "$VENV_PYTHON" ]]; then
    echo "Fehler: Python-Umgebung fehlt: $VENV_PYTHON"
    exit 1
fi

if [[ ! -x "$UV" ]]; then
    echo "Fehler: uv wurde nicht gefunden unter $UV"
    exit 1
fi

remote_url="$(run_as_bot git -C "$APP_DIR" remote get-url origin)"
if [[ "$remote_url" != "$EXPECTED_REMOTE" ]]; then
    echo "Fehler: origin zeigt auf '$remote_url', erwartet wird '$EXPECTED_REMOTE'."
    exit 1
fi

branch="$(run_as_bot git -C "$APP_DIR" branch --show-current)"
if [[ "$branch" != "main" ]]; then
    echo "Fehler: Der VPS-Checkout ist auf Branch '$branch' statt 'main'."
    exit 1
fi

if [[ -n "$(run_as_bot git -C "$APP_DIR" status --porcelain)" ]]; then
    echo "Fehler: Im VPS-Repo liegen lokale Änderungen. Bitte erst prüfen, bevor aktualisiert wird."
    run_as_bot git -C "$APP_DIR" status --short
    exit 1
fi

echo "Prüfe GitHub auf neue Commits ..."
run_as_bot git -C "$APP_DIR" fetch origin main
local_commit="$(run_as_bot git -C "$APP_DIR" rev-parse HEAD)"
remote_commit="$(run_as_bot git -C "$APP_DIR" rev-parse origin/main)"

if [[ "$local_commit" == "$remote_commit" ]]; then
    echo "Der VPS ist bereits aktuell; der Bot wurde nicht neu gestartet."
    exit 0
fi

echo "Hole Updates ..."
run_as_bot git -C "$APP_DIR" pull --ff-only origin main

echo "Aktualisiere Python-Abhängigkeiten ..."
run_as_bot "$UV" pip install --python "$VENV_PYTHON" -r "$APP_DIR/requirements.txt"

echo "Starte den Bot-Dienst neu. --apply führt dabei die Server-Einrichtung erneut aus."
if ! systemctl restart "$SERVICE"; then
    systemctl --no-pager --full status "$SERVICE" || true
    journalctl -u "$SERVICE" -n 40 --no-pager -o cat || true
    exit 1
fi

systemctl --no-pager --full status "$SERVICE"
journalctl -u "$SERVICE" -n 40 --no-pager -o cat
echo "VPS-Update abgeschlossen."
