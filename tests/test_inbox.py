"""Приём голоса и видео из чата.

Окну MAX не даёт ни микрофон, ни камеру в режиме видео, поэтому запись
идёт через сам мессенджер. Здесь проверяется разбор вложения (на нём мы
уже один раз споткнулись вживую) и жизнь записи после приёма.
"""

from __future__ import annotations

import json
import time

import pytest
from avatar_miniapp.chat import ChatSide, suffix_of
from avatar_miniapp.inbox import VIDEO, VOICE, Inbox


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


def test_put_and_get(tmp_path):
    box = Inbox(tmp_path)
    item = box.put(7, VOICE, b"x" * 1000, ".ogg")
    assert item.path.is_file()
    again = box.get(7, VOICE)
    assert again is not None and again.path == item.path


def test_new_recording_replaces_the_old(tmp_path):
    """Один последний файл на вид: копить чужие записи незачем."""
    box = Inbox(tmp_path)
    box.put(7, VOICE, b"a" * 100, ".ogg")
    box.put(7, VOICE, b"b" * 100, ".m4a")
    files = sorted(p.name for p in (tmp_path / "7").glob("voice.*"))
    assert files == ["voice.m4a"]
    assert box.get(7, VOICE).path.read_bytes() == b"b" * 100


def test_stale_recording_is_not_offered(tmp_path):
    """Вчерашняя запись — почти наверняка не та, что имеют в виду сегодня."""
    box = Inbox(tmp_path)
    box.put(7, VOICE, b"x" * 100, ".ogg")
    meta_path = tmp_path / "7" / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta[VOICE]["at"] = time.time() - 48 * 3600
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    assert box.get(7, VOICE) is None


def test_people_do_not_see_each_other(tmp_path):
    box = Inbox(tmp_path)
    box.put(7, VOICE, b"x" * 100, ".ogg")
    assert box.get(8, VOICE) is None


def test_expect_is_remembered_and_cleared(tmp_path):
    box = Inbox(tmp_path)
    assert box.expected(7) == ""
    box.arm(7, VIDEO)
    assert box.expected(7) == VIDEO
    box.disarm(7)
    assert box.expected(7) == ""


def test_expect_does_not_outlive_the_intent(tmp_path):
    box = Inbox(tmp_path)
    box.arm(7, VOICE)
    meta_path = tmp_path / "7" / "meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["expect"]["at"] = time.time() - 7200
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    assert box.expected(7) == ""


def test_clear_removes_the_file(tmp_path):
    box = Inbox(tmp_path)
    item = box.put(7, VOICE, b"x" * 100, ".ogg")
    box.clear(7, VOICE)
    assert not item.path.exists()
    assert box.get(7, VOICE) is None


def test_public_shape(tmp_path):
    box = Inbox(tmp_path)
    box.put(7, VOICE, b"x" * 100, ".ogg")
    body = box.public(7)
    assert body["video"] is None
    assert body["voice"]["name"] == "voice.ogg"
    assert "expect" in body


# --- опознание содержимого ---------------------------------------------------
# MAX отдаёт и голосовое, и видео как `file` со ссылкой getfile?rq=... —
# ни расширения, ни подсказки в типе. Гадали по имени и ошибались.

def _make(tmp_path, name, args):
    import subprocess

    path = tmp_path / name
    subprocess.run(["ffmpeg", "-y", "-nostdin", "-v", "error", *args, str(path)], check=True)
    return path.read_bytes()


def test_sniff_tells_voice_from_video(tmp_path):
    box = Inbox(tmp_path / "box")
    voice = _make(tmp_path, "v.ogg", ["-f", "lavfi", "-i", "sine=f=220:d=3", "-c:a", "libopus"])
    clip = _make(tmp_path, "c.mp4", [
        "-f", "lavfi", "-i", "testsrc=s=160x120:d=3",
        "-f", "lavfi", "-i", "sine=f=300:d=3",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
    ])
    assert box.sniff(voice, "") == VOICE
    assert box.sniff(clip, "") == VIDEO


def test_sniff_gives_up_quietly_on_garbage(tmp_path):
    """Пустая строка значит «не знаю» — вызывающий решит сам."""
    box = Inbox(tmp_path / "box")
    assert box.sniff(b"not a media file at all", "") == ""


def test_audio_from_video(tmp_path):
    """Голосовые до бота не доезжают, а видео доезжает — берём звук оттуда."""
    box = Inbox(tmp_path / "box")
    clip = _make(tmp_path, "c.mp4", [
        "-f", "lavfi", "-i", "testsrc=s=160x120:d=4",
        "-f", "lavfi", "-i", "sine=f=300:d=4",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
    ])
    sound = box.audio_from(clip, ".mp4")
    assert sound[:4] == b"RIFF"
    assert box.sniff(sound, ".wav") == VOICE


def test_audio_from_silent_video_is_empty(tmp_path):
    box = Inbox(tmp_path / "box")
    mute = _make(tmp_path, "s.mp4", [
        "-f", "lavfi", "-i", "testsrc=s=160x120:d=3",
        "-c:v", "libx264", "-pix_fmt", "yuv420p",
    ])
    assert box.audio_from(mute, ".mp4") == b""
