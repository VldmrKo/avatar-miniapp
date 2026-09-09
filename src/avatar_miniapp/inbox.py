"""То, что человек прислал боту в чат: голосовое и видео.

Зачем это нужно. Вебвью MAX не отдаёт странице микрофон — `getUserMedia`
падает с `NotAllowedError` ещё до вопроса о разрешении, потому что окно
живёт внутри чужого контейнера, и прав ему никто не передавал. Камеру
файловый выбор открывает только в режиме фотографии.

Зато сам мессенджер и записывает, и снимает — своими средствами. Значит
запись делается в чате, а окно только показывает, что уже принято.
Получается не костыль, а нормальный путь: человек пользуется той кнопкой
записи, к которой привык, а не нашей.

Храним по одному последнему файлу каждого вида на человека. Это чужой
голос и чужое лицо — лежат ровно до генерации и не дольше суток.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from avatar_core.media import probe

log = logging.getLogger("miniapp.inbox")

VOICE = "voice"
VIDEO = "video"
KINDS = (VOICE, VIDEO)
MAX_AGE_S = 24 * 3600
MAX_BYTES = 100 * 1024 * 1024


@dataclass
class Item:
    kind: str
    path: Path
    received_at: float
    seconds: float

    @property
    def age_s(self) -> float:
        return time.time() - self.received_at

    def public(self) -> dict:
        return {
            "seconds": round(self.seconds, 1),
            "age_s": round(self.age_s),
            "name": self.path.name,
        }


class Inbox:
    def __init__(self, root: Path, ffprobe: str = "ffprobe") -> None:
        self.root = root
        self.ffprobe = ffprobe

    def _dir(self, user_id: int) -> Path:
        return self.root / str(user_id)

    def _meta_path(self, user_id: int) -> Path:
        return self._dir(user_id) / "meta.json"

    def _meta(self, user_id: int) -> dict:
        path = self._meta_path(user_id)
        if not path.is_file():
            return {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def sniff(self, data: bytes, suffix: str) -> str:
        """Что это на самом деле — по содержимому, а не по имени.

        MAX отдаёт голосовое и видео одинаково: тип `file` и ссылка вида
        `getfile?rq=...`, без всякого расширения. Гадать тут нечего —
        смотрим, есть ли внутри видеодорожка.
        """
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=suffix or ".bin", delete=False) as fh:
            fh.write(data)
            path = Path(fh.name)
        try:
            info = probe(path, ffprobe=self.ffprobe)
            return VIDEO if info.video_codec else VOICE
        except Exception as exc:  # noqa: BLE001
            log.warning("не опознали вложение: %s", exc)
            return ""
        finally:
            path.unlink(missing_ok=True)

    def put(self, user_id: int, kind: str, data: bytes, suffix: str) -> Item:
        if kind not in KINDS:
            raise ValueError(f"неизвестный вид вложения: {kind}")
        if len(data) > MAX_BYTES:
            raise ValueError("файл слишком большой")
        folder = self._dir(user_id)
        folder.mkdir(parents=True, exist_ok=True)
        # Один последний файл на вид: предыдущий больше не нужен, а место
        # и чужие записи копить незачем.
        for old in folder.glob(f"{kind}.*"):
            old.unlink(missing_ok=True)
        path = folder / f"{kind}{suffix or '.bin'}"
        path.write_bytes(data)

        try:
            seconds = probe(path, ffprobe=self.ffprobe).duration_s
        except Exception as exc:  # noqa: BLE001 — длительность приятна, но не обязательна
            log.warning("не удалось измерить %s: %s", path.name, exc)
            seconds = 0.0

        meta = self._meta(user_id)
        meta[kind] = {"name": path.name, "at": time.time(), "seconds": seconds}
        self._meta_path(user_id).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        log.info("принято от %s: %s %.1f с, %.1f МБ",
                 user_id, kind, seconds, len(data) / 1_048_576)
        return Item(kind, path, meta[kind]["at"], seconds)

    def get(self, user_id: int, kind: str) -> Item | None:
        entry = self._meta(user_id).get(kind)
        if not entry:
            return None
        path = self._dir(user_id) / entry["name"]
        if not path.is_file():
            return None
        item = Item(kind, path, float(entry["at"]), float(entry.get("seconds") or 0))
        if item.age_s > MAX_AGE_S:
            # Вчерашняя запись почти наверняка не та, которую человек имеет
            # в виду сегодня. Молча ею подменять вход нельзя.
            self.clear(user_id, kind)
            return None
        return item

    # --- «жду запись» -----------------------------------------------------
    # Окно говорит боту, чего ждать. Это нужно ровно в одном месте: когда
    # человек прислал файл без расширения и по нему не понять, голос это
    # или видео. Плюс делает ответ бота осмысленным, а не общим.

    def arm(self, user_id: int, kind: str) -> None:
        if kind not in KINDS:
            raise ValueError(f"неизвестный вид: {kind}")
        meta = self._meta(user_id)
        meta["expect"] = {"kind": kind, "at": time.time()}
        self._dir(user_id).mkdir(parents=True, exist_ok=True)
        self._meta_path(user_id).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def expected(self, user_id: int) -> str:
        entry = self._meta(user_id).get("expect") or {}
        # Полчаса — столько живёт намерение. Дальше это уже другая история.
        if entry and time.time() - float(entry.get("at", 0)) < 1800:
            return str(entry.get("kind") or "")
        return ""

    def disarm(self, user_id: int) -> None:
        meta = self._meta(user_id)
        if meta.pop("expect", None) is not None:
            self._meta_path(user_id).write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    def clear(self, user_id: int, kind: str) -> None:
        meta = self._meta(user_id)
        entry = meta.pop(kind, None)
        if entry:
            (self._dir(user_id) / entry["name"]).unlink(missing_ok=True)
            self._meta_path(user_id).write_text(
                json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
            )

    def public(self, user_id: int) -> dict:
        out: dict = {"expect": self.expected(user_id)}
        for kind in KINDS:
            item = self.get(user_id, kind)
            out[kind] = item.public() if item else None
        return out

    def sweep(self) -> int:
        """Убрать всё, что старше суток. Зовётся на старте."""
        removed = 0
        if not self.root.is_dir():
            return 0
        for folder in self.root.iterdir():
            if not folder.is_dir() or not folder.name.isdigit():
                continue
            for kind in KINDS:
                if self.get(int(folder.name), kind) is None:
                    removed += 1
        return removed
