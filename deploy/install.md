# Выкладка на ВМ

Машина: `kandibot`, Ubuntu, 2 ядра, 1966 МБ памяти, Caddy уже работает,
соседний бот `kandiapp.service` занимает порт 8080. Мы садимся на 8081.

Порядок ниже — один раз. Повторная выкладка внизу, в три строки.

---

## 0. Своп (до всего остального)

Памяти меньше двух гигабайт и свопа нет. При нехватке ядро убивает не того,
кто запросил память, а самого крупного — то есть может прилететь соседнему
боту, который вообще ни при чём.

```sh
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
free -m          # в строке Swap должно появиться 2047
```

---

## 1. Пользователь и каталоги

Сервис не должен ходить от root и не должен писать туда, где лежит код.

```sh
sudo useradd --system --home /opt/avatar-miniapp --shell /usr/sbin/nologin avatar
sudo mkdir -p /opt/avatar-miniapp /var/lib/avatar-miniapp
sudo chown -R avatar:avatar /var/lib/avatar-miniapp
```

---

## 2. Код на машину

Первый раз — простым копированием с рабочей машины (в Windows `scp` есть
из коробки). Из `C:\Avatars`:

```
scp -r avatar-core miniapp vvkozlov@<IP>:/tmp/deploy/
```

На ВМ:

```sh
sudo mkdir -p /opt/avatar-miniapp
sudo cp -r /tmp/deploy/miniapp/* /opt/avatar-miniapp/
sudo cp -r /tmp/deploy/avatar-core /opt/avatar-core
sudo chown -R avatar:avatar /opt/avatar-miniapp /opt/avatar-core
```

Со второй выкладки это надоест. Тогда — два публичных репозитория
(`avatar-core` и `miniapp`), `git clone` сюда же, дальше `git pull`.
Стенд `lab` остаётся приватным и на сервер не едет вовсе.

---

## 3. Окружение Python

```sh
sudo -u avatar python3 -m venv /opt/avatar-miniapp/.venv
sudo -u avatar /opt/avatar-miniapp/.venv/bin/pip install --upgrade pip
sudo -u avatar /opt/avatar-miniapp/.venv/bin/pip install -e /opt/avatar-core
sudo -u avatar /opt/avatar-miniapp/.venv/bin/pip install aiohttp maxapi==1.2.2 pillow
sudo apt-get install -y ffmpeg      # понадобится на пятом шаге, для видео
```

---

## 4. Секреты — создаются ЗДЕСЬ, а не копируются

Локальный `secrets.env` содержит `MINIAPP_DEV_ALLOW_UNSIGNED=1`. Если он
уедет на сервер, любой с curl будет жечь наши генерации. Поэтому серверный
файл пишется руками на сервере:

```sh
sudo -u avatar tee /opt/avatar-miniapp/.env >/dev/null <<'EOF'
H3_BASE_URL=http://<адрес инстанса H3>
H3_API_KEY=ключ_от_H3
MAX_BOT_TOKEN=токен_бота
MAX_WEBAPP_URL=https://kandiavatar.duckdns.org
MINIAPP_HOST=127.0.0.1
MINIAPP_PORT=8081
MINIAPP_DATA_DIR=/var/lib/avatar-miniapp
MINIAPP_CHAT_ENABLED=1
EOF
sudo chmod 600 /opt/avatar-miniapp/.env
```

Строки `MINIAPP_DEV_ALLOW_UNSIGNED` здесь быть не должно вовсе.
`MINIAPP_DATA_DIR` задан явно: юнит разрешает запись только в
`/var/lib/avatar-miniapp`, и без этой строки сервис попробует писать рядом
с кодом.

---

## 5. Caddy

Существующий блок `kandibot.duckdns.org` не трогаем, добавляем свой.
Содержимое — в `Caddyfile.fragment` рядом.

```sh
sudo tee -a /etc/caddy/Caddyfile < /opt/avatar-miniapp/deploy/Caddyfile.fragment
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

Сертификат Caddy возьмёт сам, но для этого имя должно уже резолвиться
в этот IP. Проверить до reload:

```sh
getent hosts kandiavatar.duckdns.org
```

---

## 6. Сервис

```sh
sudo cp /opt/avatar-miniapp/deploy/avatar-miniapp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now avatar-miniapp
journalctl -u avatar-miniapp -n 40 --no-pager
```

В логе должно быть `слушаю http://127.0.0.1:8081` и `бот @имя на связи`.
Предупреждения про неподписанные запросы быть не должно.

---

## 7. Проверка — она же критерий «выложилось»

```sh
curl -s http://127.0.0.1:8081/api/health
curl -s https://kandiavatar.duckdns.org/api/health
```

Ожидаем:

```json
{"ok": true, "dev_unsigned": false, "chat": true, "bot": "...", "jobs_active": 0}
```

**`dev_unsigned` должен быть `false`.** Если `true` — выкладка не удалась,
на сервер уехал локальный `.env`. Убрать строку и перезапустить.

И проверка, что подпись реально требуется:

```sh
curl -s -o /dev/null -w '%{http_code}\n' https://kandiavatar.duckdns.org/api/state
# ожидаем 401
```

---

## 8. Сразу после этого — на локальной машине

В `%USERPROFILE%\.avatars\secrets.env` поставить:

```
MINIAPP_CHAT_ENABLED=0
```

Один токен = один процесс long polling. Пока бот крутится на сервере,
локально его поднимать нельзя: апдейты поделятся между двумя процессами,
и бот начнёт отвечать через раз. Веб-часть локально при этом работает
полностью, а флаг `MINIAPP_DEV_ALLOW_UNSIGNED=1` остаётся только локально.

---

## Повторная выкладка

```sh
# на рабочей машине, из C:\Avatars
scp -r miniapp/src miniapp/deploy vvkozlov@<IP>:/tmp/deploy/

# на ВМ
sudo cp -r /tmp/deploy/src /opt/avatar-miniapp/ && sudo chown -R avatar:avatar /opt/avatar-miniapp
sudo systemctl restart avatar-miniapp && curl -s http://127.0.0.1:8081/api/health
```

Незавершённые задачи перезапуск переживают: запись о них пишется на диск
до начала ожидания, и на старте сервис их подбирает.
