"""Разбор вложений, пришедших боту в чат: что это и как достать звук.

Раньше здесь был склад: окно не умеет ни записать голос, ни снять видео
(вебвью MAX не отдаёт странице микрофон — `getUserMedia` падает с
`NotAllowedError` ещё до вопроса о разрешении), поэтому запись делалась
в чате, складывалась сюда, а окно её оттуда забирало.

Путь получился такой: закрыть окно → найти чат → снять ролик → отправить
→ дождаться кнопки → вернуться. Шесть действий и три переключения
контекста ради одного поля. Теперь весь сценарий целиком живёт в
переписке (`dialog.py`), а окно про чат не знает вовсе: там либо готовый
голос, либо файл. Склад стал не нужен и удалён — осталось только то,
без чего не разобрать входящий файл.

А разбирать приходится: MAX отдаёт и голосовое, и видео одинаково —
типом `file` и ссылкой `getfile?rq=...`, без расширения и без подсказок.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

from avatar_core.media import probe

log = logging.getLogger("miniapp.inbox")

VOICE = "voice"
VIDEO = "video"
KINDS = (VOICE, VIDEO)


@contextmanager
def _spill(data: bytes, suffix: str):
    """Байты во временный файл: ffprobe и ffmpeg работают с путями."""
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fh:
        fh.write(data)
        path = Path(fh.name)
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


class Attachments:
    """Что это за файл, сколько в нём секунд и как вынуть из него звук."""

    def __init__(self, ffprobe: str = "ffprobe", ffmpeg: str = "ffmpeg") -> None:
        self.ffprobe = ffprobe
        self.ffmpeg = ffmpeg

    def sniff(self, data: bytes, suffix: str) -> str:
        """Что это на самом деле — по содержимому, а не по имени.

        MAX отдаёт голосовое и видео одинаково: тип `file` и ссылка вида
        `getfile?rq=...`, без всякого расширения. Гадать тут нечего —
        смотрим, есть ли внутри видеодорожка.
        """
        with _spill(data, suffix or ".bin") as path:
            try:
                info = probe(path, ffprobe=self.ffprobe)
                return VIDEO if info.video_codec else VOICE
            except Exception as exc:  # noqa: BLE001
                log.warning("не опознали вложение: %s", exc)
                return ""

    def seconds(self, data: bytes, suffix: str) -> float:
        """Длительность. 0 — не смогли измерить.

        Нужна ровно для одной проверки: запись короче двух секунд модель
        не примет, и узнать об этом лучше сразу, а не после генерации.
        """
        with _spill(data, suffix or ".bin") as path:
            try:
                return probe(path, ffprobe=self.ffprobe).duration_s
            except Exception as exc:  # noqa: BLE001
                log.warning("не измерили вложение: %s", exc)
                return 0.0

    def audio_from(self, data: bytes, suffix: str) -> bytes:
        """Звуковая дорожка из видео.

        Нужна потому, что голосовые до бота не доезжают: MAX присылает по ним
        пустое событие без тела. А видео доезжает целиком. Значит «записать
        голос» — это записать короткий ролик, а звук мы возьмём сами.
        """
        with _spill(data, suffix or ".mp4") as src:
            dst = src.with_suffix(".wav")
            try:
                proc = subprocess.run(
                    [self.ffmpeg, "-nostdin", "-y", "-v", "error", "-i", str(src),
                     "-vn", "-ac", "1", "-ar", "24000", str(dst)],
                    capture_output=True, text=True, stdin=subprocess.DEVNULL,
                )
                if proc.returncode != 0 or not dst.is_file():
                    log.error("не вынули звук: %s", (proc.stderr or "").strip()[:300])
                    return b""
                return dst.read_bytes()
            finally:
                dst.unlink(missing_ok=True)
