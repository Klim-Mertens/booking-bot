#!/bin/sh
# Установка бота как фоновой службы на Mac: работает без окна Терминала,
# запускается при входе в систему и сам перезапускается, если упал.
# Повторный запуск этого скрипта = перезапуск бота (например, после правки .env).
set -e
DIR="$(cd "$(dirname "$0")/../.." && pwd)"
LABEL="com.klim.booking-bot"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOG="$HOME/Library/Logs/booking-bot.log"

case "$DIR" in
  "$HOME"/Documents/*|"$HOME"/Desktop/*|"$HOME"/Downloads/*)
    echo "Папка бота лежит в «Документах/Рабочем столе/Загрузках». macOS не даёт фоновым службам читать эти папки"
    echo "(ошибка «Operation not permitted»). Перенесите бота в ~/bots, команды в deploy/mac/README.md."
    exit 1;;
esac

[ -f "$DIR/.env" ] || { echo "Нет файла .env в $DIR. Сначала заполните его (README, шаг «Запуск»)."; exit 1; }
[ -x "$DIR/.venv/bin/python3" ] || { echo "Нет окружения .venv. Выполните: python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"; exit 1; }

mkdir -p "$HOME/Library/LaunchAgents" "$HOME/Library/Logs"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/caffeinate</string>
    <string>-is</string>
    <string>$DIR/start.sh</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>$LOG</string>
  <key>StandardErrorPath</key><string>$LOG</string>
</dict>
</plist>
PLISTEOF
chmod 644 "$PLIST"

UIDN="$(id -u)"
launchctl bootout "gui/$UIDN/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$UIDN" "$PLIST"
launchctl enable "gui/$UIDN/$LABEL"
echo "Готово: бот запущен в фоне и будет стартовать сам при входе в систему."
echo "Логи:      tail -n 30 $LOG"
echo "Остановка: sh $DIR/deploy/mac/uninstall.sh"
echo "Важно: Mac должен быть включён. caffeinate не даёт ему засыпать от простоя,"
echo "но если закрыть крышку ноутбука, он уснёт, и бот замолчит."
