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
    h3_base_url: str = ""
    h3_api_key: str = ""
    ffmpeg: str = "ffmpeg"
    # Заглушка вместо модели: весь путь окно ↔ чат отлаживается без единой
    # потраченной генерации. Боевой режим включается сам, как только есть
    # адрес и ключ H3.
    use_stub: bool = False
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
    def voices_dir(self) -> Path:
        """Готовые голоса. НЕ в репозитории: это записи живых людей,
        а avatar-miniapp публичный. Кладутся на сервер рядом с данными."""
        return self.data_dir / "voices"

    @property
    def token_hint(self) -> str:
        """Опознавательные приметы токена для лога — без самого токена.

        «Неверный токен» на одной машине при рабочем токене на другой — это
        почти всегда обрезка или лишний символ. Длина и края отвечают на это
        сразу, а сравнить их с локальными безопасно даже в переписке.
        """
        token = self.bot_token
        if not token:
            return "пусто"
        edges = f"{token[:4]}…{token[-4:]}" if len(token) > 12 else "короткий"
        return f"{len(token)} симв., {edges}"

    @property
    def jobs_dir(self) -> Path:
        return self.data_dir / "jobs"

    @property
    def media_dir(self) -> Path:
        return self.data_dir / "media"

    def ensure_dirs(self) -> None:
        for path in (self.jobs_dir, self.media_dir, self.voices_dir):
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
        h3_base_url=values.get("H3_BASE_URL", ""),
        h3_api_key=values.get("H3_API_KEY", ""),
        ffmpeg=values.get("MINIAPP_FFMPEG", "ffmpeg"),
        use_stub=_flag(values, "MINIAPP_USE_STUB", False),
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
    if not settings.use_stub and not (settings.h3_base_url and settings.h3_api_key):
        settings.warnings.append(
            "H3_BASE_URL или H3_API_KEY пусты — работаем на заглушке, "
            "настоящих роликов не будет."
        )
        settings.use_stub = True
    # Токен, который «работает на ноутбуке и не работает на сервере», почти
    # всегда попорчен при переносе. Ловим это до первого запроса к MAX.
    token = settings.bot_token
    if token and not token.isascii():
        settings.warnings.append(
            "В MAX_BOT_TOKEN есть не-ASCII символы — похоже, вместо токена "
            "подставился текст-заполнитель или значение скопировалось с лишним."
        )
    if token and any(ch.isspace() for ch in token):
        settings.warnings.append(
            "В MAX_BOT_TOKEN есть пробельные символы внутри значения — "
            "скорее всего, токен склеился с чем-то при копировании."
        )
    return settings
