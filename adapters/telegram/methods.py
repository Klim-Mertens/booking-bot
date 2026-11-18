"""Методы Bot API 10.4 для виджетов, которых ещё нет в aiogram 3.31.

aiogram позволяет описать свой метод: класс с именем метода API и полями.
Когда библиотека добавит их официально, этот файл можно будет удалить.
"""
from __future__ import annotations

from typing import Union

from aiogram.methods.base import TelegramMethod
from aiogram.types import Message


class SendWidget(TelegramMethod[Message]):
    __returning__ = Message
    __api_method__ = "sendWidget"

    chat_id: int | str
    html: str
    caption: str | None = None
    chat_data: str | None = None
    public_data: str | None = None
    allow_queries: bool | None = None
    width: int | None = None
    height: int | None = None


class AnswerWidgetQuery(TelegramMethod[bool]):
    __returning__ = bool
    __api_method__ = "answerWidgetQuery"

    widget_query_id: str
    data: str | None = None


class EditMessageWidgetData(TelegramMethod[Union[Message, bool]]):
    __returning__ = Union[Message, bool]
    __api_method__ = "editMessageWidgetData"

    chat_id: int | str
    message_id: int
    chat_data: str | None = None
