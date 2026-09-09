"""Сквозной путь без MAX и без модели.

Проверяются ровно те инварианты, на которых прошлый проект спотыкался:
401 без подписи, 409 на вторую задачу, восстановление окна через /api/state,
доставка в чат независимо от окна, и переживание перезапуска.
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest
from aiohttp.test_utils import TestClient, TestServer
from avatar_miniapp import initdata
from avatar_miniapp.api import build_app
from avatar_miniapp.config import Settings
from avatar_miniapp.jobs import DONE, Job, JobManager

TOKEN = "123:test"


def headers(user_id: int = 777) -> dict:
    fields = {
        "auth_date": str(int(time.time())),
        "user": json.dumps({"id": user_id, "first_name": "Аня"}, ensure_ascii=False),
    }
    return {initdata.HEADER: initdata.sign(fields, TOKEN)}


@pytest.fixture
def settings(tmp_path) -> Settings:
    s = Settings(bot_token=TOKEN, data_dir=tmp_path, dev_allow_unsigned=False)
    s.ensure_dirs()
    return s


async def _quick(job: Job) -> None:
    await asyncio.sleep(0.05)
    job.media_name = f"{job.job_id}.mp4"


async def _slow(job: Job) -> None:
    await asyncio.sleep(5)
    job.media_name = f"{job.job_id}.mp4"


async def make_client(settings: Settings, runner) -> tuple[TestClient, JobManager, list]:
    jobs = JobManager(settings.jobs_dir, runner, concurrency=1)
    delivered: list[Job] = []

    async def deliver(job: Job) -> None:
        delivered.append(job)
        jobs.mark_delivered(job)

    jobs.set_delivery(deliver)
    from avatar_miniapp.inbox import Inbox

    app = build_app(settings, jobs, [{"id": "anya", "title": "Аня", "note": ""}],
                    Inbox(settings.inbox_dir))
    app.on_startup.append(lambda _: jobs.start())
    app.on_cleanup.append(lambda _: jobs.stop())
    client = TestClient(TestServer(app))
    await client.start_server()
    return client, jobs, delivered


def form(mode="photo", text="Привет! Я аватар.", filename="face.png"):
    from aiohttp import FormData

    fd = FormData()
    fd.add_field("mode", mode)
    fd.add_field("text", text)
    fd.add_field("photo" if mode == "photo" else "video", b"\x00" * 32, filename=filename)
    return fd


@pytest.mark.asyncio
async def test_no_signature_is_401(settings):
    client, _, _ = await make_client(settings, _quick)
    try:
        assert (await client.get("/api/state")).status == 401
        assert (await client.post("/api/avatar", data=form())).status == 401
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_dev_flag_opens_the_door(settings):
    """Флаг существует, и это надо видеть в health — деплой на него смотрит."""
    settings.dev_allow_unsigned = True
    client, _, _ = await make_client(settings, _quick)
    try:
        assert (await client.get("/api/state")).status == 200
        body = await (await client.get("/api/health")).json()
        assert body["dev_unsigned"] is True
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_second_job_is_409_with_the_first(settings):
    client, _, _ = await make_client(settings, _slow)
    try:
        first = await (await client.post("/api/avatar", data=form(), headers=headers())).json()
        again = await client.post("/api/avatar", data=form(), headers=headers())
        assert again.status == 409
        # В теле должна быть ПЕРВАЯ задача: клиент по ней показывает прогресс.
        assert (await again.json())["job_id"] == first["job_id"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_other_person_is_not_blocked(settings):
    client, _, _ = await make_client(settings, _slow)
    try:
        await client.post("/api/avatar", data=form(), headers=headers(777))
        other = await client.post("/api/avatar", data=form(), headers=headers(888))
        assert other.status == 201
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_state_restores_the_window(settings):
    """Окно открывается с чистого листа: вернувшийся должен увидеть прогресс."""
    client, _, _ = await make_client(settings, _slow)
    try:
        created = await (await client.post("/api/avatar", data=form(), headers=headers())).json()
        state = await (await client.get("/api/state", headers=headers())).json()
        assert state["job"]["job_id"] == created["job_id"]
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_finished_job_is_not_offered_again(settings):
    """Готовое лежит в чате. Окно человек открыл, чтобы сделать следующее."""
    client, _, delivered = await make_client(settings, _quick)
    try:
        await client.post("/api/avatar", data=form(), headers=headers())
        for _ in range(40):
            await asyncio.sleep(0.05)
            if delivered:
                break
        assert delivered and delivered[0].status == DONE
        state = await (await client.get("/api/state", headers=headers())).json()
        assert state["job"] is None
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_long_text_is_refused_with_seconds(settings):
    """Лимит объясняется секундами, а не символами: ограничивает длительность."""
    client, _, _ = await make_client(settings, _quick)
    try:
        long_text = "Добрый день! " + "Я расскажу про аватары и как их делать. " * 6
        r = await client.post("/api/avatar", data=form(text=long_text), headers=headers())
        assert r.status == 400
        body = await r.json()
        assert body["speech_seconds"] > 14
        assert "сократите" in body["error"].lower()
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_foreign_job_is_invisible(settings):
    client, _, _ = await make_client(settings, _slow)
    try:
        mine = await (await client.post("/api/avatar", data=form(), headers=headers(777))).json()
        peek = await client.get(f"/api/jobs/{mine['job_id']}", headers=headers(888))
        assert peek.status == 404
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_unfinished_job_survives_restart(settings):
    """Файл пишется ДО ожидания, иначе деплой посреди генерации теряет задачу."""
    client, jobs, _ = await make_client(settings, _slow)
    try:
        created = await (await client.post("/api/avatar", data=form(), headers=headers())).json()
        assert (settings.jobs_dir / f"{created['job_id']}.json").is_file()
    finally:
        await client.close()

    # «Перезапуск»: новый менеджер поверх того же каталога.
    fresh = JobManager(settings.jobs_dir, _quick, concurrency=1)
    fresh.set_delivery(lambda job: asyncio.sleep(0))
    await fresh.start()
    try:
        assert fresh.active_of(777) is not None
    finally:
        await fresh.stop()
