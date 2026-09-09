"""Подпорка под maxapi: обновления, которые он не смог разобрать.

Что случилось. Голосовые из чата до обработчиков вообще не доезжали, а в
журнале была только строка самой библиотеки:

    Получен неизвестный тип обновления: message_created

Внутри maxapi это выглядит так: событие прогоняется через pydantic-модель,
и если хоть одно поле не подошло — функция возвращает None, событие
молча выбрасывается, а тип печатается в предупреждение. То есть теряется
не «неизвестный тип», а обычное сообщение с непривычным вложением.

Платформа молодая и меняется быстрее библиотеки, поэтому мы:

  1. печатаем СЫРОЕ событие целиком — без него остаётся только гадать,
     чем именно MAX прислал запись;
  2. отдаём его своему обработчику, чтобы голосовое всё-таки доехало,
     даже пока библиотека его не понимает.

Когда maxapi научится — подпорка просто перестанет срабатывать, ломать
её удаление ничего не будет.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable

log = logging.getLogger("miniapp.maxcompat")

_installed = False


def install(fallback: Callable[[dict], Awaitable[None]]) -> None:
    """Перехватить разбор обновлений. Зовётся один раз, до start_polling."""
    global _installed
    if _installed:
        return

    import maxapi.methods.types.getted_updates as module

    original = module.get_update_model

    async def patched(event: dict, bot):
        model = await original(event, bot)
        if model is not None:
            return model
        try:
            body = json.dumps(event, ensure_ascii=False)
        except (TypeError, ValueError):
            body = repr(event)
        log.warning("maxapi не разобрал обновление, берём сами: %s", body[:2000])
        try:
            await fallback(event)
        except Exception as exc:  # noqa: BLE001 — подпорка не должна ронять опрос
            log.error("своя обработка тоже не справилась: %s", exc)
            log.debug("полный след", exc_info=True)
        return None

    module.get_update_model = patched
    _installed = True
    log.info("подпорка под неразобранные обновления установлена")
