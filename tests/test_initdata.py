"""Подпись — единственное, что стоит между нашим ключом и любым с curl.

Проверяем не «работает ли счастливый путь», а что подделки отбиваются.
"""

from __future__ import annotations

import json
import time

import pytest
from avatar_miniapp import initdata

TOKEN = "123:abcdef-test-token"


def make(**over) -> str:
    fields = {
        "auth_date": str(int(time.time())),
        "query_id": "q1",
        "user": json.dumps({"id": 777, "first_name": "Аня"}, ensure_ascii=False),
        "chat": json.dumps({"id": 42, "type": "dialog"}),
    }
    fields.update({k: v for k, v in over.items() if v is not None})
    return initdata.sign(fields, TOKEN)


def test_valid():
    who = initdata.parse(make(), TOKEN)
    assert who.user_id == 777
    assert who.first_name == "Аня"
    assert who.chat_id == 42
    assert who.signed


def test_percent_encoding_survives():
    """parse_qsl раскодирует сам — второй раз кодировать нельзя, иначе не сойдётся."""
    who = initdata.parse(
        make(user=json.dumps({"id": 5, "first_name": "Ан на & Co"}, ensure_ascii=False)), TOKEN
    )
    assert who.first_name == "Ан на & Co"


def test_forged_hash():
    raw = make()
    forged = raw[:-4] + "0000"
    with pytest.raises(initdata.InitDataError):
        initdata.parse(forged, TOKEN)


def test_tampered_user_id():
    """Самая опасная подделка: чужой user_id в остальном валидной строке."""
    raw = make()
    tampered = raw.replace("777", "778")
    with pytest.raises(initdata.InitDataError):
        initdata.parse(tampered, TOKEN)


def test_wrong_token():
    """Токен другого своего бота — подпись не сойдётся. На этом теряют время дважды."""
    with pytest.raises(initdata.InitDataError):
        initdata.parse(make(), "999:another-bot")


def test_expired():
    old = str(int(time.time()) - 90000)
    with pytest.raises(initdata.InitDataError, match="просрочена"):
        initdata.parse(make(auth_date=old), TOKEN)


def test_from_future():
    ahead = str(int(time.time()) + 3600)
    with pytest.raises(initdata.InitDataError, match="будущего"):
        initdata.parse(make(auth_date=ahead), TOKEN)


def test_small_clock_skew_is_fine():
    """Часы клиента убегают на секунды — это не повод отказывать."""
    ahead = str(int(time.time()) + 60)
    assert initdata.parse(make(auth_date=ahead), TOKEN).user_id == 777


def test_no_hash():
    with pytest.raises(initdata.InitDataError, match="hash"):
        initdata.parse("auth_date=1&user=%7B%7D", TOKEN)


def test_no_user():
    fields = {"auth_date": str(int(time.time())), "query_id": "q1"}
    with pytest.raises(initdata.InitDataError, match="user"):
        initdata.parse(initdata.sign(fields, TOKEN), TOKEN)


def test_empty():
    with pytest.raises(initdata.InitDataError):
        initdata.parse("", TOKEN)
