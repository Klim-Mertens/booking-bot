"""Диагностика виджетов Telegram: пробует отправить виджет несколькими способами
и показывает, какие Telegram принимает, а какие отклоняет (и с какой ошибкой).

Запуск (токен берётся из .env, в вывод он не попадает):
    set -a && source .env && set +a
    python3 tools/widget_probe.py ВАШ_ЧИСЛОВОЙ_ID
Свой ID — из ADMIN_IDS в .env. Сначала напишите боту /start, иначе он не сможет вам писать.
"""
from __future__ import annotations

import json
import os
import ssl
import sys
import urllib.error
import urllib.request
from pathlib import Path

import certifi

ROOT = Path(__file__).resolve().parents[1]
REAL_HTML = (ROOT / "widgets" / "calendar.html").read_text("utf-8")
TINY_HTML = "<div id=a>hello</div><script>document.getElementById('a').textContent='ok'</script>"
CTX = ssl.create_default_context(cafile=certifi.where())


def call(token: str, method: str, params: dict) -> str:
    req = urllib.request.Request(f"https://api.telegram.org/bot{token}/{method}",
                                 data=json.dumps(params).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20, context=CTX) as r:   # noqa: S310
            body = json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = json.loads(e.read() or b"{}")
    return "ПРИНЯТ" if body.get("ok") else f"ОТКЛОНЁН: {body.get('description')}"


def main() -> None:
    token = os.environ.get("TG_TOKEN", "")
    if not token or len(sys.argv) != 2 or not sys.argv[1].isdigit():
        raise SystemExit("Использование: python3 tools/widget_probe.py ЧИСЛОВОЙ_ID (и TG_TOKEN в окружении)")
    chat = int(sys.argv[1])
    state = json.dumps({"t": "тест", "d": []}, ensure_ascii=False)
    cases = [
        ("1. минимальный виджет", {"html": TINY_HTML}),
        ("2. минимальный + allow_queries", {"html": TINY_HTML, "allow_queries": True}),
        ("3. наш календарь без состояния", {"html": REAL_HTML}),
        ("4. наш календарь + chat_data", {"html": REAL_HTML, "chat_data": state}),
        ("5. наш календарь + chat_data + allow_queries", {"html": REAL_HTML, "chat_data": state, "allow_queries": True}),
    ]
    for name, extra in cases:
        print(f"{name}: {call(token, 'sendWidget', {'chat_id': chat, **extra})}")


if __name__ == "__main__":
    main()
