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

import aiohttp
from maxapi import Bot, Dispatcher
from maxapi.enums import UploadType
from maxapi.filters import F
from maxapi.enums import AttachmentType
from maxapi.types import (Attachment, BotStarted, ButtonsPayload, CallbackButton,
                          InputMediaBuffer, MessageCallback, MessageCreated)

from . import maxcompat
from .inbox import VIDEO, VOICE, Attachments

log = logging.getLogger("miniapp.chat")

# Запасное приветствие: используется, только если сценарий не подключён
# (бот поднят без него). Обычный путь — меню из dialog.begin.
GREETING = (
    "Привет! Я делаю говорящего аватара по вашему фото или видео.\n\n"
    "Напишите «/start» — покажу, что умею."
)
NUDGE = "Напишите «/start» — покажу, что умею."
# Оценка ролика. Порядок слева направо — от лучшего к худшему.
RATINGS = (("good", "👍 Отлично"), ("ok", "😐 Нормально"), ("bad", "👎 Так себе"))
RATING_MARKS = {"good": "👍", "ok": "😐", "bad": "👎"}
TOO_SHORT_VOICE = (
    "Запись короче двух секунд — модель такую не примет.\n"
    "Запишите ещё раз, скажите пару фраз."
)
TOO_SHORT_VIDEO = "Видео короче двух секунд. Снимите подлиннее, секунд на десять."
CANT_TAKE = "Не смог забрать файл. Попробуйте отправить ещё раз."
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


def user_id_of(event) -> int | None:
    """Кто это написал. Порядок путей важнее, чем кажется.

    У нажатия кнопки заполнено И `callback.user` (человек, который нажал),
    И `message.sender` — но там отправитель СООБЩЕНИЯ, к которому кнопка
    прикреплена, то есть сам бот. Пока `message.sender` стоял первым,
    весь разговор писался под id бота: человек нажимал «рисованный
    аватар», состояние ложилось на бота, а присланное следом фото
    искало состояние под настоящим id и не находило — бот отвечал
    «с чего начнём?» на каждое второе действие. Заодно все люди делили
    одно состояние на всех.
    """
    for path in (
        ("callback", "user", "user_id"),
        ("user", "user_id"),
        ("message", "sender", "user_id"),
        ("user_id",),
    ):
        node = event
        for attr in path:
            node = getattr(node, attr, None)
            if node is None:
                break
        if isinstance(node, int):
            return node
    return None


def suffix_of(url: str, fallback: str) -> str:
    """Расширение из ссылки. Нужно ffprobe, чтобы понять, что это за файл."""
    tail = url.split("?")[0].rsplit("/", 1)[-1]
    if "." in tail:
        ext = "." + tail.rsplit(".", 1)[-1].lower()
        if 2 <= len(ext) <= 6 and ext[1:].isalnum():
            return ext
    return fallback


class ChatSide:
    def __init__(self, token: str, webapp_url: str = "",
                 media: Attachments | None = None, conversation=None) -> None:
        self.bot = Bot(token)
        self.dp = Dispatcher()
        self.webapp_url = webapp_url
        # Разбор входящих файлов: что это и как достать звук. Склада больше
        # нет — вложение сразу уходит в сценарий.
        self.media = media or Attachments()
        # Сценарий в переписке. Без него бот остаётся тем, чем был, —
        # приветствием и доставкой; с ним умеет весь путь сам.
        self.conversation = conversation
        self.username = ""
        self._task: asyncio.Task | None = None
        self._register()

    async def _ack(self, event) -> None:
        """Подтвердить нажатие. Без него MAX крутит спиннер на кнопке.

        Пустым ack он не принимает, текст обязателен. И сам callback мог
        устареть: сообщение живёт неделями, а мы за это время перезапускались,
        поэтому отказ здесь — не повод ломать обработку.
        """
        try:
            await event.ack(notification="Готово")
        except Exception as exc:  # noqa: BLE001
            log.debug("ack не прошёл: %s", exc)

    async def _menu_or_greeting(self, event) -> None:
        chat_id, user_id = chat_id_of(event), user_id_of(event)
        if self.conversation and chat_id and user_id:
            await self.conversation.begin(chat_id, user_id)
            return
        await self._say(event, GREETING)

    # --- обработчики ------------------------------------------------------

    def _register(self) -> None:
        dp = self.dp

        @dp.bot_started()
        async def _started(event: BotStarted) -> None:
            await self._menu_or_greeting(event)

        @dp.message_created(F.message.body.text.lower().startswith("/start"))
        async def _start(event: MessageCreated) -> None:
            await self._menu_or_greeting(event)

        @dp.message_callback(F.callback.payload.startswith("again"))
        async def _again(event: MessageCallback) -> None:
            """«Сделать ещё» после готового ролика — снова в меню."""
            await self._ack(event)
            chat_id, user_id = chat_id_of(event), user_id_of(event)
            if self.conversation and chat_id and user_id:
                await self.conversation.begin(chat_id, user_id, greet=False)

        @dp.message_callback(F.callback.payload.startswith("rate:"))
        async def _rate(event: MessageCallback) -> None:
            """Оценка ролика. Три кнопки, а не одна.

            С одним «так себе» в журнале копились только жалобы, и по ним
            нельзя сказать, много их или мало: молчание одинаково значит
            и «понравилось», и «лень нажимать». Три кнопки дают знаменатель.
            """
            _, _, rest = (event.callback.payload or "").partition(":")
            score, _, key = rest.partition(":")
            log.info("%s по задаче %s", RATING_MARKS.get(score, "оценка ?"), key)
            try:
                # ack пустым MAX не принимает, текст обязателен. И сам callback
                # мог устареть: сообщение живёт неделями, а мы перезапускались.
                await event.ack(notification="Спасибо!")
            except Exception as exc:  # noqa: BLE001
                log.debug("ack не прошёл: %s", exc)

        @dp.message_callback(F.callback.payload.startswith("bad:"))
        async def _bad_legacy(event: MessageCallback) -> None:
            """Старая одиночная кнопка. Сообщения в чате живут неделями,
            и человек вполне может нажать её во вчерашнем ролике."""
            key = (event.callback.payload or "").split(":", 1)[-1]
            log.info("👎 по задаче %s", key)
            try:
                await event.ack(notification="Спасибо, записал")
            except Exception as exc:  # noqa: BLE001
                log.debug("ack не прошёл: %s", exc)

        # Кнопки сценария. Регистрируется после именованных, но до catch-all
        # по сообщениям: у callback-ов свой поток событий.
        @dp.message_callback()
        async def _step(event: MessageCallback) -> None:
            payload = (event.callback.payload or "")
            if (payload.startswith("rate:") or payload.startswith("bad:")
                    or payload.startswith("again")):
                return
            await self._ack(event)
            chat_id, user_id = chat_id_of(event), user_id_of(event)
            if self.conversation and chat_id and user_id:
                await self.conversation.on_button(chat_id, user_id, payload)

        # Ловим всё остальное. Регистрируется ПОСЛЕДНИМ: зарегистрированный
        # раньше catch-all перехватил бы и /start.
        @dp.message_created()
        async def _anything(event: MessageCreated) -> None:
            attachments = getattr(getattr(event.message, "body", None), "attachments", None)
            if attachments:
                await self._take_attachments(event, attachments)
                return
            text = getattr(getattr(event.message, "body", None), "text", "") or ""
            chat_id, user_id = chat_id_of(event), user_id_of(event)
            if self.conversation and chat_id and user_id:
                # Текст — это ответ на «что сказать аватару», если мы его ждём.
                if await self.conversation.on_text(chat_id, user_id, text):
                    return
                # Ничего не ждём — показываем меню. Раньше здесь было «нажмите
                # Старт внизу», и человек, написавший боту «привет», упирался
                # в тупик: кнопки он не видел, а других вариантов ему не дали.
                await self.conversation.begin(chat_id, user_id, greet=False)
                return
            await self._say(event, NUDGE)

    # --- приём вложений ---------------------------------------------------

    def _describe(self, attachment) -> tuple[str, str]:
        """Тип и ссылка вложения, максимально терпимо к форме объекта.

        maxapi отдаёт разные payload-классы, а голосовое MAX может назвать
        и audio, и file. Поэтому смотрим и объект, и словарь, и логируем
        то, что реально пришло: догадки тут дороже одной строки в журнале.
        """
        if isinstance(attachment, dict):
            kind = str(attachment.get("type") or "")
            payload = attachment.get("payload") or {}
            url = payload.get("url") if isinstance(payload, dict) else ""
        else:
            raw = getattr(attachment, "type", "")
            kind = str(getattr(raw, "value", raw) or "")
            payload = getattr(attachment, "payload", None)
            url = getattr(payload, "url", "") or ""
            if not url and hasattr(payload, "model_dump"):
                url = (payload.model_dump() or {}).get("url", "") or ""
        return kind.lower(), url or ""

    async def take_raw(self, event: dict) -> None:
        """Событие, которое maxapi не разобрал.

        Голосовые приходят именно так, поэтому это не запасной путь,
        а сейчас основной. Достаём то же самое руками из словаря.
        """
        if event.get("update_type") != "message_created":
            return
        message = event.get("message") or {}
        body = message.get("body") or {}
        attachments = body.get("attachments") or []
        if not attachments:
            return
        user_id = (message.get("sender") or {}).get("user_id")
        chat_id = (message.get("recipient") or {}).get("chat_id")
        await self._intake(chat_id, user_id, attachments)

    async def _take_attachments(self, event: MessageCreated, attachments: list) -> None:
        await self._intake(chat_id_of(event), user_id_of(event), attachments)

    async def _intake(self, chat_id: int | None, user_id: int | None,
                      attachments: list) -> None:
        """Вложение — это ответ на вопрос сценария, и других адресатов нет.

        Раньше вложения могло ждать ещё и окно: оно не умеет ни записать
        голос, ни снять видео, и просило сделать это в чате. Путь выходил
        на шесть действий с тремя переключениями контекста, поэтому его
        убрали целиком — весь сценарий теперь живёт здесь. Если разговор
        ничего не ждёт, вложение просто не к чему приложить: показываем
        меню, а не подсказку про окно.
        """
        for attachment in attachments:
            kind, url = self._describe(attachment)
            # Пока платформа молодая, состав вложений стоит видеть целиком:
            # один раз это уже спасло от гадания, чем MAX шлёт голосовое.
            log.info("вложение: тип=%s, ссылка=%s", kind or "?", (url or "нет")[:120])

        if user_id is None or chat_id is None:
            log.warning("вложение некуда деть: user_id=%s, chat_id=%s", user_id, chat_id)
            return
        if not self.conversation:
            await self._send(chat_id, NUDGE)
            return

        step = self.conversation.waiting_for(user_id)
        if not step:
            await self.conversation.begin(chat_id, user_id, greet=False)
            return

        for attachment in attachments:
            kind, url = self._describe(attachment)
            if not url:
                continue

            if kind == "image":
                data = await self._download(url)
                if data is None:
                    await self._send(chat_id, CANT_TAKE)
                    return
                await self.conversation.on_file(
                    chat_id, user_id, "photo", data, suffix_of(url, ".jpg"))
                return

            if kind not in ("audio", "video", "file"):
                continue

            data = await self._download(url)
            if data is None:
                await self._send(chat_id, CANT_TAKE)
                return

            if kind == "video":
                want = VIDEO
            elif kind == "audio":
                want = VOICE
            else:
                # MAX отдаёт и голосовое, и видео как `file` со ссылкой
                # `getfile?rq=...` — ни расширения, ни подсказки. Смотрим
                # содержимое: есть видеодорожка — значит видео.
                want = self.media.sniff(data, suffix_of(url, "")) or VOICE
            suffix = suffix_of(url, ".mp4" if want == VIDEO else ".ogg")

            # Короткая запись — отказ модели уже после генерации, а человек
            # к тому времени успевает забыть, что прислал две секунды.
            # Дешевле сказать сразу.
            seconds = self.media.seconds(data, suffix)
            if seconds and seconds < 2.0:
                await self._send(chat_id,
                                 TOO_SHORT_VIDEO if want == VIDEO else TOO_SHORT_VOICE)
                return

            # Голос человек присылает роликом: голосовые до бота не доезжают
            # (MAX шлёт по ним пустое событие без тела), а видео доезжает.
            # Значит на шаге голоса берём из ролика звуковую дорожку.
            if step == "voice" and want == VIDEO:
                sound = self.media.audio_from(data, suffix)
                if sound:
                    data, suffix, want = sound, ".wav", VOICE
            await self.conversation.on_file(chat_id, user_id, want, data, suffix)
            return

        # Дошли сюда — вложение есть, но не то, что мы умеем брать.
        await self._send(chat_id, NUDGE)

    async def _download(self, url: str) -> bytes | None:
        try:
            timeout = aiohttp.ClientTimeout(total=120)
            async with aiohttp.ClientSession(timeout=timeout) as session, \
                    session.get(url) as response:
                if response.status != 200:
                    log.error("вложение %s: код %s", url[:80], response.status)
                    return None
                return await response.read()
        except Exception as exc:  # noqa: BLE001
            log.error("не скачалось вложение: %s", exc)
            return None

    # --- отправка ---------------------------------------------------------

    async def _say(self, event, text: str) -> None:
        chat_id = chat_id_of(event)
        if chat_id is None:
            log.warning("не нашли, куда отвечать: %s", type(event).__name__)
            return
        await self._send(chat_id, text)

    async def _send(self, chat_id: int, text: str) -> None:
        """Простое сообщение без кнопок.

        Кнопки «вернуться в приложение» здесь больше нет: окно не просит
        ничего присылать в чат, и возвращать человека некуда — сценарий
        целиком идёт здесь же.
        """
        try:
            await self.bot.send_message(chat_id=chat_id, text=text)
        except Exception as exc:  # noqa: BLE001 — упавший мессенджер не должен ронять бота
            log.error("не отправилось в %s: %s", chat_id, exc)

    @staticmethod
    def keyboard(*buttons) -> list:
        """Кнопки уезжают ОДНИМ вложением inline_keyboard, а не списком списков.

        Список списков maxapi принимает молча, а падает уже при отправке:
        «'list' object has no attribute 'model_dump'». Ряды кнопок живут
        внутри ButtonsPayload, а не в самом attachments.
        """
        return [Attachment(type=AttachmentType.INLINE_KEYBOARD,
                           payload=ButtonsPayload(buttons=[list(buttons)]))]

    @staticmethod
    def rows(*rows) -> list:
        """То же, но несколько рядов. На телефоне три кнопки в строку
        превращаются в три обрезанных слова, поэтому меню идёт столбиком."""
        return [Attachment(type=AttachmentType.INLINE_KEYBOARD,
                           payload=ButtonsPayload(buttons=[list(r) for r in rows]))]

    async def ask(self, chat_id: int, text: str, buttons: list) -> None:
        """Сообщение сценария: текст плюс кнопки-ответы, по одной в ряд.

        Отдельный метод, а не параметр к _send: там простой текст без
        кнопок, и смешивать «сказал» и «спросил» в одной функции — верный
        способ однажды отправить вопрос без вариантов ответа.
        """
        attachments = []
        if buttons:
            attachments = self.rows(*[
                [CallbackButton(text=title, payload=payload)] for title, payload in buttons
            ])
        try:
            await self.bot.send_message(chat_id=chat_id, text=text,
                                        attachments=attachments)
        except Exception as exc:  # noqa: BLE001
            log.error("сценарий: не отправилось в %s: %s", chat_id, exc)

    async def deliver(self, user_id: int, video: Path, caption: str, feedback_key: str,
                      poster: Path | None = None) -> None:
        """Результат — в личку по user_id из проверенной подписи.

        Порядок важен: сначала уходит видео, потом отдельным сообщением кнопка.
        Есть ограничение частоты (порядка двух сообщений в секунду на адресата),
        и если второе не уйдёт — человек уже получил главное.

        Рисованный портрет отправляем отдельной картинкой ПОСЛЕ ролика: он
        нравится людям сам по себе и его ставят на аватарку, а видео на это
        не годится. Порядок именно такой — главное первым, приятное вторым.
        """
        media = InputMediaBuffer(
            buffer=video.read_bytes(), filename="avatar", type=UploadType.VIDEO
        )
        await self.bot.send_message(user_id=user_id, text=caption, attachments=[media])

        if poster is not None and poster.is_file():
            try:
                await self.bot.send_message(
                    user_id=user_id,
                    text="И сам портрет — можно поставить на аватарку.",
                    attachments=[InputMediaBuffer(
                        buffer=poster.read_bytes(), filename="avatar",
                        type=UploadType.IMAGE,
                    )],
                )
            except Exception as exc:  # noqa: BLE001
                # Картинка — бонус. Не ушла — ролик человек всё равно получил,
                # и ронять из-за этого доставку незачем.
                log.warning("портрет не ушёл: %s", exc)
        try:
            await self.bot.send_message(
                user_id=user_id,
                # Длиннее, чем нужно по смыслу: ширину клавиатуры
                # мессенджер берёт от пузыря, а пузырь — от текста.
                text="Как получилось? Оцените ролик — по этим кнопкам мы и понимаем, что чинить в первую очередь.",
                # По кнопке в ряд. Три в строку не влезают: на телефоне
                # «Нормально» обрезается до «Нормальнс», и выглядит это
                # не как компактность, а как поломка. Проверено вживую
                # в Telegram, но экран у мессенджеров один и тот же.
                attachments=self.rows(
                    *[[CallbackButton(text=title,
                                      payload=f"rate:{score}:{feedback_key}")]
                      for score, title in RATINGS],
                    [CallbackButton(text="🔁 Сделать ещё", payload="again")],
                ),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("кнопка обратной связи не ушла: %s", exc)

    async def say_failed(self, user_id: int, reason: str = "") -> None:
        text = reason.strip() or FAILED
        try:
            await self.bot.send_message(user_id=user_id, text=text)
        except Exception as exc:  # noqa: BLE001
            log.error("не отправилось про отказ: %s", exc)

    # --- жизненный цикл ---------------------------------------------------

    async def start(self) -> None:
        # Ставим ДО опроса: голосовые приходят обновлениями, которые maxapi
        # пока не разбирает, и без подпорки они теряются молча.
        maxcompat.install(self.take_raw)
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
