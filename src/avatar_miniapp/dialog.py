"""Сценарий в переписке: тот же аватар, но без окна.

ЗАЧЕМ. Путь через мини-апп упирается в платформу: MAX не отдаёт вебвью
микрофон (проверено на телефоне — `NotAllowedError` при живых
`mediaDevices` и `MediaRecorder`, то есть спросить разрешение просто
некому), а его файловый выбор открывает камеру только под фотографию.
Поэтому «записать свой голос» из окна выглядит так: закрыть окно, найти
чат, снять ролик, отправить, дождаться кнопки, вернуться. Шесть действий
и три переключения контекста — на этом теряются даже терпеливые.

В переписке ничего этого нет. Бот спрашивает по одному, человек отвечает
тем, чем привык: фото — фотографией, голос — роликом с камеры, текст —
текстом. Ноль переключений.

УСТРОЙСТВО. Конечный автомат на человека, состояние на диске: между
сообщениями процесс может перезапуститься (выкладка), а разговор должен
пережить это незаметно.

    режим toon/photo:   фото → голос → текст → генерация
    режим video:        видео → текст → генерация

Модуль намеренно ничего не знает ни про maxapi, ни про HTTP: отправка,
скачивание и постановка задачи приходят снаружи функциями. Так весь
сценарий проверяется тестами без сети и без мессенджера.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger("miniapp.dialog")

# Сколько живёт незаконченный разговор. Вернувшийся через сутки человек
# уже не помнит, на чём остановился, и продолжать за него — хуже, чем
# начать заново.
MAX_AGE_S = 2 * 3600

MODES = ("toon", "photo", "video")
STEP_PHOTO, STEP_VIDEO, STEP_VOICE, STEP_TEXT, STEP_BUSY = (
    "photo", "video", "voice", "text", "busy")

MODE_TITLES = {
    "toon": "рисованный аватар",
    "photo": "аватар по фото",
    "video": "аватар по видео",
}

# Фраза для образца голоса даётся дословно. Человек, которому сказали
# просто «запишите голос», застревает на вопросе «а что говорить» и
# присылает две секунды мычания.
SAMPLE_LINE = "Это пример голоса для аватара"

# Как записать свой голос — единственное место, где мессенджеры расходятся
# по существу, а не по словам. MAX не передаёт ботам голосовые: по ним
# прилетает пустое событие без тела, поэтому там образцом служит видео.
# Telegram отдаёт голосовые нормально, и просить ради этого снимать себя
# на камеру было бы издевательством.
#
# Подсказку выбирает точка входа и передаёт сюда: сам сценарий про
# мессенджеры не знает и знать не должен.
HINT_VOICE_MAX = (
    "Снимите видео на 4–7 секунд и пришлите сюда.\n"
    f"Скажите в тишине: «{SAMPLE_LINE}».\n\n"
    "Нужен только голос — само видео никуда не пойдёт.\n"
    "Записать голосовым, к сожалению, нельзя: MAX не передаёт"
    " голосовые сообщения ботам."
)
HINT_VOICE_TELEGRAM = (
    "В тишине запишите голосовое сообщение с фразой:\n"
    f"«{SAMPLE_LINE}»\n\n"
    "Или приложите аудио- либо видеофайл со своим голосом, до 15 секунд."
)


@dataclass
class State:
    user_id: int
    mode: str = ""
    step: str = ""
    photo: str = ""
    voice: str = ""
    voice_id: str = ""
    video: str = ""
    at: float = field(default_factory=time.time)

    @property
    def stale(self) -> bool:
        return time.time() - self.at > MAX_AGE_S

    def inputs(self) -> dict:
        """То, что уходит в задачу. Ключи те же, что у веб-части: генерация
        не должна знать, пришёл человек из окна или из переписки."""
        out: dict = {}
        for key in ("photo", "voice", "video"):
            value = getattr(self, key)
            if value:
                out[key] = value
        if self.voice_id and not self.voice:
            out["voice_id"] = self.voice_id
        return out


class Store:
    """Состояния на диске, по файлу на человека."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, user_id: int) -> Path:
        return self.root / f"{user_id}.json"

    def get(self, user_id: int) -> State:
        path = self._path(user_id)
        if path.is_file():
            try:
                state = State(**json.loads(path.read_text(encoding="utf-8")))
                if not state.stale:
                    return state
                log.info("%s: разговор протух, начинаем заново", user_id)
            except (OSError, ValueError, TypeError) as exc:
                log.warning("%s: состояние не читается (%s)", user_id, exc)
        return State(user_id=user_id)

    def save(self, state: State) -> None:
        state.at = time.time()
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self._path(state.user_id).with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(state), ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(self._path(state.user_id))

    def clear(self, user_id: int) -> None:
        self._path(user_id).unlink(missing_ok=True)


# Кнопка: подпись и payload. Отправку рисует чат-часть — здесь только смысл.
Button = tuple[str, str]

Send = Callable[[int, str, list[Button]], Awaitable[None]]
Save = Callable[[int, str, bytes, str], Path]
StartJob = Callable[[int, str, str, dict], object]
Voices = Callable[[], list[dict]]
Estimate = Callable[[str], float]

MAX_SPEECH_SECONDS = 14.0
SHORT_CLIP_SECONDS = 5.0
# Слова, а не секунды: человек пишет текст и считает слова, а не хронометраж.
# Пересчёт по темпу русской речи из avatar_core.speech — там 5.5 слогов
# в секунду, что на живых фразах даёт около 2.5 слова в секунду.
WORDS_FOR_FLOOR = 12
WORDS_FOR_LIMIT = 35


class Conversation:
    """Сценарий целиком. Ничего не знает ни про maxapi, ни про HTTP."""

    def __init__(self, store: Store, *, send: Send, save_file: Save,
                 start_job: StartJob, voices: Voices, estimate: Estimate,
                 toon_enabled: bool = True,
                 voice_hint: str = HINT_VOICE_MAX) -> None:
        self.store = store
        self.send = send
        self.save_file = save_file
        self.start_job = start_job
        self.voices = voices
        self.estimate = estimate
        self.toon_enabled = toon_enabled
        self.voice_hint = voice_hint

    # --- вход ------------------------------------------------------------

    async def begin(self, chat_id: int, user_id: int, greet: bool = True) -> None:
        """Главное меню. Оно же — ответ на «/start» и на всё непонятное."""
        self.store.clear(user_id)
        buttons: list[Button] = []
        if self.toon_enabled:
            buttons.append(("🎨 Рисованный аватар", "go:toon"))
        buttons.append(("🖼 Аватар по фото", "go:photo"))
        buttons.append(("🎬 Аватар по видео", "go:video"))
        text = (
            "Привет! Сделаю говорящего аватара — он скажет вашим голосом то,"
            " что вы напишете.\n\n"
            "Всё прямо здесь, в переписке: я спрошу по одному шагу."
            if greet else "С чего начнём?"
        )
        await self.send(chat_id, text, buttons)

    async def on_button(self, chat_id: int, user_id: int, payload: str) -> bool:
        """Нажатие кнопки. True — обработали, дальше не идём."""
        if payload == "cancel":
            self.store.clear(user_id)
            await self.begin(chat_id, user_id, greet=False)
            return True
        if payload.startswith("go:"):
            mode = payload.split(":", 1)[1]
            if mode not in MODES:
                return False
            state = State(user_id=user_id, mode=mode)
            state.step = STEP_VIDEO if mode == "video" else STEP_PHOTO
            self.store.save(state)
            await self._ask(chat_id, state)
            return True
        if payload.startswith("voice:"):
            state = self.store.get(user_id)
            if state.step != STEP_VOICE:
                return False
            choice = payload.split(":", 1)[1]
            if choice == "own":
                await self.send(chat_id, self._ask_own_voice(), [("Отмена", "cancel")])
                return True
            state.voice_id = choice
            state.voice = ""
            state.step = STEP_TEXT
            self.store.save(state)
            await self._ask(chat_id, state)
            return True
        return False

    async def on_text(self, chat_id: int, user_id: int, text: str) -> bool:
        """Обычное сообщение. True — оно было частью сценария."""
        state = self.store.get(user_id)
        if state.step != STEP_TEXT:
            return False

        line = text.strip()
        if not line:
            return False
        seconds = self.estimate(line)
        long_note = ""
        if seconds > MAX_SPEECH_SECONDS:
            # Раньше здесь был отказ с требованием сократить. Считать слова
            # люди не захотели, и справедливо: это наша арифметика, а не их
            # задача. Теперь делаем как просят и честно предупреждаем, чем
            # это грозит.
            long_note = (f"\n\nТекст длинный — примерно {seconds:.0f} секунд,"
                         f" а ролик не длиннее {MAX_SPEECH_SECONDS:.0f}."
                         " Конец фразы может смазаться или не прозвучать.")

        state.step = STEP_BUSY
        self.store.save(state)
        try:
            self.start_job(user_id, state.mode, line, state.inputs())
        except Exception as exc:  # noqa: BLE001 — занятость это не поломка
            # Одна задача на человека: вторая пришла бы вместо первой.
            state.step = STEP_TEXT
            self.store.save(state)
            log.info("%s: задача не поставлена: %s", user_id, exc)
            await self.send(chat_id, (
                "У вас уже готовится один ролик. Дождитесь его — и сразу"
                " сделаем следующий."
            ), [])
            return True

        self.store.clear(user_id)
        note = ""
        if seconds < SHORT_CLIP_SECONDS - 1:
            # Короткая реплика не заполняет минимальный ролик, а модель
            # обязана заполнить его речью и добирает недостающее сама.
            note = ("\n\nРеплика короткая — в таких роликах модель иногда"
                    " договаривает лишнее. Если получится странно, попробуйте"
                    " фразу подлиннее.")
        # Одна оценка на все режимы и с запасом. Точность тут никому не
        # нужна, а «меньше минуты», не сбывшееся из-за очереди, человек
        # читает как поломку.
        await self.send(chat_id, (
            "Принял, делаю — это займёт пару минут.\n"
            "Пришлю сюда, как будет готово." + note + long_note
        ), [])
        return True

    async def on_file(self, chat_id: int, user_id: int, kind: str,
                      data: bytes, suffix: str) -> bool:
        """Вложение. `kind` — photo, video или voice, уже опознанный."""
        state = self.store.get(user_id)
        if not state.step or state.step == STEP_BUSY:
            return False

        if state.step == STEP_PHOTO:
            if kind != "photo":
                await self.send(chat_id, "Сейчас нужно фото — пришлите фотографию лица.",
                                [("Отмена", "cancel")])
                return True
            state.photo = str(self.save_file(user_id, "photo", data, suffix))
            state.step = STEP_VOICE
            self.store.save(state)
            await self._ask(chat_id, state)
            return True

        if state.step == STEP_VIDEO:
            if kind != "video":
                await self.send(chat_id, (
                    "Сейчас нужно видео — снимите короткий ролик, где вы говорите."
                ), [("Отмена", "cancel")])
                return True
            state.video = str(self.save_file(user_id, "video", data, suffix))
            state.step = STEP_TEXT
            self.store.save(state)
            await self._ask(chat_id, state)
            return True

        if state.step == STEP_VOICE:
            # Голосовые до бота не доходят, поэтому образцом голоса служит
            # короткое видео: звук из него вынимает чат-часть до вызова сюда.
            if kind not in ("voice", "video"):
                await self.send(chat_id, self._ask_own_voice(), [("Отмена", "cancel")])
                return True
            state.voice = str(self.save_file(user_id, "voice", data, suffix))
            state.voice_id = ""
            state.step = STEP_TEXT
            self.store.save(state)
            await self._ask(chat_id, state)
            return True

        return False

    def waiting_for(self, user_id: int) -> str:
        """Чего ждём от человека прямо сейчас. Пусто — разговора нет.

        Нужно чат-части: пришедшее вложение имеет смысл, только если мы
        его о чём-то спрашивали. Иначе показываем меню.
        """
        return self.store.get(user_id).step

    # --- вопросы ---------------------------------------------------------

    async def _ask(self, chat_id: int, state: State) -> None:
        cancel: list[Button] = [("Отмена", "cancel")]

        if state.step == STEP_PHOTO:
            await self.send(chat_id, (
                f"Делаем {MODE_TITLES[state.mode]}.\n\n"
                "Шаг 1 из 3. Пришлите фото лица — обычной фотографией в чат.\n"
                "Лучше всего анфас, при ровном свете, лицо крупно."
            ), cancel)
            return

        if state.step == STEP_VIDEO:
            await self.send(chat_id, (
                f"Делаем {MODE_TITLES[state.mode]}.\n\n"
                "Шаг 1 из 2. Снимите видео на 4–7 секунд, где вы говорите,"
                " и пришлите сюда.\n"
                f"Можно сказать: «{SAMPLE_LINE}».\n\n"
                "Возьму оттуда и внешность, и голос."
            ), cancel)
            return

        if state.step == STEP_VOICE:
            buttons: list[Button] = []
            for voice in self.voices()[:3]:
                buttons.append((voice.get("title") or voice["id"], "voice:" + voice["id"]))
            buttons.append(("🎤 Записать свой", "voice:own"))
            buttons.append(("Отмена", "cancel"))
            await self.send(chat_id, (
                "Шаг 2 из 3. Теперь голос.\n\n"
                + ("Выберите стандартный голос или запишите свой."
                   if self.voices()
                   else "Готовых голосов сейчас нет — запишите свой.")
            ), buttons)
            return

        if state.step == STEP_TEXT:
            total = 2 if state.mode == "video" else 3
            await self.send(chat_id, (
                f"Шаг {total} из {total}. Что сказать аватару?\n\n"
                f"Напишите текст сообщением. Лучше от {WORDS_FOR_FLOOR} слов"
                f" — это {SHORT_CLIP_SECONDS:.0f} секунд речи: ролики короче"
                " выходят менее стабильными.\n"
                f"Больше {WORDS_FOR_LIMIT} слов"
                f" ({MAX_SPEECH_SECONDS:.0f} секунд) в ролик уже не поместится"
                " — конец может смазаться, но попробовать никто не мешает."
            ), cancel)
            return

    def _ask_own_voice(self) -> str:
        return self.voice_hint
