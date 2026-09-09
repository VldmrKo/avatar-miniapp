"""Текст пользователя → промпт для модели.

Два преобразования, оба обязательные, оба выяснены на стенде:

1. Ударение. По умолчанию модель говорит «авАтары» — читает заимствование
   по-английски. Знак U+0301 закрывает вопрос полностью, но пользователь
   его не наберёт, значит ставим сами по словарю.
2. Длительность. Модель заполняет речью ВСЁ отведённое время: попросили
   пятнадцать секунд под трёхсекундную реплику — она досочинит двенадцать,
   зачитывая вслух наш же промпт. Поэтому просим ровно под реплику.

И правило, которое дороже обоих: никаких мета-инструкций в промпте.
Всё, что не внутри тега речи, модель склонна произнести. Фраза
«не произноси скобки» будет произнесена.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml
from avatar_core.speech import estimate_speech_seconds, fit_duration

STRESS_MARK = "́"
VOWELS = "аеёиоуыэюяАЕЁИОУЫЭЮЯ"
DICT_PATH = Path(__file__).resolve().parent / "stress.yaml"

MAX_SPEECH_SECONDS = 14.0

# Универсальное поведение: то, что заказчик назвал «активная жестикуляция
# и мимика». Отдельной кнопки пока нет, это всегда включено.
BEHAVIOUR = (
    "(S1) свободно жестикулирует руками в такт речи, переводит взгляд "
    "с камеры в сторону и обратно, мимика меняется вместе со смыслом сказанного."
)

PROMPT_PHOTO = """<Picture 1> задаёт внешность (S1).
<Audio 1> задаёт голос (S1).
[Shot 1] {behaviour}
(S1) говорит: <d>[Russian] {line}</d>"""

PROMPT_VIDEO = """<Video 1> задаёт внешность, одежду, обстановку и манеру держаться (S1).
<Audio 1> задаёт голос (S1).
[Shot 1] {behaviour}
(S1) говорит: <d>[Russian] {line}</d>"""


def load_stress_dict(path: Path | None = None) -> dict[str, int]:
    """Словарь «основа слова → номер ударной гласной».

    Лежит рядом с кодом отдельным файлом, чтобы пополнять его по жалобам
    без выкладки кода.
    """
    src = path or DICT_PATH
    if not src.is_file():
        return {}
    data = yaml.safe_load(src.read_text(encoding="utf-8")) or {}
    return {str(k).lower(): int(v) for k, v in data.items()}


def put_stress(word: str, vowel_number: int) -> str:
    """Знак ударения после N-й гласной. Номер с единицы."""
    seen = 0
    for i, ch in enumerate(word):
        if ch in VOWELS:
            seen += 1
            if seen == vowel_number:
                return word[: i + 1] + STRESS_MARK + word[i + 1 :]
    return word


def mark_stress(text: str, dictionary: dict[str, int] | None = None) -> str:
    """Расставить ударения по словарю.

    Ищем основу внутри слова, а не слово целиком: «аватар» покрывает
    и «аватара», и «аватарами» — окончание на место ударения не влияет.
    Регистр исходника сохраняем, знак уже стоящего ударения не дублируем.
    """
    words = dictionary if dictionary is not None else load_stress_dict()
    if not words:
        return text
    result = text
    for stem, number in words.items():
        pattern = re.compile(re.escape(stem), re.IGNORECASE)

        def replace(match: re.Match[str]) -> str:
            found = match.group(0)
            if STRESS_MARK in found:
                return found
            return put_stress(found, number)

        # Пропускаем слова, где ударение уже проставлено руками: пользователь
        # мог скопировать текст откуда-то, и второй знак сломает произношение.
        result = pattern.sub(replace, result)
    return result


def speech_seconds(text: str) -> float:
    return estimate_speech_seconds(text)


def too_long(text: str) -> float:
    """Сколько лишних секунд. Ноль или меньше — влезает."""
    return round(speech_seconds(text) - MAX_SPEECH_SECONDS, 1)


def build(text: str, mode: str, dictionary: dict[str, int] | None = None) -> tuple[str, int]:
    """Промпт и длительность ролика. Всё остальное собирается отсюда."""
    line = " ".join(mark_stress(text.strip(), dictionary).split())
    template = PROMPT_VIDEO if mode == "video" else PROMPT_PHOTO
    prompt = template.format(behaviour=BEHAVIOUR, line=line)
    return prompt, fit_duration(line)
