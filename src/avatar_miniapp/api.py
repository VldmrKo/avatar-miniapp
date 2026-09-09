"""HTTP-часть: экраны и эндпоинты.

Долгую операцию нельзя держать открытым запросом, поэтому POST мгновенно
возвращает job_id, а окно опрашивает статус. Ключевые решения — в jobs.py.
"""

from __future__ import annotations

import logging

from aiohttp import web

from . import initdata
from .config import STATIC_DIR, Settings
from .jobs import JobBusy, JobManager

log = logging.getLogger("miniapp.api")

MAX_UPLOAD_MB = 100
MAX_SPEECH_SECONDS = 14.0

routes = web.RouteTableDef()


def caller_of(request: web.Request) -> initdata.Caller:
    """Кто пришёл. Подпись проверяется на каждом запросе, сессий нет."""
    settings: Settings = request.app["settings"]
    raw = request.headers.get(initdata.HEADER, "")
    try:
        return initdata.parse(
            raw, settings.bot_token,
            max_age_s=settings.init_data_max_age_s,
            future_skew_s=settings.init_data_future_skew_s,
        )
    except initdata.InitDataError as exc:
        if settings.dev_allow_unsigned:
            log.warning("пускаю без подписи (%s) — так можно только локально", exc)
            return initdata.Caller(user_id=0, first_name="локальный", signed=False)
        # Наружу не рассказываем, что именно не сошлось.
        raise web.HTTPUnauthorized(text='{"error":"нет подписи"}',
                                   content_type="application/json") from exc


@routes.get("/")
async def index(request: web.Request) -> web.StreamResponse:
    # Без явного заголовка вебвью MAX показывает прежнюю версию окна после
    # выкладки, и по логам этого не видно. no-cache, а не no-store: ETag
    # продолжает работать и неизменившийся файл приедет как 304 без тела.
    return web.FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@routes.get("/api/health")
async def health(request: web.Request) -> web.Response:
    settings: Settings = request.app["settings"]
    jobs: JobManager = request.app["jobs"]
    chat = request.app.get("chat")
    return web.json_response({
        "ok": True,
        # Деплой не считается успешным, если здесь true.
        "dev_unsigned": settings.dev_allow_unsigned,
        "chat": bool(chat),
        "bot": getattr(chat, "username", "") if chat else "",
        "jobs_active": sum(1 for j in jobs.jobs.values() if j.status in ("queued", "running")),
    })


@routes.get("/api/state")
async def state(request: web.Request) -> web.Response:
    """Что показать при открытии окна.

    Окно открывается с чистого листа каждый раз, и человек, вернувшийся
    в середине генерации, должен увидеть прогресс, а не пустой экран выбора.
    Готовые задачи специально не отдаём: результат уже в чате, а окно человек
    открыл, чтобы сделать следующую.
    """
    jobs: JobManager = request.app["jobs"]
    who = caller_of(request)
    job = jobs.active_of(who.user_id)
    inbox = request.app["inbox"]
    return web.json_response({
        "name": who.display_name,
        "job": job.public(jobs.position_of(job)) if job else None,
        # Что человек уже прислал боту в чат. Окно не умеет ни писать голос,
        # ни снимать видео — этим занимается сам мессенджер.
        "inbox": inbox.public(who.user_id) if inbox else {},
    })


@routes.get("/api/voices")
async def voices(request: web.Request) -> web.Response:
    """Читаем каталог на каждый запрос.

    Добавить голос должно значить «положить wav и всё». Перезапуск ради
    новой строки в списке — ровно та мелочь, из-за которой потом полчаса
    ищут, почему файл лежит, а в окне пусто.
    """
    return web.json_response({"voices": request.app["read_voices"]()})


@routes.post("/api/inbox/expect")
async def expect(request: web.Request) -> web.Response:
    """Окно сообщает боту, что сейчас придёт запись."""
    who = caller_of(request)
    inbox = request.app["inbox"]
    body = await request.json()
    kind = str(body.get("kind") or "")
    if kind not in ("voice", "video") or not inbox:
        return web.json_response({"error": "неизвестный вид записи"}, status=400)
    # «Записать заново» значит именно заново: старую запись убираем сразу.
    # Иначе человек передумает записывать, нажмёт «Сделать аватара» и молча
    # получит ролик по прошлому голосу.
    inbox.clear(who.user_id, kind)
    inbox.arm(who.user_id, kind)
    return web.json_response({"ok": True})


@routes.delete("/api/inbox/{kind}")
async def drop(request: web.Request) -> web.Response:
    who = caller_of(request)
    inbox = request.app["inbox"]
    kind = request.match_info["kind"]
    if kind not in ("voice", "video") or not inbox:
        return web.json_response({"error": "неизвестный вид записи"}, status=400)
    inbox.clear(who.user_id, kind)
    return web.json_response({"ok": True})


@routes.post("/api/avatar")
async def create(request: web.Request) -> web.Response:
    jobs: JobManager = request.app["jobs"]
    who = caller_of(request)

    if request.content_length and request.content_length > MAX_UPLOAD_MB * 1024 * 1024:
        raise web.HTTPRequestEntityTooLarge(
            max_size=MAX_UPLOAD_MB * 1024 * 1024, actual_size=request.content_length
        )

    mode, text, files = await _read_form(request)
    if mode not in ("photo", "video"):
        return web.json_response({"error": "неизвестный режим"}, status=400)

    # Голос и видео могли приехать не через окно, а голосовым или роликом
    # боту в чат — для MAX это единственный способ что-то записать.
    inbox = request.app["inbox"]
    if inbox:
        if mode == "photo" and not files.get("voice") and not files.get("voice_id"):
            item = inbox.get(who.user_id, "voice")
            if item:
                files["voice"] = str(item.path)
        if mode == "video" and not files.get("video"):
            item = inbox.get(who.user_id, "video")
            if item:
                files["video"] = str(item.path)
                files["_from_inbox"] = "video"
    if not text.strip():
        return web.json_response({"error": "нужен текст реплики"}, status=400)

    seconds = _speech_seconds(text)
    if seconds > MAX_SPEECH_SECONDS:
        # Модель заполняет речью всё время ролика, а ролик не длиннее 15 с.
        # Значит длинный текст не «обрежется», а превратится в кашу.
        return web.json_response({
            "error": f"текст на {seconds:.0f} секунд, а ролик не длиннее "
                     f"{MAX_SPEECH_SECONDS:.0f}. Сократите примерно на "
                     f"{(seconds - MAX_SPEECH_SECONDS):.0f} с.",
            "speech_seconds": round(seconds, 1),
        }, status=400)

    try:
        job = jobs.create(who.user_id, mode, text, files)
    except JobBusy as busy:
        # 409 — это не ошибка, а «у тебя уже идёт». Клиент на него показывает
        # прогресс, а не красный экран.
        return web.json_response(busy.job.public(jobs.position_of(busy.job)), status=409)
    return web.json_response(job.public(jobs.position_of(job)), status=201)


@routes.get("/api/jobs/{job_id}")
async def job_status(request: web.Request) -> web.Response:
    jobs: JobManager = request.app["jobs"]
    who = caller_of(request)
    job = jobs.get(request.match_info["job_id"])
    if job is None or job.user_id != who.user_id:
        return web.json_response({"error": "нет такой задачи"}, status=404)
    return web.json_response(job.public(jobs.position_of(job)))


@routes.post("/api/estimate")
async def estimate(request: web.Request) -> web.Response:
    """Сколько секунд займёт текст. Счётчик в окне показывает секунды,
    а не символы: ограничивает нас именно длительность речи."""
    caller_of(request)
    body = await request.json()
    seconds = _speech_seconds(str(body.get("text", "")))
    return web.json_response({
        "speech_seconds": round(seconds, 1),
        "limit_seconds": MAX_SPEECH_SECONDS,
        "over": seconds > MAX_SPEECH_SECONDS,
    })


# --- служебное ---------------------------------------------------------------

def _speech_seconds(text: str) -> float:
    from avatar_core.speech import estimate_speech_seconds

    return estimate_speech_seconds(text)


async def _read_form(request: web.Request) -> tuple[str, str, dict]:
    """Разбор multipart с сохранением файлов на диск, а не в память.

    На ВМ меньше двух гигабайт памяти, и держать в ней сорокамегабайтное
    видео с телефона, да ещё вместе с его base64, — прямой путь к тому,
    что ядро прибьёт соседний сервис.
    """
    settings: Settings = request.app["settings"]
    upload_dir = settings.data_dir / "uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)

    mode, text, files = "", "", {}
    reader = await request.multipart()
    while True:
        part = await reader.next()
        if part is None:
            break
        if part.name == "mode":
            mode = (await part.text()).strip()
        elif part.name == "text":
            text = await part.text()
        elif part.name == "voice_id":
            files["voice_id"] = (await part.text()).strip()
        elif part.filename:
            import uuid

            suffix = "".join(c for c in part.filename[-12:] if c.isalnum() or c == ".")
            path = upload_dir / f"{uuid.uuid4().hex[:12]}_{suffix or 'upload'}"
            size = 0
            with path.open("wb") as fh:
                while chunk := await part.read_chunk():
                    size += len(chunk)
                    fh.write(chunk)
            files[part.name] = str(path)
            log.info("принят %s: %s, %.1f МБ", part.name, path.name, size / 1048576)
    return mode, text, files


def build_app(settings: Settings, jobs: JobManager, read_voices, inbox=None) -> web.Application:
    app = web.Application(client_max_size=MAX_UPLOAD_MB * 1024 * 1024)
    app["settings"] = settings
    app["jobs"] = jobs
    # Функция, а не список: каталог с голосами читается на каждый запрос.
    app["read_voices"] = read_voices if callable(read_voices) else (lambda: read_voices)
    app["inbox"] = inbox
    app.add_routes(routes)
    app.router.add_static("/static/", STATIC_DIR, name="static")
    app.router.add_static("/media/", settings.media_dir, name="media")
    return app
