"""Очередь к модели, общая на все процессы этой машины.

ЗАЧЕМ. Инстанс H3 у нас один, и на двух одновременных генерациях он падает
по памяти. Внутри одного процесса это решено очередью на единицу, но ботов
теперь два — MAX и Telegram, — и каждый со своей очередью. Две очереди по
одному дают в сумме две параллельные генерации, то есть ровно тот случай,
которого мы избегали. Со стороны это выглядит как «модель не справилась»
сразу у двоих, и причину искать долго: у каждого бота в журнале всё чисто.

КАК. Один файл-замок на машину. Кто его держит — тот и работает с моделью,
остальные ждут. Замок берётся уже внутри рабочего потока, поэтому ожидание
никого не морозит: веб-часть отвечает, бот опрашивает, очередь живёт.

Держим замок ТОЛЬКО вокруг обращения к модели. Подготовка входов и рисовка
портрета в Kandinsky — это другие ресурсы, и занимать ими общий слот значит
без нужды держать соседа в очереди лишние полминуты.
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger("miniapp.slot")

try:
    import fcntl
except ImportError:                       # Windows
    fcntl = None                          # type: ignore[assignment]

# Дольше самой долгой генерации с запасом. Если ждём больше — сосед не занят,
# а мёртв, и вечно стоять в очереди за покойником незачем.
WAIT_S = 15 * 60
# Через сколько замок считается брошенным. Только для запасного пути: flock
# ядро снимает само при смерти процесса, а вот файл после kill -9 остаётся.
STALE_S = 20 * 60


@contextmanager
def only_one(path: str | Path | None, *, wait_s: float = WAIT_S, poll_s: float = 1.0):
    """Пропустить внутрь по одному. Пустой путь — не ограничивать вовсе.

    Пустой путь это не заглушка «на потом», а рабочий режим: в тестах и на
    машине разработчика процесс один, и городить межпроцессную синхронизацию
    там незачем.
    """
    if not path:
        yield
        return

    lock = Path(path)
    lock.parent.mkdir(parents=True, exist_ok=True)
    started = time.time()
    said = False

    while True:
        holder = _take(lock)
        if holder is not None:
            waited = time.time() - started
            if waited > 1:
                log.info("слот к модели освободился, ждали %.0f с", waited)
            try:
                yield
            finally:
                _release(lock, holder)
            return

        if time.time() - started > wait_s:
            # Не отказываем: лучше рискнуть параллельной генерацией, чем
            # гарантированно не сделать ничего. Но в журнале это должно
            # быть видно — такого не должно случаться вовсе.
            log.error("слот к модели не освободился за %.0f с — иду без него. "
                      "Проверьте, не завис ли соседний бот: %s", wait_s, lock)
            yield
            return

        if not said:
            log.info("модель занята соседним ботом, жду очереди")
            said = True
        time.sleep(poll_s)


def _take(lock: Path):
    """Занять замок. Возвращает то, что потом отдать в _release, или None."""
    if fcntl is not None:
        # Путь для Linux, то есть для сервера. flock снимается ядром при
        # любом конце процесса, включая kill -9, — брошенных замков не бывает.
        handle = os.open(lock, os.O_CREAT | os.O_RDWR, 0o666)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(handle)
            return None
        os.write(handle, f"{os.getpid()}\n".encode())
        return handle

    # Запасной путь для Windows: атомарное создание файла. Замок здесь может
    # пережить процесс, поэтому смотрим на возраст.
    try:
        handle = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            if time.time() - lock.stat().st_mtime > STALE_S:
                log.warning("замок %s брошен больше %.0f мин назад — снимаю",
                            lock, STALE_S / 60)
                lock.unlink(missing_ok=True)
        except OSError:
            pass
        return None
    os.write(handle, f"{os.getpid()}\n".encode())
    return handle


def _release(lock: Path, handle) -> None:
    try:
        if fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_UN)
        os.close(handle)
    except OSError as exc:
        log.debug("замок не отпустился штатно: %s", exc)
    if fcntl is None:
        lock.unlink(missing_ok=True)
