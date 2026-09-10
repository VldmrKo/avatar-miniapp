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
    """Загруженное через окно не должно лежать дольше необходимого."""
    import shutil

    photo, voice = media
    settings = make_settings(tmp_path, h3.url)
    uploads = settings.data_dir / "uploads"
    uploads.mkdir(parents=True, exist_ok=True)
    up_photo = uploads / photo.name
    up_voice = uploads / voice.name
    shutil.copy(photo, up_photo)
    shutil.copy(voice, up_voice)

    await generate.make_runner(settings)(Job(
        job_id="j5", user_id=7, mode="photo", text="Привет!",
        inputs={"photo": str(up_photo), "voice": str(up_voice)},
    ))
    assert not up_photo.exists()
    assert not up_voice.exists()
    assert not (settings.data_dir / "work" / "j5").exists()


async def test_preset_voice_survives_generation(tmp_path, h3, media):
    """Готовый голос — общий на всех и живёт в каталоге данных. Уборка
    после генерации трогает только загруженное человеком (папка uploads);
    удалить пресет означало бы сломать его для всех остальных."""
    import shutil

    photo, voice = media
    settings = make_settings(tmp_path, h3.url)
    settings.voices_dir.mkdir(parents=True, exist_ok=True)
    kept_voice = settings.voices_dir / "anya.wav"
    shutil.copy(voice, kept_voice)

    await generate.make_runner(settings)(Job(
        job_id="j8", user_id=7, mode="photo", text="Привет!",
        inputs={"photo": str(photo), "voice": str(kept_voice)},
    ))
    assert kept_voice.exists()


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


# --- мультяшный аватар -------------------------------------------------------
# Режим идёт в два шага: Kandinsky рисует портрет, H3 его оживляет. Проверяем
# именно стык — что в модель уехал РИСУНОК, а не исходное фото, и что отказ
# рисовалки отделён от отказа оживлялки.

class FakeKandinsky:
    """Три ручки Kandinsky I2I и запомненный промпт рисовки."""

    def __init__(self, png: bytes) -> None:
        self.png = png
        self.payload: dict = {}
        self.fail = False
        self.app = web.Application(client_max_size=200 * 1024 * 1024)
        self.app.router.add_post("/tasks/k6-i2i", self.submit)
        self.app.router.add_get("/tasks/{tid}", self.poll)
        self.app.router.add_get("/tasks/{tid}/result", self.result)

    async def submit(self, request: web.Request) -> web.Response:
        self.payload = await request.json()
        return web.json_response({"task_id": "toon-1"})

    async def poll(self, request: web.Request) -> web.Response:
        if self.fail:
            return web.json_response({"status": "failed"})
        return web.json_response({"status": "done"})

    async def result(self, request: web.Request) -> web.Response:
        return web.Response(body=self.png, content_type="image/png")


@pytest.fixture
async def kandinsky(tmp_path):
    from PIL import Image

    drawn = tmp_path / "drawn.png"
    # Заметно другая картинка, чем исходное фото: так видно, что в H3 уехал
    # именно результат рисовки, а не то, что прислал человек.
    Image.new("RGB", (512, 640), (240, 200, 60)).save(drawn)
    fake = FakeKandinsky(drawn.read_bytes())
    runner = web.AppRunner(fake.app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    fake.url = f"http://127.0.0.1:{runner.addresses[0][1]}"
    yield fake
    await runner.cleanup()


def toon_settings(tmp_path, h3_url: str, kandinsky_url: str) -> Settings:
    s = Settings(data_dir=tmp_path / "data", h3_base_url=h3_url, h3_api_key="k",
                 kandinsky_base_url=kandinsky_url, kandinsky_api_key="k")
    s.ensure_dirs()
    return s


async def test_toon_draws_first_then_animates(tmp_path, h3, kandinsky, media):
    from PIL import Image

    photo, voice = media
    settings = toon_settings(tmp_path, h3.url, kandinsky.url)
    job = Job(job_id="t1", user_id=7, mode="toon", text="Вот это круто!",
              inputs={"photo": str(photo), "voice": str(voice)})

    await generate.make_runner(settings)(job)

    # Рисовка получила наш зашитый промпт, и без переписывания на стороне
    # сервиса: промпт в продукте один на всех, значит и вести себя должен
    # одинаково от запуска к запуску.
    assert kandinsky.payload["params"]["beautificator"] == "disabled"
    assert "рисованный аватар" in kandinsky.payload["params"]["query"]

    # Портрет лежит рядом с роликом и переживает уборку work: окно показывает
    # его, пока H3 ещё считает.
    poster = settings.media_dir / job.poster_name
    assert job.poster_name == "t1_toon.png"
    assert poster.is_file()
    assert Image.open(poster).size == (512, 640)

    # И главное: в H3 уехал рисунок, а не исходное фото.
    import base64
    import io

    sent = base64.b64decode(h3.payload["reference_images"][0].split(",", 1)[-1])
    assert Image.open(io.BytesIO(sent)).getpixel((10, 10))[0] > 200

    # Описание стиля обязано быть внутри [Shot 1]: снаружи модель его
    # игнорирует и тянет рисунок обратно в фотографию (exp7).
    prompt = h3.payload["prompt"]
    shot = prompt.split("[Shot 1]", 1)[1].split("(S1) говорит", 1)[0]
    assert "векторная иллюстрация" in shot


async def test_toon_failure_blames_the_drawing_step(tmp_path, h3, kandinsky, media):
    """Не смог нарисовать и не смог оживить — разные беды и разные советы."""
    photo, voice = media
    kandinsky.fail = True
    settings = toon_settings(tmp_path, h3.url, kandinsky.url)
    with pytest.raises(UserError) as caught:
        await generate.make_runner(settings)(Job(
            job_id="t2", user_id=7, mode="toon", text="Привет!",
            inputs={"photo": str(photo), "voice": str(voice)},
        ))
    assert "нарисовать" in str(caught.value).lower()


async def test_toon_prompt_differs_from_plain_photo():
    """Обычный аватар не должен нечаянно получить стилевую строку."""
    from avatar_miniapp import text

    plain, _ = text.build("Привет!", "photo")
    toon, _ = text.build("Привет!", "toon")
    assert "векторная иллюстрация" in toon
    assert "векторная иллюстрация" not in plain
