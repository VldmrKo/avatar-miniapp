#!/bin/sh
# Отчёт-галерея: каждая генерация карточкой, с роликом и портретом.
#
#     sudo sh /opt/avatar-miniapp/deploy/report-html.sh [дней] [юнит]
#     sudo sh /opt/avatar-miniapp/deploy/report-html.sh 30 avatar-tgbot
#
# Отличие от report.sh: тот считает цифры в консоль, этот собирает папку
# с report.html и медиа и кладёт рядом zip — чтобы забрать к себе и
# показать, не заходя на сервер.
#
# ЧЕГО В ОТЧЁТЕ НЕТ И НЕ БУДЕТ: исходных фото, голоса и видео. Они
# удаляются сразу после генерации — это чужие лица и голоса, держать их
# дольше нужного не стоит. Поэтому отчёт показывает «что
# попросили → что получилось», а не «что загрузили → что получилось».
# Для рисованного режима середина всё же видна: портрет от Kandinsky
# остаётся, он нужен окну во время ожидания.
#
# Ничего не удаляет и не меняет.

set -e

DAYS=${1:-30}
UNIT=${2:-avatar-miniapp}
APP=/opt/avatar-miniapp
DATA=/var/lib/$UNIT
PY="$APP/.venv/bin/python"
OUT=/tmp/avatar-report-$UNIT-$(date +%Y%m%d-%H%M)

[ "$(id -u)" = "0" ] || { echo "Запускать под sudo: журнал иначе пуст." >&2; exit 1; }
[ -x "$PY" ] || PY=python3
[ -d "$DATA" ] || { echo "Нет каталога $DATA — такого бота на машине нет." >&2; exit 1; }

MARKS=$(mktemp)
trap 'rm -f "$MARKS"' EXIT
journalctl -u "$UNIT" --since "$DAYS days ago" --no-pager -o cat 2>/dev/null \
	| grep -E "по задаче" > "$MARKS" || true

mkdir -p "$OUT/media"

DAYS="$DAYS" DATA="$DATA" MARKS="$MARKS" UNIT="$UNIT" OUT="$OUT" "$PY" - <<'PY'
import html
import json
import os
import shutil
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

days = float(os.environ["DAYS"])
data = Path(os.environ["DATA"])
out = Path(os.environ["OUT"])
unit = os.environ["UNIT"]
since = time.time() - days * 86400

MODES = {"toon": "рисованный", "photo": "по фото", "video": "по видео"}
MARK_TITLE = {"👍": "отлично", "😐": "нормально", "👎": "так себе"}

# Оценки живут только в журнале, и связать их с задачей можно лишь по id.
marks = {}
for line in Path(os.environ["MARKS"]).read_text(encoding="utf-8",
                                                errors="replace").splitlines():
    for mark in MARK_TITLE:
        if mark in line:
            marks[line.rsplit(" ", 1)[-1].strip()] = mark
            break

jobs = []
for path in sorted((data / "jobs").glob("*.json")):
    try:
        job = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        continue
    if job.get("created_at", 0) >= since:
        jobs.append(job)
jobs.sort(key=lambda j: j.get("created_at", 0), reverse=True)

if not jobs:
    print("Задач за этот период нет — отчёт собирать не из чего.")
    raise SystemExit(1)

# Копируем только то, что реально попало в отчёт: media/ может хранить
# ролики и за пределами периода, тащить их в архив незачем.
copied = 0
megabytes = 0.0
for job in jobs:
    for name in (job.get("media_name"), job.get("poster_name")):
        if not name:
            continue
        src = data / "media" / name
        if src.is_file():
            shutil.copy2(src, out / "media" / name)
            copied += 1
            megabytes += src.stat().st_size / 1_048_576


def when(stamp):
    return datetime.fromtimestamp(stamp).strftime("%d.%m %H:%M") if stamp else "—"


def card(job):
    jid = job.get("job_id", "?")
    mode = job.get("mode", "?")
    status = job.get("status", "?")
    made = job.get("finished_at", 0) - job.get("created_at", 0)
    mark = marks.get(jid, "")
    video = job.get("media_name")
    poster = job.get("poster_name")

    shots = []
    if poster:
        shots.append(f'<figure><img src="media/{html.escape(poster)}" loading="lazy">'
                     f'<figcaption>портрет</figcaption></figure>')
    if video:
        shots.append(f'<figure><video src="media/{html.escape(video)}" controls '
                     f'preload="metadata"></video><figcaption>ролик</figcaption></figure>')
    if not shots:
        shots.append('<div class="nothing">файла нет</div>')

    why = job.get("user_message") or job.get("error") or ""
    return f"""
<article class="card {html.escape(status)}">
  <header>
    <span class="mode">{html.escape(MODES.get(mode, mode))}</span>
    <span class="when">{when(job.get("created_at"))}</span>
    {'<span class="mark" title="' + MARK_TITLE.get(mark, "") + '">' + mark + '</span>' if mark else ''}
  </header>
  <div class="shots">{''.join(shots)}</div>
  <p class="said">{html.escape(job.get("text", "") or "—")}</p>
  <footer>
    <span>{html.escape(status)}</span>
    <span>{made:.0f} с</span>
    <span class="id">{html.escape(jid)}</span>
  </footer>
  {'<p class="why">' + html.escape(why) + '</p>' if why else ''}
</article>"""


done = [j for j in jobs if j.get("status") == "done"]
by_mode = Counter(j.get("mode", "?") for j in jobs)
voted = Counter(marks[j["job_id"]] for j in jobs if j.get("job_id") in marks)

summary = " · ".join(filter(None, [
    f"{len(jobs)} задач",
    f"готово {len(done)}",
    f"{len(jobs) - len(done)} не дошли" if len(jobs) > len(done) else "",
    ", ".join(f"{MODES.get(m, m)} {n}" for m, n in by_mode.most_common()),
    (" ".join(f"{k} {v}" for k, v in voted.most_common())
     + f" (оценили {sum(voted.values())} из {len(done)})") if voted else "оценок нет",
]))

page = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Аватары — {html.escape(unit)}</title>
<style>
 :root {{ color-scheme: light dark; }}
 body {{ margin: 0; padding: 24px; font: 15px/1.5 system-ui, sans-serif;
        background: #f6f6f7; color: #16181d; }}
 h1 {{ margin: 0 0 4px; font-size: 20px; }}
 .lead {{ margin: 0 0 4px; color: #555; }}
 .note {{ margin: 0 0 24px; color: #777; font-size: 13px; max-width: 70ch; }}
 .grid {{ display: grid; gap: 16px;
          grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); }}
 .card {{ background: #fff; border: 1px solid #e3e3e6; border-radius: 12px;
          padding: 12px; display: flex; flex-direction: column; gap: 8px; }}
 .card.failed {{ border-color: #e5b4b4; background: #fff8f8; }}
 header {{ display: flex; align-items: center; gap: 8px; font-size: 13px; }}
 .mode {{ background: #eef1f6; border-radius: 6px; padding: 2px 8px; }}
 .when {{ color: #888; }}
 .mark {{ margin-left: auto; font-size: 18px; }}
 /* Полширины даже когда картинка одна: иначе карточка с одним роликом
    вдвое выше соседней с портретом, и сетка идёт лесенкой. */
 .shots {{ display: flex; gap: 8px; justify-content: center; }}
 figure {{ margin: 0; flex: 0 1 calc(50% - 4px); min-width: 0; }}
 figcaption {{ font-size: 11px; color: #999; text-align: center; padding-top: 2px; }}
 img, video {{ width: 100%; border-radius: 8px; background: #000;
               aspect-ratio: 3/4; object-fit: cover; }}
 .nothing {{ color: #aaa; font-size: 13px; padding: 24px 0; text-align: center; }}
 .said {{ margin: 0; font-size: 14px; }}
 footer {{ display: flex; gap: 10px; font-size: 12px; color: #888;
           border-top: 1px solid #eee; padding-top: 8px; margin-top: auto; }}
 .id {{ margin-left: auto; font-family: ui-monospace, monospace; }}
 .why {{ margin: 0; font-size: 13px; color: #a33; }}
 @media (prefers-color-scheme: dark) {{
   body {{ background: #16181d; color: #e6e6e8; }}
   .card {{ background: #1e2027; border-color: #2c2f38; }}
   .card.failed {{ background: #251c1c; border-color: #4a2b2b; }}
   .mode {{ background: #2a2e38; }}
   footer {{ border-color: #2c2f38; }}
 }}
</style></head><body>
<h1>Аватары — {html.escape(unit)}</h1>
<p class="lead">За последние {days:.0f} дн. · {html.escape(summary)}</p>
<p class="note">Исходные фото, голос и видео в отчёт не попадают: они удаляются
сразу после генерации. Для рисованного режима видна середина пути — портрет,
который нарисовал Kandinsky, прежде чем его оживил H3.</p>
<div class="grid">{''.join(card(j) for j in jobs)}</div>
</body></html>"""

(out / "report.html").write_text(page, encoding="utf-8")
print(f"задач: {len(jobs)}, файлов: {copied}, медиа: {megabytes:.0f} МБ")
PY

# zip есть не в каждом образе, а тянуть пакет ради одного архива глупо —
# tar лежит везде.
if command -v zip >/dev/null 2>&1; then
	ARCHIVE="$OUT.zip"
	( cd "$(dirname "$OUT")" && zip -qr "$ARCHIVE" "$(basename "$OUT")" )
else
	ARCHIVE="$OUT.tar.gz"
	tar -czf "$ARCHIVE" -C "$(dirname "$OUT")" "$(basename "$OUT")"
fi
chmod -R a+rX "$OUT" "$ARCHIVE"

echo "готово:"
echo "  папка: $OUT/report.html"
echo "  архив: $ARCHIVE"
echo
echo "забрать к себе (выполнять НА СВОЕЙ машине):"
echo "  scp vvkozlov@93.77.190.243:$ARCHIVE ."
