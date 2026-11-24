#!/bin/sh
# Остановить фоновую службу и убрать её из автозапуска.
LABEL="com.klim.booking-bot"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
echo "Бот остановлен и убран из автозапуска."
