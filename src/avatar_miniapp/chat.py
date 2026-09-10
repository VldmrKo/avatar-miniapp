"""Чат-часть: приветствие, доставка результата, обратная связь.

Живёт в ТОМ ЖЕ процессе, что и веб-сервер (см. __main__). Отдельным сервисом
её делать нельзя: один токен = один процесс long polling, а два процесса
поделят апдейты пополам, и бот начнёт «через раз не отвечать».

Сигнатуры maxapi 1.2.2.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import aiohttp
from maxapi import Bot, Dispatcher
from maxapi.enums import UploadType
from maxapi.filters import F
from maxapi.enums import AttachmentType
from maxapi.types import (Attachment, BotStarted, ButtonsPayload, CallbackButton,
                          InputMediaBuffer, LinkButton, MessageCallback, MessageCreated,
                          OpenAppButton)

from . import maxcompat
from .inbox import VIDEO, VOICE, Inbox

log = logging.getLogger("miniapp.chat")

GREETING = (
    "Привет! Я делаю говорящего аватара по вашему фото или видео.\n\n"
    "Здесь, в переписке, я ничего не умею — вся работа в окне приложения.\n"
    "Нажмите «Старт» внизу, чтобы открыть его."
)
NUDGE = "Нажмите «Старт» внизу — там всё и происходит."
GOT_PHOTO = (
    "Фото загружается в приложении: там же выбирается голос и текст.\n"
    "Нажмите «Старт» внизу."
)
GOT_VOICE = (
    "Голос принят — {seconds:.0f} с.\n"
    "Вернитесь в приложение: он уже выбран во вкладке «Свой голос»."
)
GOT_VIDEO = (
    "Видео принято — {seconds:.0f} с.\n"
    "Вернитесь в приложение и нажмите «Сделать аватара»."
)
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
    for path in (("message", "sender", "user_id"), ("user", "user_id"), ("user_id",)):
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
    def __init__(self, token: str, webapp_url: str, inbox: Inbox | None = None,
                 state_path: Path | None = None) -> None:
        self.bot = Bot(token)
        self.dp = Dispatcher()
        self.webapp_url = webapp_url
        self.inbox = inbox
        self.username = ""
        # Какой способ вернуть человека в приложение MAX принял. Пустая
        # строка — ещё не пробовали или ни один не подошёл. Запоминаем на
        # диск: иначе после каждой выкладки снова два неудачных запроса
        # к API, и в журнале снова выглядит как поломка.
        self._state_path = state_path
        self._good_way = self._recall()
        self._task: asyncio.Task | None = None
        self._register()

    def _recall(self) -> str:
        if not self._state_path or not self._state_path.is_file():
            return ""
        try:
            return str(json.loads(self._state_path.read_text(encoding="utf-8")).get("way") or "")
        except (OSError, ValueError):
            return ""

    def _remember(self, way: str) -> None:
        if not self._state_path:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            self._state_path.write_text(json.dumps({"way": way}, ensure_ascii=False),
                                        encoding="utf-8")
        except OSError as exc:
            log.debug("не запомнили способ возврата: %s", exc)

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
            if attachments:
                await self._take_attachments(event, attachments)
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
        """Голосовое и видео из чата — это наша запись с камеры и микрофона.

        Окну MAX ни микрофон, ни камеру в режиме видео не отдаёт, а себе —
        отдаёт. Поэтому пишет человек привычной кнопкой в переписке, а мы
        забираем файл по ссылке из вложения.
        """
        for attachment in attachments:
            kind, url = self._describe(attachment)
            # Пока платформа молодая, состав вложений стоит видеть целиком:
            # один раз это уже спасло от гадания, чем MAX шлёт голосовое.
            log.info("вложение: тип=%s, ссылка=%s", kind or "?", (url or "нет")[:120])

        if self.inbox is None or user_id is None or chat_id is None:
            log.warning("вложение некуда положить: inbox=%s, user_id=%s, chat_id=%s",
                        bool(self.inbox), user_id, chat_id)
            return

        expected = self.inbox.expected(user_id)
        for attachment in attachments:
            kind, url = self._describe(attachment)
            if kind == "image":
                await self._send(chat_id, GOT_PHOTO)
                return
            if kind not in ("audio", "video", "file") or not url:
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
                want = self.inbox.sniff(data, suffix_of(url, "")) or expected or VOICE
            suffix = suffix_of(url, ".mp4" if want == VIDEO else ".ogg")

            # Голосовые до бота не доезжают — MAX присылает по ним пустое
            # событие без тела. Зато видео доезжает. Поэтому если человек
            # шёл записывать голос, а прислал ролик — берём звук оттуда.
            if want == VIDEO and expected == VOICE:
                sound = self.inbox.audio_from(data, suffix)
                if sound:
                    data, want, suffix = sound, VOICE, ".wav"
                    log.info("ждали голос, пришло видео — взяли звуковую дорожку")

            try:
                item = self.inbox.put(user_id, want, data, suffix)
            except ValueError as exc:
                log.warning("вложение не принято: %s", exc)
                await self._send(chat_id, CANT_TAKE)
                return

            if item.seconds and item.seconds < 2.0:
                self.inbox.clear(user_id, want)
                await self._send(chat_id,
                                 TOO_SHORT_VIDEO if want == VIDEO else TOO_SHORT_VOICE)
                return

            # Куда возвращать, спрашиваем у inbox: голос нужен и обычному
            # аватару, и мультяшному, и по виду записи это не различить.
            back = self.inbox.expected_screen(user_id) or (
                "video" if want == VIDEO else "photo"
            )
            self.inbox.disarm(user_id)
            template = GOT_VIDEO if want == VIDEO else GOT_VOICE
            await self._send(chat_id, template.format(seconds=item.seconds),
                             open_app=back)
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

    async def _say(self, event, text: str, open_app: str = "") -> None:
        chat_id = chat_id_of(event)
        if chat_id is None:
            log.warning("не нашли, куда отвечать: %s", type(event).__name__)
            return
        await self._send(chat_id, text, open_app)

    async def _send(self, chat_id: int, text: str, open_app: str = "") -> None:
        """Сообщение, при возможности с кнопкой возврата в приложение.

        Кнопка приятна, но не обязательна: если MAX её не принял, человек
        всё равно должен получить текст. Молчание тут хуже некрасивого.
        """
        if open_app:
            ways = self._ways_back(open_app)
            # Как только какой-то способ сработал — держимся за него: перебор
            # стоит по неудачному запросу к API на каждое сообщение.
            if self._good_way:
                ways = [w for w in ways if w[0] == self._good_way] or ways
            last = len(ways) - 1
            for number, (name, attachments) in enumerate(ways):
                try:
                    await self.bot.send_message(chat_id=chat_id, text=text,
                                                attachments=attachments)
                    if self._good_way != name:
                        log.info("кнопка возврата: работает вариант «%s»", name)
                        self._remember(name)
                    self._good_way = name
                    return
                except Exception as exc:  # noqa: BLE001
                    # Перебор — штатная работа, а не поломка. Тревожный тон
                    # уместен только когда кончились все варианты.
                    level = log.warning if number == last else log.info
                    level("вариант «%s» не подошёл%s: %s", name,
                          "" if number == last else ", пробую следующий", exc)
            self._good_way = ""
            log.error("ни один способ вернуть в приложение не сработал — шлём без кнопки. "
                      "Проверьте адрес в business.max.ru/self и MAX_WEBAPP_URL")
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

    def _ways_back(self, screen: str) -> list[tuple[str, list]]:
        """Чем вернуть человека в приложение, от лучшего к работающему.

        OpenAppButton открывает окно прямо в мессенджере, но MAX ищет адрес
        в своём реестре по точному совпадению и на расхождение отвечает
        «Link not found with pk = LinkPK{name=...}». Что именно там записано,
        снаружи не видно.

        Поэтому в запасе глубокая ссылка `max.ru/<бот>?startapp=<экран>` —
        обычная ссылка, никакого реестра, а payload приезжает в окно тем же
        start_param. Работает всегда, просто выглядит как ссылка, а не кнопка.
        """
        ways: list[tuple[str, list]] = []
        if self.webapp_url:
            base = self.webapp_url.rstrip("/")
            for url in dict.fromkeys([self.webapp_url, base + "/", base]):
                ways.append((f"приложение {url}", self.keyboard(
                    OpenAppButton(text="Открыть приложение", web_app=url, payload=screen))))
        if self.username:
            link = f"https://max.ru/{self.username}?startapp={screen}"
            ways.append((f"ссылка {link}", self.keyboard(
                LinkButton(text="Открыть приложение", url=link))))
        return ways

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
                attachments=self.keyboard(
                    CallbackButton(text="👎 Так себе", payload=f"bad:{feedback_key}")),
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
