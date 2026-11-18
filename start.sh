#!/bin/sh
# Запуск обоих ботов одной командой: ./start.sh
cd "$(dirname "$0")" || exit 1
. .venv/bin/activate
set -a; . ./.env; set +a
exec python3 run_all.py
