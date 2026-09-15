#!/bin/bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

PYTHON_COMMAND="${PYTHON_COMMAND:-}"
if [ -z "$PYTHON_COMMAND" ]; then
    if command -v python3.14 >/dev/null 2>&1; then
        PYTHON_COMMAND="$(command -v python3.14)"
    elif command -v python3 >/dev/null 2>&1; then
        PYTHON_COMMAND="$(command -v python3)"
    else
        echo "Python 3 не найден. Установите Python 3.11 или новее."
        exit 1
    fi
fi

if [ ! -x "venv/bin/python" ]; then
    "$PYTHON_COMMAND" -m venv venv
fi

venv/bin/python -m pip install --upgrade pip
venv/bin/python -m pip install -r requirements.txt

if command -v npm >/dev/null 2>&1 && [ -f "frontend/package.json" ]; then
    echo "Собираю современный интерфейс Quiz Studio…"
    (cd frontend && npm install && npm run build)
else
    echo "Node.js не найден — используется совместимый встроенный редактор."
fi

echo
echo "Готово. Зависимости установлены."
echo "Теперь дважды нажмите start.command или выполните ./start.command"
