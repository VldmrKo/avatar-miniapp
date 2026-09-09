# Три репозитория

Ограничение с первого дня: стенд сравнения моделей на GitHub не уезжает.
Разделяем физически, а не `.gitignore` — так его нельзя обойти случайно
одним `git add -f`.

```
C:\Avatars\
  avatar-core\   репозиторий, ПУБЛИЧНЫЙ   — общая библиотека
  miniapp\       репозиторий, ПУБЛИЧНЫЙ   — мини-приложение
  lab\           репозиторий, ПРИВАТНЫЙ   — стенд сравнения
  findings.md, research_h3_duration.md, PLAN_STAGE*.md, MAX_PLATFORM.md
                 ← лежат в корне, вне всех трёх репозиториев
  vm-check.sh    ← туда же
```

Ничего перекладывать не нужно: `git init` делается в каждой папке отдельно,
пути в `.bat` остаются рабочими, внутренние документы физически не попадают
ни в один репозиторий.

## Что сделать один раз

На GitHub создать три пустых репозитория **без** README и .gitignore
(иначе первый push упрётся в расхождение историй):

| репозиторий | видимость |
|---|---|
| `avatar-core` | публичный |
| `avatar-miniapp` | публичный |
| `avatar-lab` | **приватный** |

Дальше из `C:\Avatars`, по одной папке:

```
cd C:\Avatars\avatar-core
git init -b main
git add .
git commit -m "Общая библиотека: провайдеры, медиа, оценка речи"
git remote add origin https://github.com/<вы>/avatar-core.git
git push -u origin main

cd C:\Avatars\miniapp
git init -b main
git add .
git commit -m "Мини-приложение в MAX: скелет с заглушкой генерации"
git remote add origin https://github.com/<вы>/avatar-miniapp.git
git push -u origin main

cd C:\Avatars\lab
git init -b main
git add .
git commit -m "Стенд сравнения моделей"
git remote add origin https://github.com/<вы>/avatar-lab.git
git push -u origin main
```

## Проверка перед первым push

Секреты лежат в `%USERPROFILE%\.avatars\secrets.env`, то есть вне дерева
проекта, и попасть в репозиторий не могут. Но убедиться дешевле, чем потом
чистить историю:

```
cd C:\Avatars\avatar-core && git status --short
cd C:\Avatars\miniapp     && git status --short
```

В списке не должно быть `.env`, `data/`, `.venv/`, `secrets`. В `miniapp`
это уже закрыто `.gitignore`; в `avatar-core` проверьте, что там тоже есть
`.gitignore` с `.venv/` и `__pycache__/`.

И отдельно: `C:\Avatars\lab` — приватный. Если случайно создадите публичным,
переключить видимость можно в настройках репозитория, но всё, что успело
уехать, считайте опубликованным.

## Зачем это до первой выкладки

Деплой из git — одна команда, и она же откатывается:

```sh
sudo sh /opt/avatar-miniapp/deploy/update-vm.sh
```

Против `scp -r` с рабочей машины каждый раз, без истории и без понимания,
что именно сейчас крутится на сервере. Разница копится с каждым шагом,
а сделать разделение сейчас, пока в `miniapp` двадцать файлов и нет истории,
дешевле, чем потом.
