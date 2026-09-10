"""Телеграм-часть: перевод с языка мессенджера на язык сценария.

Сам сценарий проверяется в test_dialog и от мессенджера не зависит.
Здесь — только стык: что приехало, кому это отдать и что ответить,
если отдать нельзя.
"""

from __future__ import annotations

import pytest

pytest.importorskip("aiogram")

from avatar_miniapp import tgchat            # noqa: E402
from avatar_miniapp.inbox import VIDEO, VOICE  # noqa: E402

pytestmark = pytest.mark.asyncio

CHAT, USER = 100, 7


class Item:
    def __init__(self, file_id="f1", file_size=1024, duration=0,
                 file_name="", mime_type=""):
        self.file_id = file_id
        self.file_size = file_size
        self.duration = duration
        self.file_name = file_name
        self.mime_type = mime_type


class Msg:
    """Сообщение Telegram в объёме, который нас интересует."""

    def __init__(self, **kinds):
        for name in ("voice", "audio", "video", "video_note", "photo",
                     "document", "text", "caption"):
            setattr(self, name, kinds.get(name))


class Talk:
    def __init__(self, step=""):
        self.step = step
        self.got: list[tuple] = []

    def waiting_for(self, user_id):
        return self.step

    async def on_file(self, chat_id, user_id, kind, data, suffix):
        self.got.append((kind, suffix, len(data)))
        return True

    async def begin(self, chat_id, user_id, greet=True):
        self.got.append(("menu", "", 0))


class Media:
    def __init__(self, secs=4.0, sound=b"RIFFxxxx"):
        self.secs, self.sound = secs, sound

    def seconds(self, data, suffix): return self.secs

    def audio_from(self, data, suffix): return self.sound

    def sniff(self, data, suffix): return VOICE


def _side(talk, media=None, payload=b"x" * 32):
    side = tgchat.TelegramSide.__new__(tgchat.TelegramSide)
    side.conversation = talk
    side.media = media or Media()
    side.said: list[str] = []

    class FakeBot:
        async def send_message(self, chat_id, text, reply_markup=None):
            side.said.append(text)

    side.bot = FakeBot()

    async def download(file_id):
        return payload

    side._download = download
    return side


# --- что именно приехало -----------------------------------------------------
# В MAX это приходилось выяснять по видеодорожке: и голос, и видео он отдавал
# одинаковым `file` без расширения. Telegram говорит сам, и здесь мы просто
# проверяем, что ничего не перепутали.

@pytest.mark.parametrize("message,expected", [
    (Msg(voice=Item(duration=5)), (VOICE, ".ogg")),
    (Msg(audio=Item()), (VOICE, ".m4a")),
    (Msg(video=Item(duration=6)), (VIDEO, ".mp4")),
    (Msg(video_note=Item(duration=6)), (VIDEO, ".mp4")),
    (Msg(photo=[Item(file_id="small"), Item(file_id="big")]), ("photo", ".jpg")),
    (Msg(document=Item(file_name="face.PNG", mime_type="image/png")), ("photo", ".png")),
    (Msg(document=Item(file_name="clip.mov", mime_type="video/quicktime")), (VIDEO, ".mov")),
    (Msg(document=Item(file_name="note.txt", mime_type="text/plain")), ("", "")),
    (Msg(text="просто текст"), ("", "")),
])
async def test_what_came(message, expected):
    kind, _, suffix = tgchat._what_came(message)
    assert (kind, suffix) == expected


async def test_biggest_photo_size_wins():
    """Telegram присылает лесенку размеров; мелкий превью модели не годится."""
    _, item, _ = tgchat._what_came(
        Msg(photo=[Item(file_id="small"), Item(file_id="big")]))
    assert item.file_id == "big"


# --- кому отдать -------------------------------------------------------------

async def test_photo_goes_to_the_conversation():
    talk = Talk(step="photo")
    side = _side(talk)
    assert await side._take_attachment(Msg(photo=[Item()]), CHAT, USER)
    assert talk.got == [("photo", ".jpg", 32)]


async def test_attachment_without_a_conversation_shows_the_menu():
    talk = Talk(step="")
    side = _side(talk)
    assert await side._take_attachment(Msg(photo=[Item()]), CHAT, USER)
    assert talk.got == [("menu", "", 0)]


async def test_plain_text_is_not_an_attachment():
    talk = Talk(step="text")
    side = _side(talk)
    assert not await side._take_attachment(Msg(text="Привет!"), CHAT, USER)
    assert talk.got == []


async def test_voice_message_goes_as_voice():
    """Главная выгода переезда: голосовые доходят как голосовые. В MAX по
    ним прилетало пустое событие, и образец приходилось просить роликом."""
    talk = Talk(step="voice")
    side = _side(talk)
    await side._take_attachment(Msg(voice=Item(duration=5)), CHAT, USER)
    assert talk.got == [(VOICE, ".ogg", 32)]


async def test_clip_on_the_voice_step_gives_up_its_soundtrack():
    talk = Talk(step="voice")
    side = _side(talk, Media(sound=b"RIFF1234"))
    await side._take_attachment(Msg(video=Item(duration=6)), CHAT, USER)
    assert talk.got == [(VOICE, ".wav", 8)]


async def test_short_voice_is_refused_by_telegram_duration():
    """Длительность приходит вместе с файлом — ffprobe для этого не нужен."""
    talk = Talk(step="voice")
    side = _side(talk)
    await side._take_attachment(Msg(voice=Item(duration=1)), CHAT, USER)
    assert talk.got == []
    assert "двух секунд" in side.said[-1]


async def test_too_big_file_is_explained_not_downloaded():
    """20 МБ — предел Bot API, а не наш. Человеку надо сказать словами,
    иначе он будет слать одно и то же видео и не понимать, что не так."""
    talk = Talk(step="video")
    side = _side(talk)
    huge = Msg(video=Item(file_size=40 * 1024 * 1024, duration=7))
    await side._take_attachment(huge, CHAT, USER)
    assert talk.got == []
    assert "20 МБ" in side.said[-1]


async def test_broken_download_says_so():
    talk = Talk(step="photo")
    side = _side(talk)

    async def nothing(file_id):
        return None

    side._download = nothing
    await side._take_attachment(Msg(photo=[Item()]), CHAT, USER)
    assert talk.got == []
    assert "не смог забрать" in side.said[-1].lower()


# --- поверхность -------------------------------------------------------------

async def test_surface_matches_the_max_side():
    """Точка входа собирает бота, не зная, какой мессенджер перед ней.
    Расхождение в именах всплыло бы только на запуске в бою."""
    from avatar_miniapp.chat import ChatSide

    for name in ("ask", "deliver", "say_failed", "start", "stop", "conversation"):
        assert hasattr(tgchat.TelegramSide, name) or name == "conversation", name
        assert hasattr(ChatSide, name) or name == "conversation", name


async def test_ratings_are_the_same_three():
    """Отчёт считает оценки по значкам из журнала. Разойдутся значки —
    разойдётся и статистика, причём молча."""
    from avatar_miniapp.chat import RATINGS as MAX_RATINGS

    assert tgchat.RATINGS == MAX_RATINGS
    assert tgchat.RATING_MARKS == {"good": "👍", "ok": "😐", "bad": "👎"}


# --- почему не подключились --------------------------------------------------
# Сорок строк трассировки про SSL и сорок строк про неверный токен выглядят
# одинаково, а делать надо противоположное. Разбираем до того, как человек
# начнёт читать снизу вверх.

async def test_certificate_failure_names_the_cure():
    why = tgchat._why_no_telegram(
        Exception("ClientConnectorCertificateError: ... CERTIFICATE_VERIFY_FAILED ..."))
    assert "MINIAPP_TRUST_OS_CERTS=1" in why
    assert "truststore" in why
    # И отдельно: не предлагать отключать проверку.
    assert "отключать проверку" in why.lower()


async def test_bad_token_is_not_blamed_on_the_network():
    why = tgchat._why_no_telegram(Exception("Telegram server says - Unauthorized"))
    assert "TELEGRAM_BOT_TOKEN" in why
    assert "сертификат" not in why.lower()


async def test_anything_else_still_says_what_to_try():
    why = tgchat._why_no_telegram(Exception("Cannot connect to host"))
    assert "браузером" in why
