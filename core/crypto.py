"""Защита персональных данных (телефонов) в базе.

- Шифрование: Fernet из библиотеки `cryptography` (AES-128-CBC + HMAC-SHA256).
  Даже если файл базы утечёт, телефоны без ключа не прочитать.
- Поиск: зашифрованный текст каждый раз разный (случайный IV), поэтому
  для поиска «есть ли уже такой клиент» храним отдельный HMAC-хэш
  с другим ключом. Обычный sha256 тут не годится: телефонов мало,
  их легко перебрать по словарю, а HMAC без ключа не посчитать.

Ключи берутся только из переменных окружения, никогда из кода.
"""
from __future__ import annotations

import hashlib
import hmac
import re

from cryptography.fernet import Fernet, InvalidToken

_PHONE_RE = re.compile(r"^\+?\d{10,15}$")


def normalize_phone(raw: str) -> str:
    """Привести телефон к виду +79991234567 или выбросить ValueError.

    Проверяем вход строго: это данные от пользователя, им нельзя доверять.
    """
    digits = re.sub(r"[\s\-()]", "", raw)
    if digits.startswith("8") and len(digits) == 11:
        digits = "+7" + digits[1:]
    if not _PHONE_RE.fullmatch(digits):
        raise ValueError("Неверный формат телефона")
    return digits if digits.startswith("+") else "+" + digits


class PiiCipher:
    def __init__(self, enc_key: bytes, hmac_key: bytes) -> None:
        if len(hmac_key) < 32:
            raise ValueError("HMAC-ключ должен быть не короче 32 байт")
        self._fernet = Fernet(enc_key)
        self._hmac_key = hmac_key

    def encrypt(self, value: str) -> bytes:
        return self._fernet.encrypt(value.encode())

    def decrypt(self, token: bytes) -> str:
        try:
            return self._fernet.decrypt(token).decode()
        except InvalidToken as exc:  # подменённые данные или чужой ключ
            raise ValueError("Не удалось расшифровать данные") from exc

    def lookup_hash(self, value: str) -> str:
        return hmac.new(self._hmac_key, value.encode(), hashlib.sha256).hexdigest()
