#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

EXPECTED_HTTPS_REMOTE="https://github.com/ArvidsGertners/duality-bot.git"
EXPECTED_SSH_REMOTE="git@github.com:ArvidsGertners/duality-bot.git"
FILES=(main.py requirements.txt .gitignore .python-version update-mac.sh update-vps.sh README.md PANEL_PROMPT.md PROGRESS.md panel.py panel_static tests)

if ! command -v git >/dev/null 2>&1; then
    echo "Fehler: Git wurde nicht gefunden."
    exit 1
fi

if ! remote_url="$(git remote get-url origin 2>/dev/null)"; then
    echo "Fehler: Es gibt noch kein Git-Remote 'origin'."
    echo "Füge es zuerst mit 'git remote add origin $EXPECTED_HTTPS_REMOTE' hinzu."
    exit 1
fi

case "$remote_url" in
    "$EXPECTED_HTTPS_REMOTE"|"${EXPECTED_HTTPS_REMOTE%.git}"|"$EXPECTED_SSH_REMOTE") ;;
    *)
        echo "Fehler: origin zeigt auf ein anderes Repo: $remote_url"
        exit 1
        ;;
esac

if [[ "$(git branch --show-current)" != "main" ]]; then
    echo "Fehler: Der aktuelle Branch ist nicht 'main'."
    exit 1
fi

for file in "${FILES[@]}"; do
    if [[ ! -e "$file" ]]; then
        echo "Fehler: Datei fehlt: $PROJECT_DIR/$file"
        exit 1
    fi
done

git add -- "${FILES[@]}"

if git diff --cached --quiet -- "${FILES[@]}"; then
    echo "Keine Änderungen an den Bot-Dateien; pushe vorhandene Commits."
else
    echo "Diese Bot-Dateien werden committet:"
    git diff --cached --name-only -- "${FILES[@]}"
    commit_message="${1:-}"
    if [[ -z "$commit_message" ]]; then
        read -r -p "Commit-Nachricht [Update bot]: " commit_message
        commit_message="${commit_message:-Update bot}"
    fi
    git commit -m "$commit_message" -- "${FILES[@]}"
fi

git push origin main
echo "GitHub wurde aktualisiert."
