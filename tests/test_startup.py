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


# --- секреты из окружения ----------------------------------------------------
# На сервере секреты приходят не из файла, а из EnvironmentFile юнита.
# Список префиксов, которые мы забираем из окружения, — белый, и забытый
# в нём префикс выглядит как «токен пуст» без единой подсказки, почему
# ровно тот же токен работает на машине разработчика. Так и случилось
# с телеграм-ботом: локально токен лежал в файле секретов и проходил,
# на сервере приходил из окружения и молча терялся.

@pytest.mark.parametrize("name", [
    "MAX_BOT_TOKEN", "TELEGRAM_BOT_TOKEN", "H3_API_KEY",
    "KANDINSKY_TOKEN", "MINIAPP_DATA_DIR", "MINIAPP_MESSENGER",
])
def test_secret_from_the_environment_reaches_settings(monkeypatch, tmp_path, name):
    from avatar_core.config import load_secrets

    monkeypatch.setenv(name, "значение-из-юнита")
    values = load_secrets(tmp_path / "нет-такого-файла.env")
    assert values.get(name) == "значение-из-юнита", (
        f"{name} не доехал из окружения — проверьте список префиксов "
        "в avatar_core.config.load_secrets"
    )


def test_telegram_token_lands_in_settings(monkeypatch, tmp_path):
    """Сквозная проверка того самого случая: мессенджер и токен приходят
    только из окружения, файла секретов нет вовсе."""
    from avatar_miniapp import config as appconfig

    monkeypatch.setenv("MINIAPP_MESSENGER", "telegram")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:AA-токен")
    monkeypatch.setenv("MINIAPP_DATA_DIR", str(tmp_path))
    settings = appconfig.load(tmp_path / "нет-такого-файла.env")
    assert settings.messenger == "telegram"
    assert settings.bot_token == "123:AA-токен"
    assert settings.chat_enabled, settings.warnings


def test_a_foreign_variable_is_not_picked_up(monkeypatch, tmp_path):
    """Список белый намеренно: тянуть в настройки всё окружение процесса
    значит однажды подхватить чужой PATH или пароль соседнего сервиса."""
    from avatar_core.config import load_secrets

    monkeypatch.setenv("SOME_OTHER_SECRET", "не наше")
    values = load_secrets(tmp_path / "нет-такого-файла.env")
    assert "SOME_OTHER_SECRET" not in values
