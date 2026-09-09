"""Проверка окна в поддельном браузере.

Логика окна ломалась дважды и оба раза незаметно: код проходил все
питоновские тесты, а на экране кнопка не возвращалась и слот не исчезал.
Поэтому сам интерфейс тоже прогоняется — в jsdom, тем же test.bat.

Если node или jsdom не установлены, проверка пропускается: обязательной
частью сборки её делать рано, а полезной она уже стала.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "ui" / "dom.test.js"


def _jsdom_path() -> str:
    """Где лежит jsdom: рядом с проектом или там, куда его поставили."""
    if os.environ.get("JSDOM_PATH"):
        return os.environ["JSDOM_PATH"]
    for base in (HERE.parents[1], HERE.parents[0], Path("/tmp")):
        candidate = base / "node_modules" / "jsdom"
        if candidate.is_dir():
            return str(candidate)
    return ""


def test_window_behaves(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("нет node — проверка окна пропущена")
    jsdom = _jsdom_path()
    if not jsdom:
        pytest.skip("нет jsdom — поставьте: npm install jsdom")

    proc = subprocess.run(
        [node, str(SCRIPT)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "JSDOM_PATH": jsdom},
    )
    if proc.returncode != 0:
        pytest.fail((proc.stderr or proc.stdout or "").strip())
