"""Подготовка входов: то, что прислал человек → то, что примет модель.

Здесь же живут все отказы, которые видит пользователь. Правило простое:
если вход негоден, сказать об этом по-человечески и до вызова модели —
генерация стоит минуту чужого GPU, и тратить её на заведомо битый вход
незачем.

Ограничения H3: изображение png/jpeg, аудио 2–15 с, видео 2–15 с, и всё
это уезжает в теле запроса как base64, который раздувает объём на треть.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from avatar_core.media import canon_audio, detect_speech_window, probe

from .jobs import UserError

log = logging.getLogger("miniapp.prepare")

MIN_SECONDS = 2.0
MAX_SECONDS = 15.0
# Длинная сторона кадра. Больше модели не нужно, а нам это память и время
# на пережатие — на машине с двумя гигабайтами это заметно.
MAX_VIDEO_SIDE = 720
MAX_IMAGE_SIDE = 1024


class PrepareError(UserError):
    """Текст показывается пользователю как есть. Пишем по-человечески."""


@dataclass
class Prepared:
    image: Path | None = None
    audio: Path | None = None
    video: Path | None = None

    def sizes_mb(self) -> float:
        total = sum(p.stat().st_size for p in (self.image, self.audio, self.video) if p)
        # base64 раздувает на треть — считаем то, что реально уедет.
        return round(total * 4 / 3 / 1_048_576, 2)


def _ffmpeg(args: list[str], ffmpeg: str = "ffmpeg") -> None:
    proc = subprocess.run(
        [ffmpeg, "-nostdin", "-y", "-v", "error", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        log.error("ffmpeg %s: %s", args, (proc.stderr or "").strip()[:400])
        raise PrepareError("Не удалось обработать файл. Попробуйте другой.")


# --- фото --------------------------------------------------------------------

def prepare_photo(src: Path, dst: Path) -> Path:
    """Разворот по EXIF, RGB, разумный размер.

    exif_transpose обязательно ДО convert("RGB"): convert выбрасывает тег
    ориентации, и портретные фото с телефона уезжают в модель повёрнутыми.
    Причину потом ищут в промпте.
    """
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with Image.open(src) as img:
            img = ImageOps.exif_transpose(img)
            img = img.convert("RGB")
            if max(img.size) > MAX_IMAGE_SIDE:
                img.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE), Image.LANCZOS)
            dst.parent.mkdir(parents=True, exist_ok=True)
            img.save(dst, "PNG", optimize=True)
    except UnidentifiedImageError as exc:
        raise PrepareError("Это не похоже на изображение. Нужен PNG или JPEG.") from exc
    except OSError as exc:
        raise PrepareError("Не удалось прочитать фото. Попробуйте другое.") from exc
    return dst


# --- голос -------------------------------------------------------------------

def prepare_voice(src: Path, dst: Path, *, ffmpeg: str = "ffmpeg") -> Path:
    """Канонический WAV: моно, 24 кГц, −18 LUFS, 2–15 секунд.

    Если запись длиннее — берём окно с речью, а не первые пятнадцать секунд:
    в начале обычно возня, вдох и «так, я записываю».
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    source = src
    try:
        info = probe(src, ffprobe=ffmpeg.replace("ffmpeg", "ffprobe"))
    except Exception as exc:  # noqa: BLE001
        raise PrepareError("Не удалось прочитать запись голоса.") from exc

    # Сюда всё чаще приходит видео, а не аудио: человек снимает себя нативным
    # рекордером прямо в окне, и голос мы берём из дорожки. Немой ролик при
    # этом упадёт где-то в недрах ffmpeg — лучше сказать прямо здесь.
    if not info.audio_codec:
        raise PrepareError(
            "В этой записи нет звука. Снимите ещё раз и проверьте, "
            "что микрофон не выключен."
        )

    if info.duration_s > MAX_SECONDS:
        begin, finish = detect_speech_window(src, ffmpeg=ffmpeg)
        finish = min(finish, begin + MAX_SECONDS - 0.2)
        cut = dst.with_name(dst.stem + "_cut.wav")
        _ffmpeg(["-ss", f"{begin:.2f}", "-to", f"{finish:.2f}", "-i", str(src),
                 "-vn", "-ac", "1", "-ar", "24000", str(cut)], ffmpeg)
        source = cut

    result = canon_audio(source, dst, ffmpeg=ffmpeg)
    if result.duration_s < MIN_SECONDS:
        raise PrepareError(
            f"Запись слишком короткая — {result.duration_s:.1f} с после обрезки тишины. "
            f"Нужно хотя бы {MIN_SECONDS:.0f} секунды живой речи."
        )
    return dst


# --- видео -------------------------------------------------------------------

def prepare_video(src: Path, dst_video: Path, dst_audio: Path, *,
                  ffmpeg: str = "ffmpeg") -> tuple[Path, Path]:
    """Отрезок с речью до 15 секунд, 720p H.264, и голос оттуда же.

    Телефонное видео — это сорок секунд и сто мегабайт, а в теле запроса
    у нас лимит и окно 2–15 с. Режем и пережимаем здесь, а не надеемся,
    что модель разберётся.
    """
    ffprobe = ffmpeg.replace("ffmpeg", "ffprobe")
    try:
        info = probe(src, ffprobe=ffprobe)
    except Exception as exc:  # noqa: BLE001
        raise PrepareError("Не удалось прочитать видео. Попробуйте другой файл.") from exc

    if info.duration_s < MIN_SECONDS:
        raise PrepareError(
            f"Видео короче {MIN_SECONDS:.0f} секунд — модель такое не примет."
        )
    if not info.audio_codec:
        raise PrepareError(
            "В видео нет звука, а голос я беру именно оттуда. "
            "Снимите со звуком или сделайте аватара по фото."
        )

    begin, finish = 0.0, info.duration_s
    if info.duration_s > MAX_SECONDS:
        begin, finish = detect_speech_window(src, ffmpeg=ffmpeg)
        finish = min(finish, begin + MAX_SECONDS - 0.2)
        if finish - begin < MIN_SECONDS:
            begin, finish = 0.0, MAX_SECONDS - 0.2

    dst_video.parent.mkdir(parents=True, exist_ok=True)
    # scale с -2 держит чётность сторон: H.264 иначе откажется кодировать.
    _ffmpeg([
        "-ss", f"{begin:.2f}", "-to", f"{finish:.2f}", "-i", str(src),
        "-vf", f"scale='if(gt(iw,ih),min({MAX_VIDEO_SIDE},iw),-2)':"
               f"'if(gt(iw,ih),-2,min({MAX_VIDEO_SIDE},ih))'",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart",
        str(dst_video),
    ], ffmpeg)

    # Голос отдельным референсом: в exp2 мы подавали видео и аудио порознь,
    # и поведение по видео при этом отыгрывалось лучше всего.
    raw_audio = dst_audio.with_name(dst_audio.stem + "_raw.wav")
    _ffmpeg(["-i", str(dst_video), "-vn", "-ac", "1", "-ar", "24000", str(raw_audio)], ffmpeg)
    result = canon_audio(raw_audio, dst_audio, ffmpeg=ffmpeg)
    if result.duration_s < MIN_SECONDS:
        raise PrepareError(
            "В выбранном отрезке почти нет речи. Снимите видео, где вы говорите."
        )
    return dst_video, dst_audio
