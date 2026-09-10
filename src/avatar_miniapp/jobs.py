"""Задачи: очередь к модели и жизнь после перезапуска.

Две разные вещи, и путать их нельзя:

  * одна активная задача НА ЧЕЛОВЕКА — иначе человек загрузит второе фото
    и получит в чат первое;
  * одна задача В МОДЕЛИ на всех — у H3 конкурентность 1, и он падает
    по памяти, если гнать подряд.

Генерация подставляется снаружи (`runner`), поэтому на первом шаге это
заглушка с паузой, а на пятом — настоящий H3, и ничего вокруг не меняется.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger("miniapp.jobs")

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
ACTIVE = (QUEUED, RUNNING)

DEFAULT_FAIL = "Не получилось сделать ролик. Попробуйте ещё раз через пару минут."


@dataclass
class Job:
    job_id: str
    user_id: int
    mode: str                     # photo | video | toon
    text: str = ""
    status: str = QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    progress: int = 0
    provider_job_id: str = ""
    inputs: dict = field(default_factory=dict)   # пути к подготовленным файлам
    media_name: str = ""                          # имя результата в media_dir
    # Рисованный портрет: готов на середине пути и показывается в окне,
    # пока H3 доделывает ролик. Полминуты ожидания превращаются в «о, это я».
    poster_name: str = ""
    error: str = ""          # техническое, в лог и в файл задачи
    user_message: str = ""   # то, что не стыдно показать человеку
    delivered: bool = False

    def public(self, position: int | None = None) -> dict:
        body = {
            "job_id": self.job_id,
            "mode": self.mode,
            "status": self.status,
            "progress": self.progress,
            "waited_s": round((self.finished_at or time.time()) - self.created_at),
            "media_url": f"/media/{self.media_name}" if self.media_name else None,
            "poster_url": f"/media/{self.poster_name}" if self.poster_name else None,
            "error": self.user_message or ("" if self.status != FAILED else DEFAULT_FAIL),
        }
        if position is not None:
            body["queue_position"] = position
        return body


class UserError(Exception):
    """Отказ, который можно показать человеку дословно.

    Всё остальное наружу не идёт: в тексте обычного исключения бывает
    трассировка чужого воркера, путь на диске или кусок ответа API —
    человеку это ничего не объясняет, а нам лишнее в чате.
    """


class JobBusy(Exception):
    """У человека уже есть активная задача. Наружу — 409 с её телом."""

    def __init__(self, job: Job) -> None:
        super().__init__("уже есть активная задача")
        self.job = job


Runner = Callable[[Job], Awaitable[None]]


class JobManager:
    def __init__(self, jobs_dir: Path, runner: Runner, *, concurrency: int = 1) -> None:
        self.dir = jobs_dir
        self.runner = runner
        self.jobs: dict[str, Job] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._concurrency = concurrency
        self._on_done: Callable[[Job], Awaitable[None]] | None = None

    # --- жизненный цикл -------------------------------------------------

    def set_delivery(self, callback: Callable[[Job], Awaitable[None]]) -> None:
        """Куда отдавать готовое. Доставка всегда в чат, окно тут ни при чём."""
        self._on_done = callback

    async def start(self) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._resume_from_disk()
        for n in range(self._concurrency):
            self._workers.append(asyncio.create_task(self._worker(n), name=f"job-worker-{n}"))

    async def stop(self) -> None:
        for task in self._workers:
            task.cancel()
        for task in self._workers:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._workers.clear()

    def _resume_from_disk(self) -> None:
        """Задачи переживают деплой: файл пишется до начала ожидания."""
        for path in sorted(self.dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                job = Job(**data)
            except (OSError, ValueError, TypeError) as exc:
                log.warning("не читается %s: %s", path.name, exc)
                continue
            if job.status in ACTIVE:
                # Модель уже могла всё посчитать, пока нас не было. Ставим
                # обратно в очередь: воркер сначала смотрит provider_job_id
                # и дожидается, а не начинает заново и не тратит генерацию.
                job.status = QUEUED
                self.jobs[job.job_id] = job
                self._queue.put_nowait(job.job_id)
                log.info("подобрали незавершённую задачу %s", job.job_id)
            elif not job.delivered:
                self.jobs[job.job_id] = job
                log.info("задача %s готова, но не доставлена", job.job_id)

    def _save(self, job: Job) -> None:
        tmp = self.dir / f"{job.job_id}.json.tmp"
        tmp.write_text(json.dumps(asdict(job), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.dir / f"{job.job_id}.json")

    # --- работа с задачами ----------------------------------------------

    def active_of(self, user_id: int) -> Job | None:
        for job in self.jobs.values():
            if job.user_id == user_id and job.status in ACTIVE:
                return job
        return None

    def position_of(self, job: Job) -> int:
        """Сколько задач впереди. Считаем по времени постановки, а не по очереди:
        очередь asyncio не умеет в себя заглядывать."""
        if job.status is RUNNING:
            return 0
        earlier = [
            other for other in self.jobs.values()
            if other.status in ACTIVE and other.created_at < job.created_at
        ]
        return len(earlier)

    def create(self, user_id: int, mode: str, text: str, inputs: dict) -> Job:
        busy = self.active_of(user_id)
        if busy is not None:
            raise JobBusy(busy)
        job = Job(job_id=uuid.uuid4().hex[:12], user_id=user_id, mode=mode,
                  text=text, inputs=inputs)
        self.jobs[job.job_id] = job
        self._save(job)
        self._queue.put_nowait(job.job_id)
        log.info("%s: задача поставлена (%s, %d в очереди)",
                 job.job_id, mode, self._queue.qsize())
        return job

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def mark_delivered(self, job: Job) -> None:
        job.delivered = True
        self._save(job)

    async def deliver_pending(self) -> None:
        """Дослать то, что доделалось перед самым перезапуском.

        Обещание продукта — результат приходит в чат. Ролик, готовый за секунду
        до деплоя, иначе не дойдёт никогда: окно человек давно закрыл.
        Вызывается после того, как чат-часть поднялась.
        """
        if self._on_done is None:
            return
        pending = [j for j in self.jobs.values()
                   if j.status in (DONE, FAILED) and not j.delivered]
        for job in pending:
            log.info("%s: досылаю результат после перезапуска", job.job_id)
            try:
                await self._on_done(job)
            except Exception as exc:  # noqa: BLE001
                log.error("%s: дослать не удалось: %s", job.job_id, exc)

    # --- воркер ----------------------------------------------------------

    async def _worker(self, number: int) -> None:
        while True:
            job_id = await self._queue.get()
            job = self.jobs.get(job_id)
            if job is None or job.status not in ACTIVE:
                self._queue.task_done()
                continue
            job.status = RUNNING
            job.started_at = time.time()
            self._save(job)
            try:
                await self.runner(job)
                job.status = DONE
                job.progress = 100
            except asyncio.CancelledError:
                # Гасят сервис. Оставляем задачу активной: после старта подберём.
                job.status = QUEUED
                self._save(job)
                raise
            except Exception as exc:  # noqa: BLE001 — падение задачи не роняет воркер
                job.status = FAILED
                job.error = f"{type(exc).__name__}: {exc}"
                job.user_message = str(exc) if isinstance(exc, UserError) else ""
                log.error("%s: %s: %s", job.job_id, type(exc).__name__, exc)
                log.debug("%s: полный след", job.job_id, exc_info=True)
            finally:
                if job.status in (DONE, FAILED):
                    job.finished_at = time.time()
                    self._save(job)
                self._queue.task_done()

            if job.status in (DONE, FAILED) and self._on_done is not None:
                try:
                    await self._on_done(job)
                except Exception as exc:  # noqa: BLE001
                    # Доставка отвалилась — результат на диске, скажем об этом
                    # в лог. Терять задачу из-за упавшего мессенджера незачем.
                    log.error("%s: доставить не удалось: %s", job.job_id, exc)
