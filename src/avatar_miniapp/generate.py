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
from avatar_core.toonify import Toonify

from . import prepare, slot, text
from .config import Settings
from .jobs import Job, UserError

log = logging.getLogger("miniapp.generate")

# Вертикаль под мессенджер. У Kandinsky она недостижима, у H3 — штатный пресет.
ASPECT = "9:16"

# Рисованный режим идёт в два шага, и человеку это видно по прогрессу:
# сначала Kandinsky рисует портрет (15–25 с), потом H3 его оживляет (~30 с).
TOON_DRAWN = 40


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


def toonify_settings(settings: Settings) -> Toonify:
    """Клиент к Kandinsky I2I. Ключи те же, что и у всего остального."""
    return Toonify(
        settings.kandinsky_base_url,
        settings.kandinsky_api_key,
        poll_interval_s=3.0,
        # Замеры на корзинке из восьми лиц: 13–25 с на картинку, но первые
        # задачи после простоя ждали очереди по две минуты. Пять минут —
        # с запасом, и всё равно меньше, чем ждёт человек у H3.
        poll_timeout_s=300.0,
        logger=log,
    )


def draw_toon(settings: Settings, job: Job, face: Path, dest: Path) -> Path:
    """Фото → рисованный портрет. Первый шаг рисованного режима.

    Отказ Kandinsky отделяем от отказа H3 намеренно: это разные сервисы,
    и «не смог нарисовать» лечится не тем же, чем «не смог оживить».
    """
    if settings.use_stub:
        # Тот же путь, что и у остальной заглушки: без ключей отлаживаем
        # окно и чат, не тратя генерации. Портретом становится само фото.
        import shutil

        shutil.copyfile(face, dest)
        return dest
    client = toonify_settings(settings)
    try:
        client.run(face, dest, text.TOON_STYLE, label=job.job_id)
    except AvatarError as exc:
        log.error("%s: Kandinsky не нарисовал: %s", job.job_id, exc)
        raise UserError(
            "Не получилось нарисовать аватар по этому фото. "
            "Попробуйте другое — лучше всего портрет анфас при ровном свете."
        ) from exc
    finally:
        client.close()
    return dest


def make_runner(settings: Settings):
    """Замыкание с настройками — то, что кладётся в JobManager."""

    def build_inputs(job: Job, work: Path) -> tuple[Basket, str, int]:
        """Подготовка входов и промпт. Всё, что может не понравиться, — здесь."""
        work.mkdir(parents=True, exist_ok=True)
        ff = settings.ffmpeg
        images: list[Asset] = []
        audios: list[Asset] = []
        videos: list[Asset] = []

        if job.mode in ("photo", "toon"):
            src = job.inputs.get("photo")
            if not src:
                raise prepare.PrepareError("Не пришло фото.")
            face = prepare.prepare_photo(Path(src), work / "face.png")
            if job.mode == "toon":
                # Рисунок кладём в media_dir, а не в work: work удаляется
                # после генерации, а портрет надо показать в окне, пока
                # H3 ещё работает.
                drawn = settings.media_dir / f"{job.job_id}_toon.png"
                draw_toon(settings, job, face, drawn)
                job.poster_name = drawn.name
                job.progress = TOON_DRAWN
                log.info("%s: портрет готов, оживляю", job.job_id)
                face = drawn
            images.append(Asset(face, IMAGE))

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
            job.progress = max(job.progress, 10)
            # Замок берём здесь, а не вокруг всего run_blocking: подготовка
            # входов и рисовка портрета в Kandinsky к H3 отношения не имеют,
            # и держать под ними общий слот значит зря морозить соседа.
            # Мы уже в рабочем потоке, так что ожидание никого не морозит.
            with slot.only_one(settings.h3_lock_path):
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
        _cleanup(work, job, settings.data_dir / "uploads")

    return run


def _cleanup(work: Path, job: Job, uploads: Path) -> None:
    """Промежуточные файлы не нужны, а место на диске конечно.

    Загруженное через окно удаляем: это чужое лицо и чужой голос, держать
    их дольше необходимого незачем. А вот присланное боту в чат не трогаем —
    человек прислал голосовое один раз и вправе сделать по нему несколько
    роликов. Оно само истечёт через сутки.
    """
    import shutil

    shutil.rmtree(work, ignore_errors=True)
    for key in ("photo", "voice", "video"):
        raw = job.inputs.get(key)
        if not raw:
            continue
        path = Path(raw)
        if path.parent == uploads:
            path.unlink(missing_ok=True)
