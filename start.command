#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

if [ ! -x "venv/bin/python" ]; then
    echo "Сначала устанавливаю зависимости…"
    "$PROJECT_DIR/setup.command"
fi

if [ -f ".env" ]; then
    set -a
    source ".env"
    set +a
fi

QUIZ_BIND_HOST="${QUIZ_HOST:-0.0.0.0}"
QUIZ_BIND_PORT="${QUIZ_PORT:-8000}"
EDITOR_ACCESS_TOKEN="$(venv/bin/python -c 'import main; print(main.EDITOR_TOKEN)')"
LOCAL_ADDRESS="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || true)"

echo
echo "Kahoot Clone запускается"
echo "Ведущий:   http://127.0.0.1:${QUIZ_BIND_PORT}/host.html"
echo "Игрок:     http://127.0.0.1:${QUIZ_BIND_PORT}/player.html"
echo "Админ:     http://127.0.0.1:${QUIZ_BIND_PORT}/admin.html#${EDITOR_ACCESS_TOKEN}"
echo "Редактор:  http://127.0.0.1:${QUIZ_BIND_PORT}/editor.html#${EDITOR_ACCESS_TOKEN}"
if [ -n "$LOCAL_ADDRESS" ]; then
    echo "В локальной сети: http://${LOCAL_ADDRESS}:${QUIZ_BIND_PORT}/player.html"
    echo "Редактор в сети:  http://${LOCAL_ADDRESS}:${QUIZ_BIND_PORT}/editor.html#${EDITOR_ACCESS_TOKEN}"
fi
echo
echo "Остановить сервер: Ctrl+C"
echo

exec venv/bin/python -m uvicorn main:app --host "$QUIZ_BIND_HOST" --port "$QUIZ_BIND_PORT"
