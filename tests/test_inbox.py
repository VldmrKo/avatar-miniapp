"""Разбор вложения, пришедшего боту в чат.

Раньше здесь проверялся ещё и склад: окно заказывало запись боту, бот
складывал файл, окно его забирало. Этого пути больше нет — свой голос
целиком делается в переписке, — и от модуля осталось ровно то, без чего
не разобрать входящий файл.

А разбирать приходится, и на этом мы уже спотыкались вживую: MAX отдаёт
и голосовое, и видео одинаково — типом `file` и ссылкой `getfile?rq=...`,
без расширения и без подсказок в типе.
"""

from __future__ import annotations

import pytest
from avatar_miniapp.chat import ChatSide, suffix_of
from avatar_miniapp.inbox import VIDEO, VOICE, Attachments


class FakeType:
    """maxapi отдаёт тип перечислением, у которого значение в .value."""

    def __init__(self, value): self.value = value


class FakePayload:
    def __init__(self, url): self.url = url


class FakeAttachment:
    def __init__(self, kind, url):
        self.type = FakeType(kind)
        self.payload = FakePayload(url)


def describe(attachment):
    return ChatSide._describe(None, attachment)


def test_attachment_as_object():
    assert describe(FakeAttachment("audio", "https://x/y/voice.ogg")) == (
        "audio", "https://x/y/voice.ogg")


def test_attachment_as_dict():
    """Разные версии платформы отдают то объект, то словарь."""
    raw = {"type": "video", "payload": {"url": "https://x/clip.mp4"}}
    assert describe(raw) == ("video", "https://x/clip.mp4")


def test_attachment_without_url_is_not_fatal():
    assert describe({"type": "sticker", "payload": {}}) == ("sticker", "")


def test_type_as_plain_string():
    class Plain:
        type = "audio"
        payload = FakePayload("https://x/a.m4a")

    assert describe(Plain()) == ("audio", "https://x/a.m4a")


@pytest.mark.parametrize("url,fallback,expected", [
    ("https://x/y/voice.ogg", ".bin", ".ogg"),
    ("https://x/y/clip.MP4?token=abc", ".bin", ".mp4"),
    ("https://x/y/noext", ".ogg", ".ogg"),
    ("https://x/y/file.verylongextension", ".ogg", ".ogg"),
])
def test_suffix_of(url, fallback, expected):
    assert suffix_of(url, fallback) == expected


# --- опознание содержимого ---------------------------------------------------

def _make(tmp_path, name, args):
    import subprocess

    path = tmp_path / name
    subprocess.run(["ffmpeg", "-y", "-nostdin", "-v", "error", *args, str(path)], check=True)
    return path.read_bytes()


def test_sniff_tells_voice_from_video(tmp_path):
    box = Attachments()
    voice = _make(tmp_path, "v.ogg", ["-f", "lavfi", "-i", "sine=f=220:d=3", "-c:a", "libopus"])
    clip = _make(tmp_path, "c.mp4", [
        "-f", "lavfi", "-i", "testsrc=s=160x120:d=3",
        "-f", "lavfi", "-i", "sine=f=300:d=3",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
    ])
    assert box.sniff(voice, "") == VOICE
    assert box.sniff(clip, "") == VIDEO


def test_sniff_gives_up_quietly_on_garbage():
    """Пустая строка значит «не знаю» — вызывающий решит сам."""
    assert Attachments().sniff(b"not a media file at all", "") == ""


def test_seconds_measures_the_clip(tmp_path):
    """По длительности решается «слишком короткая запись» — до генерации,
    а не после отказа модели."""
    box = Attachments()
    voice = _make(tmp_path, "v.ogg", ["-f", "lavfi", "-i", "sine=f=220:d=3", "-c:a", "libopus"])
    assert 2.5 < box.seconds(voice, ".ogg") < 3.5


def test_seconds_of_garbage_is_zero():
    assert Attachments().seconds(b"not a media file at all", "") == 0.0


def test_audio_from_video(tmp_path):
    """Голосовые до бота не доезжают, а видео доезжает — берём звук оттуда."""
    box = Attachments()
    clip = _make(tmp_path, "c.mp4", [
        "-f", "lavfi", "-i", "testsrc=s=160x120:d=4",
        "-f", "lavfi", "-i", "sine=f=300:d=4",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
    ])
    sound = box.audio_from(clip, ".mp4")
    assert sound[:4] == b"RIFF"
    assert box.sniff(sound, ".wav") == VOICE


def test_audio_from_silent_video_is_empty(tmp_path):
    box = Attachments()
    mute = _make(tmp_path, "s.mp4", [
        "-f", "lavfi", "-i", "testsrc=s=160x120:d=3",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
    ])
    assert box.audio_from(mute, ".mp4") == b""


# --- кто автор события -------------------------------------------------------
# Стоило нам живого бага: у нажатия кнопки заполнены оба поля, и в
# message.sender лежит НЕ нажавший, а отправитель сообщения с кнопкой —
# то есть сам бот. Разговор уезжал под id бота, а следующее сообщение
# человека искало его под настоящим id и не находило.

class _Node:
    def __init__(self, **kids):
        for name, value in kids.items():
            setattr(self, name, value)


BOT, HUMAN = 435474556, 19839514


def test_button_press_is_attributed_to_the_person():
    from avatar_miniapp.chat import user_id_of

    press = _Node(
        callback=_Node(user=_Node(user_id=HUMAN), payload="go:toon"),
        # сообщение с кнопкой отправлял бот — это НЕ автор нажатия
        message=_Node(sender=_Node(user_id=BOT),
                      recipient=_Node(chat_id=416967270)),
    )
    assert user_id_of(press) == HUMAN


def test_plain_message_is_attributed_to_its_sender():
    from avatar_miniapp.chat import user_id_of

    written = _Node(message=_Node(sender=_Node(user_id=HUMAN),
                                  recipient=_Node(chat_id=416967270)))
    assert user_id_of(written) == HUMAN


def test_bot_started_is_attributed_to_the_person():
    from avatar_miniapp.chat import user_id_of

    assert user_id_of(_Node(user=_Node(user_id=HUMAN), chat_id=416967270)) == HUMAN


def test_chat_id_survives_all_three():
    from avatar_miniapp.chat import chat_id_of

    press = _Node(callback=_Node(user=_Node(user_id=HUMAN)),
                  message=_Node(sender=_Node(user_id=BOT),
                                recipient=_Node(chat_id=416967270)))
    assert chat_id_of(press) == 416967270
    assert chat_id_of(_Node(chat_id=416967270)) == 416967270
