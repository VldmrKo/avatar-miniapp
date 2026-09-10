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


async def test_long_text_warns_but_goes(tmp_path):
    """Раньше длинный текст отклоняли и требовали сократить. Люди начали
    считать слова и ругаться — справедливо: это наша арифметика, не их
    работа. Теперь делаем как просят и говорим, чем это грозит."""
    fake = Fake(tmp_path)
    await fake.talk.on_button(CHAT, USER, "go:video")
    await fake.talk.on_file(CHAT, USER, "video", b"mp4", ".mp4")
    await fake.talk.on_text(CHAT, USER, " ".join(["слово"] * 60))
    assert len(fake.jobs) == 1
    assert "сократите" not in fake.last.lower()
    assert "смазаться" in fake.last.lower(), fake.last
    # Разговор закончен так же, как на обычной реплике.
    assert fake.talk.waiting_for(USER) == ""


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


# --- что человек читает ------------------------------------------------------
# Тексты правились по живым замечаниям: обещание «меньше минуты» не
# сбывалось из-за очереди, а про нижнюю границу в пять секунд человек
# узнавал только постфактум, когда ролик уже выходил кривым.

async def test_text_step_names_both_borders(tmp_path):
    fake = Fake(tmp_path)
    await fake.talk.on_button(CHAT, USER, "go:photo")
    await fake.talk.on_file(CHAT, USER, "photo", b"jpegdata", ".jpg")
    await fake.talk.on_button(CHAT, USER, fake.button_titled("Аня"))
    said = fake.last
    assert f"{dialog.WORDS_FOR_FLOOR} слов" in said, said
    assert "менее стабильными" in said, said
    assert f"{dialog.WORDS_FOR_LIMIT} слов" in said, said


async def test_waiting_promise_is_one_and_vague(tmp_path):
    """Одно обещание на все режимы: «меньше минуты», сорванное очередью,
    человек читает как поломку."""
    fake = Fake(tmp_path)
    await fake.talk.on_button(CHAT, USER, "go:toon")
    await fake.talk.on_file(CHAT, USER, "photo", b"jpegdata", ".jpg")
    await fake.talk.on_button(CHAT, USER, fake.button_titled("Аня"))
    await fake.talk.on_text(CHAT, USER, "Привет! Это мой аватар, и он говорит моим голосом.")
    assert "пару минут" in fake.last, fake.last
    assert "меньше минуты" not in fake.last
    # И одинаково для обоих режимов: отдельной оговорки про рисованный нет.
    assert "рисованный" not in fake.last.lower()


async def test_three_ratings_with_distinct_payloads():
    """Одна кнопка «так себе» давала только жалобы без знаменателя:
    молчание одинаково значит «понравилось» и «лень нажимать»."""
    from avatar_miniapp.chat import RATINGS

    scores = [score for score, _ in RATINGS]
    assert scores == ["good", "ok", "bad"]
    titles = [title for _, title in RATINGS]
    assert len(set(titles)) == 3
    assert all(len(t) <= 14 for t in titles), titles


# --- кто получает вложение ---------------------------------------------------
# Адресат теперь ровно один — сценарий в переписке. Раньше вложения ждало
# ещё и окно, и споры между ними стоили нам живого бага: висящее с прошлого
# захода «жду запись» съедало первый же шаг разговора. Путь через окно
# убран целиком, и проверяем мы теперь не приоритет, а что вложение не
# теряется и не уходит в пустоту.

class Recorder:
    """Сценарий-пустышка: только запоминает, что ему отдали."""

    def __init__(self, step=""):
        self.step = step
        self.got: list[tuple] = []

    def waiting_for(self, user_id):
        return self.step

    async def on_file(self, chat_id, user_id, kind, data, suffix):
        self.got.append((kind, suffix, len(data)))
        return True

    async def on_text(self, chat_id, user_id, text):
        return False

    async def begin(self, chat_id, user_id, greet=True):
        self.got.append(("menu", "", 0))


def _chat(conversation, media=None):
    from avatar_miniapp.chat import ChatSide

    chat = ChatSide.__new__(ChatSide)
    chat.conversation = conversation
    chat.media = media or _Media()
    chat.webapp_url = ""
    chat.username = ""
    chat.said = []

    async def download(url):
        return b"x" * 32

    async def send(chat_id, text):
        chat.said.append(text)

    chat._download = download
    chat._send = send
    return chat


class _Media:
    """ffmpeg в этих тестах не нужен: разбор содержимого проверяется
    отдельно, в test_inbox."""

    def __init__(self, kind="voice", secs=4.0, sound=b"WAV"):
        self.kind, self.secs, self.sound = kind, secs, sound

    def sniff(self, data, suffix): return self.kind

    def seconds(self, data, suffix): return self.secs

    def audio_from(self, data, suffix): return self.sound


async def test_photo_goes_to_the_conversation():
    talk = Recorder(step="photo")
    chat = _chat(talk)
    await chat._intake(CHAT, USER, [{"type": "image", "payload": {"url": "http://x/a.jpg"}}])
    assert talk.got == [("photo", ".jpg", 32)]


async def test_attachment_without_a_conversation_shows_the_menu():
    """Прислали файл, ничего не начав. Уводить в окно больше некуда —
    показываем меню, чтобы человек хотя бы понял, с чего начать."""
    talk = Recorder(step="")
    chat = _chat(talk)
    await chat._intake(CHAT, USER, [{"type": "image", "payload": {"url": "http://x/a.jpg"}}])
    assert talk.got == [("menu", "", 0)]
    assert not any("приложени" in said for said in chat.said)


async def test_video_goes_to_the_conversation():
    talk = Recorder(step="video")
    chat = _chat(talk, _Media(kind="video"))
    await chat._intake(CHAT, USER, [{"type": "video", "payload": {"url": "http://x/v.mp4"}}])
    assert talk.got == [("video", ".mp4", 32)]


async def test_voice_step_takes_the_soundtrack_out_of_a_clip():
    """Голосовые до бота не доезжают, поэтому образец голоса человек
    присылает роликом — звук из него вынимаем до передачи в сценарий."""
    talk = Recorder(step="voice")
    chat = _chat(talk, _Media(kind="video", sound=b"RIFFxxxx"))
    await chat._intake(CHAT, USER, [{"type": "video", "payload": {"url": "http://x/v.mp4"}}])
    assert talk.got == [("voice", ".wav", 8)]


async def test_too_short_recording_is_refused_before_the_scenario():
    """Две секунды модель не примет, и узнать об этом надо сразу,
    а не после генерации."""
    talk = Recorder(step="voice")
    chat = _chat(talk, _Media(kind="video", secs=1.2))
    await chat._intake(CHAT, USER, [{"type": "video", "payload": {"url": "http://x/v.mp4"}}])
    assert talk.got == []
    assert chat.said and "двух секунд" in chat.said[-1]
