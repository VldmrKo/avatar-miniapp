"""Чат-часть: приветствие, доставка результата, обратная связь.

Живёт в ТОМ ЖЕ процессе, что и веб-сервер (см. __main__). Отдельным сервисом
её делать нельзя: один токен = один процесс long polling, а два процесса
поделят апдейты пополам, и бот начнёт «через раз не отвечать».

Сигнатуры maxapi 1.2.2.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from maxapi import Bot, Dispatcher
from maxapi.enums import UploadType
from maxapi.filters import F
from maxapi.types import BotStarted, CallbackButton, InputMediaBuffer, MessageCallback, MessageCreated

log = logging.getLogger("miniapp.chat")

GREETING = (
    "Привет! Я делаю говорящего аватара по вашему фото или видео.\n\n"
    "Здесь, в переписке, я ничего не умею — вся работа в окне приложения.\n"
    "Нажмите «Старт» внизу, чтобы открыть его."
)
NUDGE = "Нажмите «Старт» внизу — там всё и происходит."
GOT_PHOTO = (
    "Фото лучше загружать в приложении: там же выбирается голос и текст.\n"
    "Нажмите «Старт» внизу."
)
FAILED = (
    "Не получилось сделать ролик. Это бывает, когда модель занята — "
    "попробуйте ещё раз через пару минут."
)


def chat_id_of(event) -> int | None:
    """chat_id лежит в разных местах у разных апдейтов.

    У BotStarted, MessageCreated и MessageCallback заполнено разное, поэтому
    ищем с запасом. Вернуть None лучше, чем упасть с AttributeError.
    """
    for path in (
        ("message", "recipient", "chat_id"),
        ("recipient", "chat_id"),
        ("chat_id",),
        ("message", "sender", "user_id"),
    ):
        node = event
        for attr in path:
            node = getattr(node, attr, None)
            if node is None:
                break
        if isinstance(node, int):
            return node
    return None


class ChatSide:
    def __init__(self, token: str, webapp_url: str) -> None:
        self.bot = Bot(token)
        self.dp = Dispatcher()
        self.webapp_url = webapp_url
        self.username = ""
        self._task: asyncio.Task | None = None
        self._register()

    # --- обработчики ------------------------------------------------------

    def _register(self) -> None:
        dp = self.dp

        @dp.bot_started()
        async def _started(event: BotStarted) -> None:
            await self._say(event, GREETING)

        @dp.message_created(F.message.body.text.lower().startswith("/start"))
        async def _start(event: MessageCreated) -> None:
            await self._say(event, GREETING)

        @dp.message_callback(F.callback.payload.startswith("bad:"))
        async def _bad(event: MessageCallback) -> None:
            key = (event.callback.payload or "").split(":", 1)[-1]
            log.info("👎 по задаче %s", key)
            try:
                # ack пустым MAX не принимает, текст обязателен. И сам callback
                # мог устареть: сообщение живёт неделями, а мы перезапускались.
                await event.ack(notification="Спасибо, записал")
            except Exception as exc:  # noqa: BLE001
                log.debug("ack не прошёл: %s", exc)

        # Ловим всё остальное. Регистрируется ПОСЛЕДНИМ: зарегистрированный
        # раньше catch-all перехватил бы и /start.
        @dp.message_created()
        async def _anything(event: MessageCreated) -> None:
            attachments = getattr(getattr(event.message, "body", None), "attachments", None)
            await self._say(event, GOT_PHOTO if attachments else NUDGE)

    # --- отправка ---------------------------------------------------------

    async def _say(self, event, text: str) -> None:
        chat_id = chat_id_of(event)
        if chat_id is None:
            log.warning("не нашли, куда отвечать: %s", type(event).__name__)
            return
        try:
            await self.bot.send_message(chat_id=chat_id, text=text)
        except Exception as exc:  # noqa: BLE001 — упавший мессенджер не должен ронять бота
            log.error("не отправилось в %s: %s", chat_id, exc)

    async def deliver(self, user_id: int, video: Path, caption: str, feedback_key: str) -> None:
        """Результат — в личку по user_id из проверенной подписи.

        Порядок важен: сначала уходит видео, потом отдельным сообщением кнопка.
        Есть ограничение частоты (порядка двух сообщений в секунду на адресата),
        и если второе не уйдёт — человек уже получил главное.
        """
        media = InputMediaBuffer(
            buffer=video.read_bytes(), filename="avatar", type=UploadType.VIDEO
        )
        await self.bot.send_message(user_id=user_id, text=caption, attachments=[media])
        try:
            await self.bot.send_message(
                user_id=user_id,
                text="Как получилось?",
                attachments=[[CallbackButton(text="👎 Так себе", payload=f"bad:{feedback_key}")]],
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("кнопка обратной связи не ушла: %s", exc)

    async def say_failed(self, user_id: int) -> None:
        try:
            await self.bot.send_message(user_id=user_id, text=FAILED)
        except Exception as exc:  # noqa: BLE001
            log.error("не отправилось про отказ: %s", exc)

    # --- жизненный цикл ---------------------------------------------------

    async def start(self) -> None:
        try:
            me = await self.bot.get_me()
            self.username = getattr(me, "username", "") or ""
            log.info("бот @%s на связи", self.username or "без имени")
        except Exception as exc:  # noqa: BLE001
            # Без имени нельзя собрать deep link, но отвечать бот уже может.
            log.warning("get_me не ответил: %s", exc)
            if "токен" in str(exc).lower() or "token" in str(exc).lower():
                log.error(
                    "MAX не принял токен — бот отвечать не будет. Сверьте длину и края "
                    "значения MAX_BOT_TOKEN с рабочей машиной (в логе выше строка «токен»): "
                    "чаще всего оно обрезано при копировании или осталось заполнителем."
                )
        self._task = asyncio.create_task(self.dp.start_polling(self.bot), name="max-polling")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None
