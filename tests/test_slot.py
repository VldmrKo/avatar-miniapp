"""Общая очередь к модели.

Инстанс H3 один и на двух параллельных генерациях падает по памяти.
Внутри процесса это решает очередь на единицу, но ботов теперь двое, и
у каждого своя. Здесь проверяется то, что их связывает.
"""

from __future__ import annotations

import multiprocessing
import time

from avatar_miniapp import slot


def test_without_a_path_it_does_not_get_in_the_way():
    """Пустой путь — рабочий режим, а не заглушка: на машине разработчика
    процесс один, и синхронизировать не с кем."""
    with slot.only_one(""):
        pass
    with slot.only_one(None):
        pass


def test_second_taker_waits_for_the_first(tmp_path):
    lock = tmp_path / "h3.lock"
    with slot.only_one(lock):
        # Внутри — второй заход не должен пройти. Ждать полный WAIT_S
        # в тесте нельзя, поэтому проверяем сам захват.
        assert slot._take(lock) is None
    # Отпустили — снова свободно.
    handle = slot._take(lock)
    assert handle is not None
    slot._release(lock, handle)


def _hold(path, seconds, started):
    with slot.only_one(path):
        started.set()
        time.sleep(seconds)


def test_two_processes_do_not_overlap(tmp_path):
    """Главное, ради чего всё затевалось: замок должен работать МЕЖДУ
    процессами, а не только между потоками одного."""
    lock = tmp_path / "h3.lock"
    started = multiprocessing.Event()
    other = multiprocessing.Process(target=_hold, args=(str(lock), 1.5, started))
    other.start()
    try:
        assert started.wait(10), "соседний процесс не занял замок"
        began = time.time()
        with slot.only_one(lock, poll_s=0.05):
            waited = time.time() - began
        assert waited > 0.5, f"вошли, не дождавшись соседа: {waited:.2f} с"
    finally:
        other.join(10)


def test_it_gives_up_rather_than_hangs_forever(tmp_path):
    """Вечно стоять в очереди за покойником незачем: лучше рискнуть
    параллельной генерацией, чем гарантированно не сделать ничего."""
    lock = tmp_path / "h3.lock"
    held = slot._take(lock)
    assert held is not None
    try:
        began = time.time()
        with slot.only_one(lock, wait_s=0.3, poll_s=0.05):
            pass
        assert time.time() - began < 5
    finally:
        slot._release(lock, held)
