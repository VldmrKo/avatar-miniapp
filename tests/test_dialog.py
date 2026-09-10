"""Сценарий в переписке целиком, без maxapi и без сети.

Смысл этих тестов: путь через бота — это теперь ОСНОВНОЙ путь для тех,
кому нужен свой голос, а проверить его руками стоит десяти сообщений
в мессенджере на каждую правку. Здесь весь диалог проходится за
миллисекунды, и видно ровно то, что увидит человек.
"""

from __future__ import annotations

import pytest
from avatar_miniapp import dialog

pytestmark = pytest.mark.asyncio

CHAT = 100
USER = 7


class Fake:
    """Подставка вместо мессенджера, диска и очереди задач."""

    def __init__(self, tmp_path, voices=None, busy=False):
        self.said: list[tuple[str, list]] = []
        self.jobs: list[tuple[str, str, dict]] = []
        self.busy = busy
        self.tmp = tmp_path
        self._voices = voices if voices is not None else [
            {"id": "anya", "title": "Аня", "note": ""},
        ]
        self.talk = dialog.Conversation(
            dialog.Store(tmp_path / "dialogs"),
            send=self.send,
            save_file=self.save_file,
            start_job=self.start_job,
            voices=lambda: self._voices,
            estimate=lambda text: len(text.split()) * 0.45,
            toon_enabled=True,
        )

    async def send(self, chat_id, text, buttons):
        self.said.append((text, buttons))

    def save_file(self, user_id, kind, data, suffix):
        path = self.tmp / f"{user_id}_{kind}{suffix}"
        path.write_bytes(data)
        return path

    def start_job(self, user_id, mode, text, inputs):
        if self.busy:
            raise RuntimeError("уже есть активная задача")
        self.jobs.append((mode, text, inputs))
        return object()

    # --- удобства для проверок ---

    @property
    def last(self) -> str:
        return self.said[-1][0]

    @property
    def last_buttons(self) -> list[str]:
        return [payload for _, payload in self.said[-1][1]]

    def button_titled(self, part: str) -> str:
        for title, payload in self.said[-1][1]:
            if part.lower() in title.lower():
                return payload
        raise AssertionError(f"нет кнопки со словом {part!r}: {self.said[-1][1]}")


# --- меню --------------------------------------------------------------------

async def test_menu_offers_three_modes(tmp_path):
    fake = Fake(tmp_path)
    await fake.talk.begin(CHAT, USER)
    assert fake.last_buttons == ["go:toon", "go:photo", "go:video"]


async def test_without_kandinsky_no_toon(tmp_path):
    """Кнопки не должно быть, если рисовать нечем: она бы привела к отказу."""
    fake = Fake(tmp_path)
    fake.talk.toon_enabled = False
    await fake.talk.begin(CHAT, USER)
    assert "go:toon" not in fake.last_buttons


# --- полный путь -------------------------------------------------------------

async def test_photo_avatar_end_to_end(tmp_path):
    fake = Fake(tmp_path)
    await fake.talk.begin(CHAT, USER)
    await fake.talk.on_button(CHAT, USER, "go:photo")
    assert "фото лица" in fake.last

    await fake.talk.on_file(CHAT, USER, "photo", b"jpegdata", ".jpg")
    assert "голос" in fake.last.lower()

    await fake.talk.on_button(CHAT, USER, fake.button_titled("Аня"))
    assert "сказать" in fake.last.lower()

    await fake.talk.on_text(CHAT, USER, "Привет! Это мой аватар, и он говорит моим голосом.")
    assert len(fake.jobs) == 1
    mode, text, inputs = fake.jobs[0]
    assert mode == "photo"
    assert text.startswith("Привет!")
    assert inputs["voice_id"] == "anya"
    assert inputs["photo"].endswith(".jpg")
    # Разговор закончен: следующее сообщение не должно попасть в него.
    assert fake.talk.waiting_for(USER) == ""


async def test_toon_with_own_voice(tmp_path):
    """Главный сценарий ради которого всё затевалось: свой голос без окна."""
    fake = Fake(tmp_path)
    await fake.talk.on_button(CHAT, USER, "go:toon")
    await fake.talk.on_file(CHAT, USER, "photo", b"jpeg", ".jpg")
    await fake.talk.on_button(CHAT, USER, "voice:own")
    # Инструкция обязана содержать готовую фразу: без неё человек застревает
    # на вопросе «а что говорить».
    assert "образец голоса для аватара" in fake.last

    await fake.talk.on_file(CHAT, USER, "voice", b"wav", ".wav")
    assert "сказать" in fake.last.lower()

    await fake.talk.on_text(CHAT, USER, "Ого, вот это правда здорово получилось!")
    mode, _, inputs = fake.jobs[0]
    assert mode == "toon"
    assert inputs["voice"].endswith(".wav")
    assert "voice_id" not in inputs


async def test_video_mode_skips_voice_step(tmp_path):
    fake = Fake(tmp_path)
    await fake.talk.on_button(CHAT, USER, "go:video")
    assert "видео" in fake.last.lower()
    await fake.talk.on_file(CHAT, USER, "video", b"mp4", ".mp4")
    # Голос берётся из того же видео — отдельного шага быть не должно.
    assert "сказать" in fake.last.lower()
    await fake.talk.on_text(CHAT, USER, "Проверка связи, раз два три.")
    mode, _, inputs = fake.jobs[0]
    assert mode == "video"
    assert set(inputs) == {"video"}


# --- что человек делает не так -----------------------------------------------

async def test_wrong_attachment_is_explained(tmp_path):
    """Прислали видео там, где ждали фото. Молчать нельзя: человек решит,
    что бот сломался, и уйдёт."""
    fake = Fake(tmp_path)
    await fake.talk.on_button(CHAT, USER, "go:photo")
    await fake.talk.on_file(CHAT, USER, "video", b"mp4", ".mp4")
    assert "нужно фото" in fake.last.lower()
    assert fake.talk.waiting_for(USER) == "photo"


async def test_too_long_text_says_how_much_to_cut(tmp_path):
    fake = Fake(tmp_path)
    await fake.talk.on_button(CHAT, USER, "go:video")
    await fake.talk.on_file(CHAT, USER, "video", b"mp4", ".mp4")
    await fake.talk.on_text(CHAT, USER, " ".join(["слово"] * 60))
    assert not fake.jobs
    assert "сократите" in fake.last.lower()
    # Шаг не потерян: человек пришлёт текст короче, и всё поедет дальше.
    assert fake.talk.waiting_for(USER) == "text"
    await fake.talk.on_text(CHAT, USER, "Короткая реплика для проверки, вот такая.")
    assert len(fake.jobs) == 1


async def test_short_text_warns_but_goes(tmp_path):
    """Короткое запрещать нельзя — ролик выйдет. Но предупредить надо:
    модель дозаполняет незанятое время сама."""
    fake = Fake(tmp_path)
    await fake.talk.on_button(CHAT, USER, "go:video")
    await fake.talk.on_file(CHAT, USER, "video", b"mp4", ".mp4")
    await fake.talk.on_text(CHAT, USER, "Привет")
    assert len(fake.jobs) == 1
    assert "короткая" in fake.last.lower()


async def test_busy_keeps_the_step(tmp_path):
    """Занятая очередь не должна съедать набранный текст."""
    fake = Fake(tmp_path, busy=True)
    await fake.talk.on_button(CHAT, USER, "go:video")
    await fake.talk.on_file(CHAT, USER, "video", b"mp4", ".mp4")
    await fake.talk.on_text(CHAT, USER, "Проверка связи, раз два три.")
    assert not fake.jobs
    assert "уже готовится" in fake.last
    assert fake.talk.waiting_for(USER) == "text"


async def test_cancel_returns_to_menu(tmp_path):
    fake = Fake(tmp_path)
    await fake.talk.on_button(CHAT, USER, "go:photo")
    await fake.talk.on_button(CHAT, USER, "cancel")
    assert fake.talk.waiting_for(USER) == ""
    assert "go:photo" in fake.last_buttons


async def test_text_outside_the_flow_is_not_swallowed(tmp_path):
    """Пока текста не ждут, сообщение не наше — чат-часть покажет меню."""
    fake = Fake(tmp_path)
    assert await fake.talk.on_text(CHAT, USER, "привет") is False


async def test_state_survives_restart(tmp_path):
    """Между шагами процесс может перезапуститься: выкладка идёт в любой
    момент, а человек не должен начинать сначала."""
    first = Fake(tmp_path)
    await first.talk.on_button(CHAT, USER, "go:photo")
    await first.talk.on_file(CHAT, USER, "photo", b"jpeg", ".jpg")

    second = Fake(tmp_path)          # новый процесс, то же хранилище
    assert second.talk.waiting_for(USER) == "voice"
    await second.talk.on_button(CHAT, USER, "voice:own")
    await second.talk.on_file(CHAT, USER, "voice", b"wav", ".wav")
    await second.talk.on_text(CHAT, USER, "Проверка после перезапуска, всё цело.")
    assert second.jobs[0][0] == "photo"


async def test_no_presets_still_offers_own_voice(tmp_path):
    fake = Fake(tmp_path, voices=[])
    await fake.talk.on_button(CHAT, USER, "go:photo")
    await fake.talk.on_file(CHAT, USER, "photo", b"jpeg", ".jpg")
    assert "voice:own" in fake.last_buttons
    assert "Готовых голосов сейчас нет" in fake.last


# --- кто получает вложение ---------------------------------------------------
# Самое опасное место интеграции: одно и то же видео может быть ответом
# сценарию в переписке ИЛИ записью для окна мини-аппа. Перепутать — значит
# у человека, нажавшего в окне «записать», молча уедет ролик не туда.

class Recorder:
    """Сценарий-пустышка: только запоминает, что ему отдали."""

    def __init__(self, step=""):
        self.step = step
        self.got: list[tuple] = []

    def waiting_for(self, user_id):
        return self.step

    async def on_file(self, chat_id, user_id, kind, data, suffix):
        self.got.append((kind, suffix, len(data)))

    async def on_text(self, chat_id, user_id, text):
        return False

    async def begin(self, chat_id, user_id, greet=True):
        self.got.append(("menu", "", 0))


def _chat(tmp_path, conversation):
    from avatar_miniapp.chat import ChatSide
    from avatar_miniapp.inbox import Inbox

    chat = ChatSide.__new__(ChatSide)
    chat.inbox = Inbox(tmp_path / "inbox")
    chat.conversation = conversation
    chat.webapp_url = ""
    chat.username = ""
    chat._good_way = ""
    chat._state_path = None
    chat.said = []

    async def download(url):
        return b"x" * 32

    async def send(chat_id, text, open_app=""):
        chat.said.append(text)

    chat._download = download
    chat._send = send
    return chat


async def test_photo_goes_to_the_conversation(tmp_path):
    talk = Recorder(step="photo")
    chat = _chat(tmp_path, talk)
    await chat._intake(CHAT, USER, [{"type": "image", "payload": {"url": "http://x/a.jpg"}}])
    assert talk.got == [("photo", ".jpg", 32)]


async def test_photo_without_conversation_points_at_the_window(tmp_path):
    """Сценарий не начат — значит фото прислали «просто так», и это
    по-прежнему история про окно."""
    talk = Recorder(step="")
    chat = _chat(tmp_path, talk)
    await chat._intake(CHAT, USER, [{"type": "image", "payload": {"url": "http://x/a.jpg"}}])
    assert talk.got == []
    assert chat.said and "приложении" in chat.said[-1]


async def test_window_wins_when_it_asked_first(tmp_path):
    """Человек нажал в окне «записать» и ушёл в чат. Даже если у него
    висит незаконченный разговор, эта запись — для окна."""
    talk = Recorder(step="voice")
    chat = _chat(tmp_path, talk)
    chat.inbox.arm(USER, "video", "video")
    await chat._intake(CHAT, USER, [{"type": "video", "payload": {"url": "http://x/v.mp4"}}])
    assert talk.got == []
    assert chat.inbox.get(USER, "video") is not None


async def test_conversation_wins_when_the_window_is_silent(tmp_path):
    talk = Recorder(step="video")
    chat = _chat(tmp_path, talk)
    await chat._intake(CHAT, USER, [{"type": "video", "payload": {"url": "http://x/v.mp4"}}])
    assert talk.got == [("video", ".mp4", 32)]
    assert chat.inbox.get(USER, "video") is None
