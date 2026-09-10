#!/bin/sh
# Отчёт по работе сервиса. Под sudo:
#
#     sudo sh /opt/avatar-miniapp/deploy/report.sh [дней]
#
# По умолчанию за неделю. Два источника, и оба нужны: файлы задач знают,
# что человек просил и что получилось, а журнал знает, как он это оценил —
# оценки нигде больше не хранятся, они только строки в логе.
#
# Ничего не удаляет и не меняет. Читать можно хоть каждый день.

set -e

DAYS=${1:-7}
APP=/opt/avatar-miniapp
DATA=/var/lib/avatar-miniapp
PY="$APP/.venv/bin/python"

[ "$(id -u)" = "0" ] || { echo "Запускать под sudo: журнал иначе пуст." >&2; exit 1; }
[ -x "$PY" ] || PY=python3

echo "=== Аватары: отчёт за последние $DAYS дн."
echo

# Оценки живут только в журнале — вытаскиваем их до питона и отдаём файлом.
MARKS=$(mktemp)
trap 'rm -f "$MARKS"' EXIT
journalctl -u avatar-miniapp --since "$DAYS days ago" --no-pager -o cat 2>/dev/null \
	| grep -E "по задаче|длинная реплика" > "$MARKS" || true

DAYS="$DAYS" DATA="$DATA" MARKS="$MARKS" "$PY" - <<'PY'
import json
import os
import re
import statistics
import time
from collections import Counter
from pathlib import Path

days = float(os.environ["DAYS"])
data = Path(os.environ["DATA"])
since = time.time() - days * 86400

try:
    from avatar_core.speech import estimate_speech_seconds
except ImportError:                       # без core считаем грубее, но считаем
    def estimate_speech_seconds(text, lang="ru"):
        return len(text.split()) / 2.5

jobs = []
for path in sorted((data / "jobs").glob("*.json")):
    try:
        job = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        continue
    if job.get("created_at", 0) >= since:
        jobs.append(job)

if not jobs:
    print("Задач за этот период нет.")
    raise SystemExit

MODES = {"toon": "рисованный", "photo": "по фото", "video": "по видео"}


def block(title):
    print(f"--- {title}")


def share(part, whole):
    return f"{part} из {whole} ({round(100 * part / whole)}%)" if whole else "нет"


# --- сколько и чего ---------------------------------------------------------
done = [j for j in jobs if j.get("status") == "done"]
failed = [j for j in jobs if j.get("status") == "failed"]
stuck = [j for j in jobs if j.get("status") not in ("done", "failed")]

block("Задачи")
print(f"  всего      {len(jobs)}")
print(f"  готово     {share(len(done), len(jobs))}")
print(f"  отказ      {share(len(failed), len(jobs))}")
if stuck:
    # Незавершённые в отчёте за прошлое — это либо прямо сейчас идущие,
    # либо застрявшие после падения. Второе стоит увидеть.
    print(f"  не дошли   {len(stuck)}  (идут прямо сейчас или застряли)")
print()

block("По режимам")
by_mode = Counter(j.get("mode", "?") for j in jobs)
for mode, count in by_mode.most_common():
    ok = sum(1 for j in jobs if j.get("mode") == mode and j.get("status") == "done")
    print(f"  {MODES.get(mode, mode):<12} {count:>4}   готово {share(ok, count)}")
print()

# --- люди -------------------------------------------------------------------
per_user = Counter(j.get("user_id") for j in jobs)
repeat = sum(1 for count in per_user.values() if count > 1)
block("Люди")
print(f"  всего      {len(per_user)}")
print(f"  вернулись  {share(repeat, len(per_user))}   — сделали больше одного ролика")
print(f"  в среднем  {len(jobs) / len(per_user):.1f} ролика на человека")
print()

# --- время ------------------------------------------------------------------
waits = [j["finished_at"] - j["created_at"] for j in done
         if j.get("finished_at") and j.get("created_at")]
if waits:
    block("Сколько ждали, от нажатия до готового")
    print(f"  медиана       {statistics.median(waits):.0f} с")
    print(f"  худший        {max(waits):.0f} с")
    over_two = sum(1 for w in waits if w > 120)
    # Мы обещаем «пару минут». Если обещание регулярно не сбывается,
    # менять надо либо обещание, либо очередь.
    print(f"  дольше 2 мин  {share(over_two, len(waits))}   — а обещаем «пару минут»")
    print()

# --- длина реплики ----------------------------------------------------------
seconds = [estimate_speech_seconds(j.get("text", "")) for j in jobs if j.get("text")]
if seconds:
    block("Длина реплики")
    short = share(sum(1 for s in seconds if s < 5), len(seconds))
    long = share(sum(1 for s in seconds if s > 14), len(seconds))
    print(f"  медиана          {statistics.median(seconds):.1f} с")
    print(f"  короче 5 с       {short}   — тут модель договаривает лишнее")
    print(f"  длиннее 14 с     {long}   — тут конец смазывается")
    print(f"  самая длинная    {max(seconds):.0f} с")
    print()

# --- оценки -----------------------------------------------------------------
marks = Counter()
long_lines = 0
for line in Path(os.environ["MARKS"]).read_text(encoding="utf-8", errors="replace").splitlines():
    if "длинная реплика" in line:
        long_lines += 1
        continue
    for mark in ("👍", "😐", "👎"):
        if mark in line:
            marks[mark] += 1
            break

voted = sum(marks.values())
block("Оценки")
if voted:
    for mark, title in (("👍", "отлично"), ("😐", "нормально"), ("👎", "так себе")):
        print(f"  {mark} {title:<10} {share(marks[mark], voted)}")
    print(f"  оценили    {share(voted, len(done))} готовых роликов")
else:
    print("  никто не нажал ни одной кнопки")
    print("  (это не то же самое, что «не понравилось» — просто нет данных)")
print()

# --- отказы -----------------------------------------------------------------
if failed:
    block("Почему отказывали")
    reasons = Counter()
    for job in failed:
        why = (job.get("user_message") or job.get("error") or "без объяснения").strip()
        reasons[re.sub(r"\s+", " ", why)[:70]] += 1
    for why, count in reasons.most_common(5):
        print(f"  {count:>3}  {why}")
    print()

if long_lines:
    print(f"Реплик длиннее предела принято: {long_lines}."
          " Ограничение снято намеренно — смотрим, что из этого выходит.")
PY

echo
echo "Место на диске:"
du -sh "$DATA"/* 2>/dev/null | sed 's/^/  /'
