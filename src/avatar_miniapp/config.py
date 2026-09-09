"""Настройки мини-приложения.

Секреты читаются тем же механизмом, что и в стенде: файл вне репозитория
(`~/.avatars/secrets.env`, на Windows `%USERPROFILE%\\.avatars\\secrets.env`),
поверх — переменные окружения процесса. На сервере переменные приходят из
systemd-юнита, локально — из файла; код при этом одинаковый.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from avatar_core.config import load_secrets

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"


def _flag(values: dict[str, str], key: str, default: bool) -> bool:
    raw = values.get(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "да"}


@dataclass
class Settings:
    bot_token: str = ""
    webapp_url: str = ""
    host: str = "127.0.0.1"
    port: int = 8081
    data_dir: Path = Path("./data")
    # Пускать неподписанные запросы. Это открытая дверь к нашим генерациям:
    # с ней любой с curl тратит наш ключ. Только локально, и видно в /api/health.
    dev_allow_unsigned: bool = False
    # Один токен = один процесс long polling. Если бот уже крутится на сервере,
    # локально чат-часть надо гасить, иначе апдейты поделятся пополам и бот
    # начнёт «через раз не отвечать».
    chat_enabled: bool = True
    # Сколько живёт подпись initData.
    init_data_max_age_s: int = 86400
    init_data_future_skew_s: int = 300
    secrets_path: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    def ensure_dirs(self) -> None:
        for path in (self.jobs_dir, self.media_dir):
            path.mkdir(parents=True, exist_ok=True)


def load(env_file: str | os.PathLike[str] | None = None) -> Settings:
    values = load_secrets(Path(env_file) if env_file else None)
    settings = Settings(
        bot_token=values.get("MAX_BOT_TOKEN", ""),
        webapp_url=values.get("MAX_WEBAPP_URL", ""),
        host=values.get("MINIAPP_HOST", "127.0.0.1"),
        port=int(values.get("MINIAPP_PORT") or 8081),
        # По умолчанию — рядом с проектом (в .gitignore). На сервере каталог
        # задаётся явно: юнит разрешает запись только в /var/lib/avatar-miniapp.
        data_dir=Path(values.get("MINIAPP_DATA_DIR") or (APP_DIR.parents[1] / "data")),
        dev_allow_unsigned=_flag(values, "MINIAPP_DEV_ALLOW_UNSIGNED", False),
        chat_enabled=_flag(values, "MINIAPP_CHAT_ENABLED", True),
        secrets_path=values.get("_secrets_path", ""),
    )
    if settings.dev_allow_unsigned:
        settings.warnings.append(
            "MINIAPP_DEV_ALLOW_UNSIGNED=1 — запросы без подписи проходят. "
            "На сервере этого быть не должно."
        )
    if settings.chat_enabled and not settings.bot_token:
        settings.warnings.append(
            f"MAX_BOT_TOKEN пуст ({settings.secrets_path}) — чат-часть не поднимется, "
            "работает только веб-часть."
        )
        settings.chat_enabled = False
    return settings
