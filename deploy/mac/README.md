# Фоновый запуск на Mac

Папка бота должна лежать **вне** «Документов», «Рабочего стола» и «Загрузок»: macOS не даёт
фоновым службам читать эти папки (ошибка `Operation not permitted`). Подходит `~/bots`.

## Перенос из Документов (один раз)

```bash
sh ~/Documents/booking-bot/deploy/mac/uninstall.sh
mkdir -p ~/bots
mv ~/Documents/booking-bot ~/bots/booking-bot
cd ~/bots/booking-bot
rm -rf .venv
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
sh deploy/mac/install.sh
```

Файл `.env` и база `bookings.db` переезжают вместе с папкой. Окружение `.venv` создаётся заново:
в нём зашиты старые пути.

## Дальше

* логи: `tail -n 30 ~/Library/Logs/booking-bot.log`
* перезапуск после правки `.env`: `sh deploy/mac/install.sh`
* остановка: `sh deploy/mac/uninstall.sh`
