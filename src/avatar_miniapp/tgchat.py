"""Тот же бот, но в Telegram.

ЗАЧЕМ. В MAX генерации заходят, а тестировщиков мало — просто потому,
что мессенджер непопулярный. Продукт от этого не проверяется. Telegram
даёт ту же функциональность на аудитории, которая у людей уже стоит.

ЧТО ОБЩЕГО. Всё, кроме этого файла. Сценарий разговора (`dialog.py`) не
знает ни про один мессенджер: отправка, сохранение файла и постановка
задачи приходят в него функциями снаружи. Очередь, генерация, подготовка
входов, тексты промптов — общие. Здесь только перевод с языка Telegram
на язык сценария и обратно.

ЧТО ПРОЩЕ, ЧЕМ В MAX. Голосовые доходят до бота как голосовые, с типом
и длительностью, — не нужно ни просить ролик вместо записи, ни доставать
дорожку, ни угадывать содержимое файла по видеопотоку. Вложения приходят
типизированными. Библиотека не теряет события молча, так что подпорки
вроде maxcompat здесь не нужно.

ЧТО СЛОЖНЕЕ. Бот не может скачать файл больше 20 МБ — это ограничение
Bot API, а не наше. Семисекундное видео с телефона в него укладывается
не всегда, поэтому отказ по размеру объясняем человеку словами.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import CommandStart
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

from .inbox import VIDEO, VOICE, Attachments

log = logging.getLogger("miniapp.tgchat")

GREETING = (
    "Привет! Я делаю говорящего аватара по вашему фото или видео.\n\n"
    "Напишите «/start» — покажу, что умею."
)
CANT_TAKE = "Не смог забрать файл. Попробуйте отправить ещё раз."
TOO_BIG = (
    "Файл больше 20 МБ — столько мне из Telegram не отдают, это его "
    "ограничение для ботов.\n"
    "Снимите ролик покороче или запишите голосовое — оно всегда лёгкое."
)
TOO_SHORT_VOICE = (
    "Запись короче двух секунд — модель такую не примет.\n"
    "Запишите ещё раз, скажите пару фраз."
)
TOO_SHORT_VIDEO = "Видео короче двух секунд. Снимите подлиннее, секунд на десять."
FAILED = (
    "Не получилось сделать ролик. Это бывает, когда модель занята — "
    "попробуйте ещё раз через пару минут."
)

# Оценка ролика. Порядок слева направо — от лучшего к худшему.
RATINGS = (("good", "👍 Отлично"), ("ok", "😐 Нормально"), ("bad", "👎 Так себе"))
RATING_MARKS = {"good": "👍", "ok": "😐", "bad": "👎"}

# Столько отдаёт Bot API по getFile. Больше — только своим сервером API,
# а его мы ради MVP поднимать не будем.
MAX_DOWNLOAD_MB = 20


class TelegramSide:
    """Поверхность ровно та же, что у чат-части MAX.

    Одинаковые имена здесь не для красоты: точка входа собирает бота, не
    зная, какой мессенджер перед ней, и любое расхождение всплывёт только
    на запуске в бою.
    """

    def __init__(self, token: str, media: Attachments | None = None,
                 conversation=None, proxy: str = "", ipv4_only: bool = False) -> None:
        # Прокси нужен там, откуда до Telegram не дозвониться напрямую.
        # Через него идёт ВСЁ: и опрос обновлений, и отправка, и скачивание
        # присланных файлов, — поэтому он задаётся сессии целиком, а не
        # отдельным вызовам.
        session = AiohttpSession(proxy=proxy) if proxy else None
        if proxy:
            log.info("Telegram через прокси %s", _hide_password(proxy))
        if ipv4_only:
            # У api.telegram.org есть адрес IPv6, и он в DNS идёт первым.
            # На машине без IPv6 попытка не отваливается сразу, а висит до
            # таймаута — со стороны неотличимо от блокировки Telegram.
            session = session or AiohttpSession()
            session._connector_init["family"] = socket.AF_INET
            log.info("Telegram только по IPv4")
        self.bot = Bot(token, session=session) if session else Bot(token)
        self.dp = Dispatcher()
        self.media = media or Attachments()
        self.conversation = conversation
        self.username = ""
        self._task: asyncio.Task | None = None
        self._register()

    # --- обработчики ------------------------------------------------------

    def _register(self) -> None:
        dp = self.dp

        @dp.message(CommandStart())
        async def _start(message: Message) -> None:
            await self._menu(message)

        @dp.callback_query(F.data == "again")
        async def _again(call: CallbackQuery) -> None:
            """«Сделать ещё» после готового ролика — снова в меню."""
            await self._ack(call)
            if call.message:
                await self.conversation.begin(call.message.chat.id,
                                              call.from_user.id, greet=False)

        @dp.callback_query(F.data.startswith("rate:"))
        async def _rate(call: CallbackQuery) -> None:
            _, _, rest = (call.data or "").partition(":")
            score, _, key = rest.partition(":")
            log.info("%s по задаче %s", RATING_MARKS.get(score, "оценка ?"), key)
            await self._ack(call, "Спасибо!")

        @dp.callback_query()
        async def _step(call: CallbackQuery) -> None:
            await self._ack(call)
            if self.conversation and call.message:
                await self.conversation.on_button(
                    call.message.chat.id, call.from_user.id, call.data or "")

        # Ловим всё остальное. Регистрируется ПОСЛЕДНИМ: зарегистрированный
        # раньше catch-all перехватил бы и /start.
        @dp.message()
        async def _anything(message: Message) -> None:
            if not self.conversation:
                await message.answer(GREETING)
                return
            chat_id, user_id = message.chat.id, message.from_user.id
            if await self._take_attachment(message, chat_id, user_id):
                return
            text = message.text or message.caption or ""
            if await self.conversation.on_text(chat_id, user_id, text):
                return
            # Ничего не ждём — показываем меню. Человеку, написавшему боту
            # «привет», нужен следующий шаг, а не отповедь.
            await self.conversation.begin(chat_id, user_id, greet=False)

    async def _ack(self, call: CallbackQuery, text: str = "") -> None:
        """Погасить «часики» на кнопке.

        Сообщение живёт в переписке неделями, а мы за это время
        перезапускались, поэтому устаревший callback — не повод падать.
        """
        try:
            await call.answer(text or None)
        except Exception as exc:  # noqa: BLE001
            log.debug("ack не прошёл: %s", exc)

    async def _menu(self, message: Message) -> None:
        if self.conversation:
            await self.conversation.begin(message.chat.id, message.from_user.id)
            return
        await message.answer(GREETING)

    # --- приём вложений ---------------------------------------------------

    async def _take_attachment(self, message: Message, chat_id: int,
                               user_id: int) -> bool:
        """True — вложение было и мы им занялись.

        В отличие от MAX гадать не приходится: Telegram сам говорит, что
        именно приехало. Голосовое здесь ещё и с длительностью, так что
        «короче двух секунд» ловится без ffprobe.
        """
        kind, item, suffix = _what_came(message)
        if not kind:
            return False

        step = self.conversation.waiting_for(user_id)
        if not step:
            # Вложение без начатого разговора приложить не к чему.
            await self.conversation.begin(chat_id, user_id, greet=False)
            return True

        size = getattr(item, "file_size", 0) or 0
        if size > MAX_DOWNLOAD_MB * 1024 * 1024:
            log.info("%s: файл %.0f МБ — Bot API его не отдаст",
                     user_id, size / 1_048_576)
            await self.bot.send_message(chat_id, TOO_BIG)
            return True

        data = await self._download(item.file_id)
        if data is None:
            await self.bot.send_message(chat_id, CANT_TAKE)
            return True

        if kind in (VOICE, VIDEO):
            # Длительность Telegram присылает сам; ffprobe нужен, только
            # если это документ и в нём её нет.
            seconds = getattr(item, "duration", 0) or self.media.seconds(data, suffix)
            if seconds and seconds < 2:
                await self.bot.send_message(
                    chat_id, TOO_SHORT_VIDEO if kind == VIDEO else TOO_SHORT_VOICE)
                return True

        # Голос сценарий ждёт голосом. Если человек на этом шаге прислал
        # ролик — берём звуковую дорожку, как делали и в MAX.
        if step == "voice" and kind == VIDEO:
            sound = self.media.audio_from(data, suffix)
            if sound:
                data, suffix, kind = sound, ".wav", VOICE

        await self.conversation.on_file(chat_id, user_id, kind, data, suffix)
        return True

    async def _download(self, file_id: str) -> bytes | None:
        try:
            buffer = await self.bot.download(file_id)
            return buffer.read() if buffer else None
        except Exception as exc:  # noqa: BLE001
            log.warning("не скачали %s: %s", file_id, exc)
            return None

    # --- отправка ---------------------------------------------------------

    async def ask(self, chat_id: int, text: str, buttons: list) -> None:
        """Сообщение сценария: текст плюс кнопки-ответы, по одной в ряд."""
        markup = None
        if buttons:
            markup = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=title, callback_data=payload)]
                for title, payload in buttons
            ])
        try:
            await self.bot.send_message(chat_id, text, reply_markup=markup)
        except Exception as exc:  # noqa: BLE001
            log.error("сценарий: не отправилось в %s: %s", chat_id, exc)

    async def deliver(self, user_id: int, video: Path, caption: str, feedback_key: str,
                      poster: Path | None = None) -> None:
        """Результат — в личку.

        Порядок тот же, что и в MAX: сначала ролик, потом портрет, потом
        кнопки. Главное первым: если следующее сообщение не уйдёт, человек
        уже получил то, ради чего всё затевалось.
        """
        await self.bot.send_video(
            user_id, BufferedInputFile(video.read_bytes(), filename="avatar.mp4"),
            caption=caption,
        )

        if poster is not None and poster.is_file():
            try:
                await self.bot.send_photo(
                    user_id,
                    BufferedInputFile(poster.read_bytes(), filename="avatar.png"),
                    caption="И сам портрет — можно поставить на аватарку.",
                )
            except Exception as exc:  # noqa: BLE001
                # Картинка — бонус. Не ушла, а ролик человек всё равно получил.
                log.warning("портрет не ушёл: %s", exc)

        try:
            await self.bot.send_message(
                user_id, "Как получилось?",
                # По кнопке в ряд. В строку они не влезают: на телефоне
                # «Нормально» обрезается до «Нормальнс», и выглядит это
                # не как компактность, а как поломка.
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    *[[InlineKeyboardButton(
                        text=title, callback_data=f"rate:{score}:{feedback_key}")]
                      for score, title in RATINGS],
                    [InlineKeyboardButton(text="🔁 Сделать ещё", callback_data="again")],
                ]),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("кнопка обратной связи не ушла: %s", exc)

    async def say_failed(self, user_id: int, reason: str = "") -> None:
        try:
            await self.bot.send_message(user_id, reason.strip() or FAILED)
        except Exception as exc:  # noqa: BLE001
            log.error("не отправилось про отказ: %s", exc)

    # --- жизненный цикл ---------------------------------------------------

    async def start(self) -> None:
        try:
            me = await self.bot.get_me()
        except Exception as exc:
            # Сессию за собой закрываем сами: иначе поверх настоящей причины
            # ляжет ещё и «Unclosed client session», и читать это будут снизу.
            await self.bot.session.close()
            raise RuntimeError(_why_no_telegram(exc)) from exc
        self.username = me.username or ""
        log.info("бот @%s на связи", self.username or "без имени")
        self._task = asyncio.create_task(self.dp.start_polling(self.bot),
                                         name="tg-polling")

    async def stop(self) -> None:
        if self._task:
            await self.dp.stop_polling()
            self._task.cancel()
            self._task = None
        await self.bot.session.close()


def _hide_password(proxy: str) -> str:
    """Прокси в лог — без пароля: журнал читают и пересылают."""
    if "@" not in proxy:
        return proxy
    head, _, tail = proxy.rpartition("@")
    scheme, sep, creds = head.partition("://")
    user = creds.split(":", 1)[0] if ":" in creds else creds
    return f"{scheme}{sep}{user}:…@{tail}"


def _why_no_telegram(exc: Exception) -> str:
    """Перевести сетевой отказ в то, что человеку делать.

    Оба частых случая выглядят одинаково — «не смог подключиться», — но
    лечатся противоположно, и трассировка на сорок строк об этом молчит.
    """
    text = str(exc)
    if "CERTIFICATE_VERIFY_FAILED" in text or "certificate" in text.lower():
        return (
            "Telegram отвечает, но его сертификат не проходит проверку. "
            "Так выглядит корпоративная сеть, которая вскрывает TLS своим "
            "корневым сертификатом: система ему доверяет, а Python смотрит "
            "в собственный набор и о нём не знает.\n"
            "Лечится доверием к хранилищу системы — MINIAPP_TRUST_OS_CERTS=1 "
            "и pip install truststore. Отключать проверку сертификатов не надо: "
            "это ровно та дверь, ради которой перехват и делают.\n"
            f"Исходная ошибка: {text}"
        )
    if "Unauthorized" in text or "401" in text:
        return ("Telegram не принял токен. Проверьте TELEGRAM_BOT_TOKEN — "
                f"это тот, что выдал @BotFather?\nИсходная ошибка: {text}")
    return (
        "Не достучались до api.telegram.org.\n"
        "Сначала проверьте IPv6 — он тут первый подозреваемый. У Telegram "
        "есть адрес IPv6, в DNS он идёт первым, и на машине без IPv6 "
        "попытка не отваливается, а висит до таймаута: со стороны "
        "неотличимо от блокировки.\n"
        "    curl -4 -m 10 https://api.telegram.org/\n"
        "Отвечает по -4 — поставьте TELEGRAM_IPV4_ONLY=1, и всё.\n"
        "Не отвечает и так — значит режут сеть, и из кода это не обойти: "
        "нужен прокси (TELEGRAM_PROXY=http://… или socks5://…) либо "
        f"площадка, с которой Telegram доступен.\nИсходная ошибка: {text}"
    )


def _what_came(message: Message) -> tuple[str, object, str]:
    """Что за вложение и с каким расширением его сохранить.

    Порядок проверок — от самого однозначного к самому мутному.
    `document` идёт последним: им приезжает что угодно, включая
    присланное «файлом» видео.
    """
    if message.voice:
        return VOICE, message.voice, ".ogg"
    if message.audio:
        return VOICE, message.audio, ".m4a"
    if message.video_note:
        # Кружок — законный образец голоса и лица: короткий и всегда с речью.
        return VIDEO, message.video_note, ".mp4"
    if message.video:
        return VIDEO, message.video, ".mp4"
    if message.photo:
        # Список размеров от мелкого к крупному; берём самый большой.
        return "photo", message.photo[-1], ".jpg"
    if message.document:
        name = (message.document.file_name or "").lower()
        mime = (message.document.mime_type or "").lower()
        if mime.startswith("image/") or name.endswith((".jpg", ".jpeg", ".png")):
            return "photo", message.document, Path(name).suffix or ".jpg"
        if mime.startswith("video/") or name.endswith((".mp4", ".mov")):
            return VIDEO, message.document, Path(name).suffix or ".mp4"
        if mime.startswith("audio/"):
            return VOICE, message.document, Path(name).suffix or ".m4a"
    return "", None, ""
