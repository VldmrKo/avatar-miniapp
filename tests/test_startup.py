"""Порядок запуска и досылка после перезапуска.

Оба сюжета встречались вживую: занятый порт (второй экземпляр в другом окне)
и ролик, доделавшийся перед самой остановкой сервиса.
"""

from __future__ import annotations

import asyncio

import pytest
from aiohttp import web
from avatar_miniapp.__main__ import PortBusy, serve
from avatar_miniapp.config import Settings
from avatar_miniapp.jobs import DONE, FAILED, Job, JobManager


class FakeChat:
    """Считает, трогали ли бота. Настоящий MAX тут не нужен."""

    def __init__(self) -> None:
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


@pytest.mark.asyncio
async def test_busy_port_does_not_touch_the_bot(tmp_path, unused_tcp_port):
    """Если начать с бота, мы займём токен и только потом упадём на bind.

    Один токен = один процесс long polling, поэтому даже секунда вторым
    процессом — это уже поделённые пополам апдейты.
    """
    settings = Settings(data_dir=tmp_path, port=unused_tcp_port)
    settings.ensure_dirs()

    # Занимаем порт заранее — ровно то, что делает второе окно run-local.bat.
    squatter = await asyncio.start_server(lambda r, w: None, settings.host, settings.port)
    chat = FakeChat()
    try:
        with pytest.raises(PortBusy) as caught:
            await serve(web.Application(), settings, JobManager(settings.jobs_dir, _noop), chat)
        assert str(settings.port) in str(caught.value)
        assert chat.started is False, "бот не должен подниматься при занятом порте"
    finally:
        squatter.close()
        await squatter.wait_closed()


async def _noop(job: Job) -> None:
    return None


@pytest.mark.asyncio
async def test_pending_result_is_delivered_after_restart(tmp_path):
    """Ролик, готовый за секунду до перезапуска, всё равно должен доехать."""
    settings = Settings(data_dir=tmp_path)
    settings.ensure_dirs()

    first = JobManager(settings.jobs_dir, _noop)
    stale = Job(job_id="abc123", user_id=777, mode="photo", status=DONE,
                media_name="abc123.mp4", delivered=False)
    first.jobs[stale.job_id] = stale
    first._save(stale)

    delivered: list[Job] = []
    second = JobManager(settings.jobs_dir, _noop)

    async def deliver(job: Job) -> None:
        delivered.append(job)
        second.mark_delivered(job)

    second.set_delivery(deliver)
    await second.start()
    try:
        await second.deliver_pending()
        assert [j.job_id for j in delivered] == ["abc123"]
        # Второй запуск не должен слать то же самое ещё раз.
        delivered.clear()
        third = JobManager(settings.jobs_dir, _noop)
        third.set_delivery(deliver)
        await third.start()
        await third.deliver_pending()
        assert delivered == []
        await third.stop()
    finally:
        await second.stop()


@pytest.mark.asyncio
async def test_failed_job_is_also_reported(tmp_path):
    """Про отказ человеку тоже надо сказать, иначе он ждёт молча."""
    settings = Settings(data_dir=tmp_path)
    settings.ensure_dirs()
    manager = JobManager(settings.jobs_dir, _noop)
    broken = Job(job_id="def456", user_id=777, mode="photo", status=FAILED,
                 error="модель занята", delivered=False)
    manager.jobs[broken.job_id] = broken

    seen: list[Job] = []
    manager.set_delivery(lambda job: _collect(seen, job))
    await manager.deliver_pending()
    assert [j.job_id for j in seen] == ["def456"]


async def _collect(bucket: list, job: Job) -> None:
    bucket.append(job)
