"""Точка входа.

Веб-сервер и чат-часть поднимаются В ОДНОМ процессе: разносить их нельзя,
иначе понадобится второй токен, а второго бота у нас нет.

    python -m avatar_miniapp
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys
from pathlib import Path

from aiohttp import web

from . import config
from .api import build_app
from .chat import ChatSide
from .jobs import DONE, Job, JobManager

log = logging.getLogger("miniapp")


def setup_logging(verbose: bool = False) -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
        stream=sys.stdout,
    )
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)


def make_stub_runner(media_dir: Path, seconds: float = 20.0):
    """Заглушка генерации на первом шаге.

    Настоящий H3 подключается на пятом шаге и заменяет только эту функцию —
    всё вокруг (очередь, 409, переживание рестарта, доставка) отлаживается
    без единой потраченной генерации.
    """
    sample = config.STATIC_DIR / "sample.mp4"

    async def run(job: Job) -> None:
        for step in range(1, 11):
            await asyncio.sleep(seconds / 10)
            job.progress = step * 10
        if not sample.is_file():
            raise RuntimeError(
                f"нет образца {sample.name} — положите любой mp4 в static/, "
                "заглушке нечего отдать"
            )
        name = f"{job.job_id}.mp4"
        shutil.copyfile(sample, media_dir / name)
        job.media_name = name

    return run


def load_voices() -> list[dict]:
    """Готовые голоса. На первом шаге — заглушки, файлы подложим на шестом."""
    return [
        {"id": "anya", "title": "Аня", "note": "женский, спокойный"},
        {"id": "sergey", "title": "Сергей", "note": "мужской, деловой"},
        {"id": "andrey", "title": "Андрей", "note": "мужской, живой"},
    ]


def main(argv: list[str] | None = None) -> int:
    setup_logging("-v" in (argv or sys.argv[1:]))
    settings = config.load()
    settings.ensure_dirs()

    for warning in settings.warnings:
        log.warning("%s", warning)
    log.info("секреты: %s", settings.secrets_path)
    log.info("данные:  %s", settings.data_dir)
    if settings.chat_enabled:
        log.info("токен:   %s", settings.token_hint)

    jobs = JobManager(settings.jobs_dir, make_stub_runner(settings.media_dir), concurrency=1)
    app = build_app(settings, jobs, load_voices())

    chat: ChatSide | None = None
    if settings.chat_enabled:
        chat = ChatSide(settings.bot_token, settings.webapp_url)
        app["chat"] = chat

    async def deliver(job: Job) -> None:
        """Результат уходит в чат ВСЕГДА, открыто окно или нет.

        Догнать окно нечем: ни push в него, ни переоткрытия в API нет.
        Человек, закрывший окно за секунду до готовности, иначе теряет ролик.
        """
        if chat is None:
            log.info("%s: чат выключен, доставка пропущена", job.job_id)
            return
        if job.status == DONE and job.media_name:
            await chat.deliver(
                job.user_id,
                settings.media_dir / job.media_name,
                caption="Готово!",
                feedback_key=job.job_id,
            )
        else:
            await chat.say_failed(job.user_id)
        jobs.mark_delivered(job)

    jobs.set_delivery(deliver)
    app.on_startup.append(lambda _: jobs.start())
    app.on_cleanup.append(lambda _: jobs.stop())

    try:
        asyncio.run(serve(app, settings, jobs, chat))
    except PortBusy as exc:
        log.error("%s", exc)
        return 1
    except KeyboardInterrupt:
        pass
    return 0


class PortBusy(RuntimeError):
    pass


async def serve(app: web.Application, settings, jobs: JobManager, chat: ChatSide | None) -> None:
    """Поднимаем в правильном порядке: сначала порт, потом бот.

    Порт — самое вероятное место отказа (второй экземпляр, соседний сервис).
    Если начать с бота, мы уже займём токен long polling'ом и только потом
    упадём на bind. Один токен = один процесс, и такие «полсекунды вторым
    процессом» — ровно тот случай, из-за которого бот отвечает через раз.
    """
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, settings.host, settings.port)
    try:
        await site.start()
    except OSError as exc:
        await runner.cleanup()
        raise PortBusy(
            f"Порт {settings.port} занят — скорее всего, приложение уже запущено "
            f"в другом окне.\n"
            f"  Windows: netstat -ano | findstr :{settings.port}   затем  taskkill /PID <номер> /F\n"
            f"  Linux:   sudo ss -tlnp | grep :{settings.port}\n"
            f"Либо задайте другой порт: MINIAPP_PORT=<число> в secrets.env.\n"
            f"Исходная ошибка: {exc}"
        ) from exc

    log.info("слушаю http://%s:%d", settings.host, settings.port)
    if chat is not None:
        await chat.start()
        # Ролик, доделанный перед самым перезапуском, всё равно должен доехать:
        # окна у человека может уже не быть, а обещали мы доставку в чат.
        await jobs.deliver_pending()

    stop = asyncio.Event()
    try:
        await stop.wait()
    finally:
        if chat is not None:
            await chat.stop()
        await runner.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
