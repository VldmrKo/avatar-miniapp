"""Проверка подписи initData.

Без неё эндпоинт открыт любому с curl, а каждый чужой запрос — это наша
генерация на нашем ключе. Проверяется на КАЖДОМ запросе: сессий тут нет
и не нужно, клиент просто шлёт ту же подписанную строку заголовком.

Алгоритм: dev.max.ru/docs/webapps/validation
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl

HEADER = "X-Init-Data"


class InitDataError(Exception):
    """Подпись не сошлась или данные негодные. Наружу отдаём 401 без подробностей."""


@dataclass(frozen=True)
class Caller:
    user_id: int
    first_name: str = ""
    chat_id: int | None = None
    start_param: str = ""
    signed: bool = True

    @property
    def display_name(self) -> str:
        return self.first_name or f"id{self.user_id}"


def parse(
    init_data: str,
    bot_token: str,
    *,
    max_age_s: int = 86400,
    future_skew_s: int = 300,
    now: float | None = None,
) -> Caller:
    if not init_data:
        raise InitDataError("пустой initData")

    # parse_qsl уже раскодирует percent-encoding — это и есть шаг 1 алгоритма.
    # Раскодировать второй раз значит гарантированно не сойтись.
    fields = dict(parse_qsl(init_data, keep_blank_values=True))
    received = fields.pop("hash", "")
    if not received:
        raise InitDataError("в initData нет hash")

    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    # compare_digest, а не ==: обычное сравнение строк отвечает за разное время
    # и по этому времени подпись подбирается.
    if not hmac.compare_digest(expected, received):
        raise InitDataError("подпись не совпала")

    moment = time.time() if now is None else now
    try:
        auth_date = int(fields.get("auth_date", "0"))
    except ValueError as exc:
        raise InitDataError("auth_date не число") from exc
    if auth_date <= 0:
        raise InitDataError("в initData нет auth_date")
    if moment - auth_date > max_age_s:
        raise InitDataError("подпись просрочена")
    # Часы клиента иногда убегают вперёд, но не на часы.
    if auth_date - moment > future_skew_s:
        raise InitDataError("auth_date из будущего")

    try:
        user = json.loads(fields.get("user") or "{}")
    except json.JSONDecodeError as exc:
        raise InitDataError("поле user не разбирается") from exc
    user_id = user.get("id")
    if not isinstance(user_id, int):
        raise InitDataError("в initData нет user.id")

    chat_id = None
    try:
        chat = json.loads(fields.get("chat") or "{}")
        if isinstance(chat.get("id"), int):
            chat_id = chat["id"]
    except json.JSONDecodeError:
        chat_id = None

    return Caller(
        user_id=user_id,
        first_name=str(user.get("first_name") or ""),
        chat_id=chat_id,
        start_param=fields.get("start_param", ""),
    )


def sign(fields: dict[str, str], bot_token: str) -> str:
    """Собрать подписанную строку. Нужно только тестам — в бою подписывает MAX."""
    check = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    digest = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    from urllib.parse import urlencode

    return urlencode({**fields, "hash": digest})
