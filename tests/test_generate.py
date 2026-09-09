"""Боевой путь против поддельного H3.

Настоящая модель стоит минуту чужого GPU, и отлаживать на ней проводку —
дорого и медленно. Здесь поднимается заглушка с теми же тремя ручками,
что у H3, и проверяется всё между загруженным файлом и готовым роликом:
подготовка входов, состав запроса, промпт, длительность и уборка за собой.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from aiohttp import web
from avatar_miniapp import generate, prepare
from avatar_miniapp.config import Settings
from avatar_miniapp.jobs import Job, UserError

pytestmark = pytest.mark.asyncio


# --- поддельный H3 -----------------------------------------------------------

class FakeH3:
    """Три ручки настоящего H3 и запомненное тело запроса."""

    def __init__(self, clip: bytes) -> None:
        self.clip = clip
        self.payload: dict = {}
        self.fail_first = 0
        self.app = web.Application(client_max_size=200 * 1024 * 1024)
        self.app.router.add_post("/v1/videos", self.submit)
        self.app.router.add_get("/v1/videos/{jid}", self.poll)
        self.app.router.add_get("/v1/videos/{jid}/content", self.content)

    async def submit(self, request: web.Request) -> web.Response:
        self.payload = await request.json()
        return web.json_response({"id": "fake-1"})

    async def poll(self, request: web.Request) -> web.Response:
        if self.fail_first > 0:
            self.fail_first -= 1
            return web.json_response({"status": "failed",
                                      "error": "CUDA out of memory"})
        return web.json_response({"status": "completed", "progress": 100})

    async def content(self, request: web.Request) -> web.Response:
        return web.Response(body=self.clip, content_type="video/mp4")


@pytest.fixture
async def h3(tmp_path):
    clip = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-nostdin", "-v", "error", "-f", "lavfi",
         "-i", "color=c=black:s=64x64:d=1", "-c:v", "libx264",
         "-pix_fmt", "yuv420p", str(clip)], check=True,
    )
    fake = FakeH3(clip.read_bytes())
    runner = web.AppRunner(fake.app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = runner.addresses[0][1]
    fake.url = f"http://127.0.0.1:{port}"
    yield fake
    await runner.cleanup()


@pytest.fixture
def media(tmp_path):
    """Фото и голос, годные для подачи в модель."""
    photo = tmp_path / "face.jpg"
    voice = tmp_path / "voice.wav"
    from PIL import Image

    Image.new("RGB", (800, 600), (30, 60, 120)).save(photo)
    subprocess.run(
        ["ffmpeg", "-y", "-nostdin", "-v", "error", "-f", "lavfi",
         "-i", "sine=f=220:d=6", "-ac", "1", "-ar", "44100", str(voice)], check=True,
    )
    return photo, voice


def make_settings(tmp_path, url: str) -> Settings:
    s = Settings(data_dir=tmp_path / "data", h3_base_url=url, h3_api_key="k")
    s.ensure_dirs()
    return s


# --- тесты -------------------------------------------------------------------

async def test_photo_job_end_to_end(tmp_path, h3, media):
    photo, voice = media
    settings = make_settings(tmp_path, h3.url)
    run = generate.make_runner(settings)
    job = Job(job_id="j1", user_id=7, mode="photo",
              text="Привет! Аватары — крутая тема.",
              inputs={"photo": str(photo), "voice": str(voice)})

    await run(job)

    assert job.media_name == "j1.mp4"
    assert (settings.media_dir / "j1.mp4").is_file()
    assert job.progress == 100


async def test_request_carries_what_the_model_needs(tmp_path, h3, media):
    photo, voice = media
    settings = make_settings(tmp_path, h3.url)
    await generate.make_runner(settings)(Job(
        job_id="j2", user_id=7, mode="photo",
        text="Привет! Аватары — крутая тема.",
        inputs={"photo": str(photo), "voice": str(voice)},
    ))

    body = h3.payload
    assert body["aspect_ratio"] == "9:16"
    assert len(body["reference_images"]) == 1
    assert len(body["reference_audios"]) == 1
    assert "reference_videos" not in body

    prompt = body["prompt"]
    # Ударение проставлено само: пользователь его не набирает.
    assert "Авата́ры" in prompt
    # Речь размечена тегом, иначе модель зачитает и всё остальное.
    assert "<d>[Russian]" in prompt and "</d>" in prompt
    # Длительность — под реплику. Просить больше значит просить отсебятину.
    assert 4 <= body["duration_seconds"] <= 6


async def test_video_job_sends_video_and_voice_from_it(tmp_path, h3):
    src = tmp_path / "phone.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-nostdin", "-v", "error",
         "-f", "lavfi", "-i", "testsrc=s=640x480:d=20",
         "-f", "lavfi", "-i", "sine=f=300:d=20",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
         "-shortest", str(src)], check=True,
    )
    settings = make_settings(tmp_path, h3.url)
    await generate.make_runner(settings)(Job(
        job_id="j3", user_id=7, mode="video", text="Привет!",
        inputs={"video": str(src)},
    ))

    body = h3.payload
    assert len(body["reference_videos"]) == 1
    # Голос вытаскиваем из самого видео — отдельным референсом, как в exp2.
    assert len(body["reference_audios"]) == 1
    assert "reference_images" not in body
    assert "<Video 1>" in body["prompt"]


async def test_transient_failure_is_retried(tmp_path, h3, media):
    """«Кончилась память GPU» через минуту уже неправда — повторяем сами."""
    photo, voice = media
    h3.fail_first = 1
    settings = make_settings(tmp_path, h3.url)
    settings_provider = generate.provider_settings(settings)
    assert settings_provider.job_retries >= 1

    job = Job(job_id="j4", user_id=7, mode="photo", text="Привет!",
              inputs={"photo": str(photo), "voice": str(voice)})
    # Пауза перед повтором в бою почти минута; здесь укорачиваем.
    original = generate.provider_settings
    generate.provider_settings = lambda s: original(s).model_copy(
        update={"retry_pause_s": 0.1, "cooldown_s": 0.0}
    )
    try:
        await generate.make_runner(settings)(job)
    finally:
        generate.provider_settings = original
    assert job.media_name == "j4.mp4"


async def test_uploads_are_removed_after_success(tmp_path, h3, media):
    """Чужое лицо и чужой голос не должны лежать дольше необходимого."""
    photo, voice = media
    settings = make_settings(tmp_path, h3.url)
    await generate.make_runner(settings)(Job(
        job_id="j5", user_id=7, mode="photo", text="Привет!",
        inputs={"photo": str(photo), "voice": str(voice)},
    ))
    assert not photo.exists()
    assert not voice.exists()
    assert not (settings.data_dir / "work" / "j5").exists()


async def test_broken_input_explains_itself(tmp_path, h3, media):
    _, voice = media
    settings = make_settings(tmp_path, h3.url)
    job = Job(job_id="j6", user_id=7, mode="photo", text="Привет!",
              inputs={"photo": str(voice), "voice": str(voice)})
    with pytest.raises(prepare.PrepareError) as caught:
        await generate.make_runner(settings)(job)
    assert "изображение" in str(caught.value).lower()


async def test_model_error_is_not_leaked_to_the_person(tmp_path, h3, media):
    """В тексте отказа модели бывает трассировка чужого воркера."""
    photo, voice = media
    h3.fail_first = 99
    settings = make_settings(tmp_path, h3.url)
    original = generate.provider_settings
    generate.provider_settings = lambda s: original(s).model_copy(
        update={"retry_pause_s": 0.05, "cooldown_s": 0.0, "job_retries": 0}
    )
    try:
        with pytest.raises(UserError) as caught:
            await generate.make_runner(settings)(Job(
                job_id="j7", user_id=7, mode="photo", text="Привет!",
                inputs={"photo": str(photo), "voice": str(voice)},
            ))
    finally:
        generate.provider_settings = original
    assert "CUDA" not in str(caught.value)
    assert "попробуйте" in str(caught.value).lower()
