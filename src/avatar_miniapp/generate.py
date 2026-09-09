"""Боевая генерация: подготовка входов, вызов H3, готовый ролик.

Подставляется в JobManager вместо заглушки — всё вокруг (очередь, 409,
переживание перезапуска, доставка в чат) уже отлажено и не меняется.

H3 синхронный и блокирующий, поэтому крутим его в отдельном потоке: один
`await` на всю задачу, событийный цикл при этом продолжает отвечать окну
и боту. Конкурентность всё равно единица — у инстанса она такая же, и
он падает по памяти, если гнать задачи подряд.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from avatar_core.basket import AUDIO, IMAGE, VIDEO, Asset, Basket
from avatar_core.config import ProviderSettings
from avatar_core.errors import AvatarError
from avatar_core.models import JobSpec, JobStatus
from avatar_core.providers.h3 import H3Provider

from . import prepare, text
from .config import Settings
from .jobs import Job, UserError

log = logging.getLogger("miniapp.generate")

# Вертикаль под мессенджер. У Kandinsky она недостижима, у H3 — штатный пресет.
ASPECT = "9:16"


def provider_settings(settings: Settings) -> ProviderSettings:
    """Настройки H3 из тех же секретов, что и всё остальное.

    Отдельного settings.yaml у мини-аппа нет намеренно: на сервере всё
    приезжает переменными окружения из юнита, и лишний файл — это лишнее
    место, где конфигурация может разойтись с реальностью.
    """
    return ProviderSettings(
        base_url=settings.h3_base_url,
        api_key=settings.h3_api_key,
        concurrency=1,
        poll_interval_s=3.0,
        poll_timeout_s=600.0,
        heartbeat_s=30.0,
        # Инстанс не всегда отдаёт память между задачами. Повторяем сами:
        # «кончилась память GPU» через минуту уже неправда.
        job_retries=2,
        retry_pause_s=45.0,
        cooldown_s=10.0,
        max_request_mb=80.0,
    )


def make_runner(settings: Settings):
    """Замыкание с настройками — то, что кладётся в JobManager."""

    def build_inputs(job: Job, work: Path) -> tuple[Basket, str, int]:
        """Подготовка входов и промпт. Всё, что может не понравиться, — здесь."""
        work.mkdir(parents=True, exist_ok=True)
        ff = settings.ffmpeg
        images: list[Asset] = []
        audios: list[Asset] = []
        videos: list[Asset] = []

        if job.mode == "photo":
            src = job.inputs.get("photo")
            if not src:
                raise prepare.PrepareError("Не пришло фото.")
            images.append(Asset(prepare.prepare_photo(Path(src), work / "face.png"), IMAGE))

            voice_id = job.inputs.get("voice_id")
            voice_file = job.inputs.get("voice")
            if voice_file:
                audios.append(Asset(
                    prepare.prepare_voice(Path(voice_file), work / "voice.wav", ffmpeg=ff), AUDIO
                ))
            elif voice_id:
                preset = settings.voices_dir / f"{voice_id}.wav"
                if not preset.is_file():
                    raise prepare.PrepareError("Такого голоса нет. Выберите другой.")
                audios.append(Asset(
                    prepare.prepare_voice(preset, work / "voice.wav", ffmpeg=ff), AUDIO
                ))
            else:
                raise prepare.PrepareError("Не выбран голос.")
        else:
            src = job.inputs.get("video")
            if not src:
                raise prepare.PrepareError("Не пришло видео.")
            clip, voice = prepare.prepare_video(
                Path(src), work / "ref.mp4", work / "voice.wav", ffmpeg=ff
            )
            videos.append(Asset(clip, VIDEO))
            audios.append(Asset(voice, AUDIO))

        prompt, duration = text.build(job.text, job.mode)
        basket = Basket(text=job.text, images=images, audios=audios, videos=videos,
                        meta={"job_id": job.job_id, "mode": job.mode})
        return basket, prompt, duration

    def run_blocking(job: Job, work: Path, out: Path) -> None:
        basket, prompt, duration = build_inputs(job, work)
        spec = JobSpec(
            cell_id=job.job_id,
            provider="h3",
            person_id=str(job.user_id),
            text_id=job.mode,
            form="miniapp",
            wrapper="ru",
            basket=basket,
            params={"duration_seconds": duration, "aspect_ratio": ASPECT},
            prompt_logical=job.text,
            prompt_rendered=prompt,
        )
        provider = H3Provider(provider_settings(settings))
        try:
            # Корзину проверяем ДО отправки: лучше внятный отказ здесь,
            # чем таймаут на восьмидесяти мегабайтах base64.
            provider.validate(spec, strict=True)
            job.progress = 10
            result = provider.run(spec, work)
            if result.status is not JobStatus.DONE or not result.output_path:
                raise AvatarError(result.error or "Модель не вернула ролик")
            job.provider_job_id = result.provider_job_id
            Path(result.output_path).replace(out)
        finally:
            provider.close()

    async def run(job: Job) -> None:
        work = settings.data_dir / "work" / job.job_id
        out = settings.media_dir / f"{job.job_id}.mp4"
        log.info("%s: готовлю входы (%s)", job.job_id, job.mode)
        try:
            await asyncio.to_thread(run_blocking, job, work, out)
        except prepare.PrepareError:
            raise
        except AvatarError as exc:
            # Текст провайдера пользователю не показываем: там бывает
            # трассировка чужого воркера. В лог — целиком, наружу — коротко.
            log.error("%s: модель отказала: %s", job.job_id, exc)
            raise UserError("Модель сейчас не справилась. Попробуйте ещё раз через пару минут.") from exc
        job.media_name = out.name
        job.progress = 100
        log.info("%s: готово, %.1f МБ", job.job_id, out.stat().st_size / 1_048_576)
        _cleanup(work, job)

    return run


def _cleanup(work: Path, job: Job) -> None:
    """Промежуточные файлы не нужны, а место на диске конечно.

    Загруженное пользователем удаляем тоже: это чужое лицо и чужой голос,
    держать их дольше необходимого незачем.
    """
    import shutil

    shutil.rmtree(work, ignore_errors=True)
    for key in ("photo", "voice", "video"):
        raw = job.inputs.get(key)
        if raw:
            Path(raw).unlink(missing_ok=True)
