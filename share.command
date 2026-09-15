#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

if ! command -v ngrok >/dev/null 2>&1; then
    echo "ngrok не найден. Сначала установите ngrok или используйте ссылку локальной сети из start.command."
    exit 1
fi
if [ ! -x "venv/bin/python" ]; then
    echo "Сначала запустите setup.command."
    exit 1
fi
if [ -f ".env" ]; then
    set -a
    source ".env"
    set +a
fi

QUIZ_BIND_PORT="${QUIZ_PORT:-8000}"
EDITOR_ACCESS_TOKEN="$(venv/bin/python -c 'import main; print(main.EDITOR_TOKEN)')"

echo "Создаю временную публичную ссылку для порта ${QUIZ_BIND_PORT}…"
echo "Сервер должен быть запущен через start.command."
echo

ngrok http "$QUIZ_BIND_PORT" --log=stdout 2>&1 | while IFS= read -r NGROK_LINE; do
    case "$NGROK_LINE" in
        *"started tunnel"*"url=https://"*)
            PUBLIC_BASE="${NGROK_LINE##*url=}"
            PUBLIC_BASE="${PUBLIC_BASE%% *}"
            PUBLIC_BASE="${PUBLIC_BASE%\"}"
            echo "Публичные ссылки готовы:"
            echo "Ведущий:  ${PUBLIC_BASE}/host.html"
            echo "Игрок:    ${PUBLIC_BASE}/player.html"
            echo "Админ:    ${PUBLIC_BASE}/admin.html#${EDITOR_ACCESS_TOKEN}"
            echo "Редактор: ${PUBLIC_BASE}/editor.html#${EDITOR_ACCESS_TOKEN}"
            echo
            echo "Ссылки работают, пока открыты start.command и share.command."
            ;;
    esac
done
