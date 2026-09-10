# Второй бот: Telegram

Тот же код, тот же сервер, тот же venv. Отличаются мессенджер, токен
и каталог данных. Мини-приложения у него нет — весь путь идёт в переписке,
поэтому ни домена, ни маршрута в Caddy, ни сертификата не нужно: Telegram
работает по long polling, входящих соединений не требуется.

## Один раз

**1. Завести бота.** В Telegram у @BotFather: `/newbot`, имя и username.
Он выдаст токен вида `123456789:AA...`.

Полезное там же:

```
/setdescription   что бот умеет — видно до первого запуска
/setcommands      start - начать
```

**2. Каталоги на сервере.**

```sh
sudo install -d -o avatar -g avatar /var/lib/avatar-tgbot
# Общий замок к модели: инстанс H3 один, а ботов теперь двое.
sudo install -d -o avatar -g avatar /var/lib/avatar-shared
```

**3. Настройки этого бота.** Общие секреты (H3, Kandinsky) остаются
в `/opt/avatar-miniapp/.env` — их читают оба. Здесь только различия:

```sh
sudo -u avatar tee /opt/avatar-miniapp/.env.tg >/dev/null <<'ENV'
MINIAPP_MESSENGER=telegram
TELEGRAM_BOT_TOKEN=сюда_токен_от_BotFather
MINIAPP_DATA_DIR=/var/lib/avatar-tgbot
MINIAPP_H3_LOCK=/var/lib/avatar-shared/h3.lock
MINIAPP_PORT=8082
ENV
sudo chmod 600 /opt/avatar-miniapp/.env.tg
sudo chown avatar:avatar /opt/avatar-miniapp/.env.tg
```

Порт нужен только для `/api/health`; наружу он не смотрит — окна у этого
бота нет. Главное, чтобы не совпал с 8081 (MAX) и 8080 (соседний бот).

**4. Замок и соседу.** MAX-бот тоже должен уметь брать общий замок,
иначе договариваться будет не с кем:

```sh
sudo tee -a /opt/avatar-miniapp/.env >/dev/null <<'ENV'
MINIAPP_H3_LOCK=/var/lib/avatar-shared/h3.lock
ENV
```

**5. Юниты.**

```sh
sudo cp /opt/avatar-miniapp/deploy/avatar-tgbot.service /etc/systemd/system/
sudo cp /opt/avatar-miniapp/deploy/avatar-miniapp.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now avatar-tgbot
sudo systemctl restart avatar-miniapp
```

**6. Голоса.** Готовые голоса лежат рядом с данными, а данные у ботов
разные. Проще всего дать один и тот же каталог обоим:

```sh
sudo -u avatar cp /var/lib/avatar-miniapp/voices/*.wav /var/lib/avatar-tgbot/voices/
```

## Если Telegram с сервера недоступен

Наш случай. С ВМ в Yandex Cloud `api.telegram.org` не отвечает: соединение
висит до таймаута и по IPv4 (`149.154.166.110`), и по IPv6. При этом GitHub
и Kandinsky с той же машины работают, а с рабочего ноутбука Telegram
доступен. То есть режет площадка, а не страна и не корпоративная сеть.

Проверяется одной командой:

```sh
curl -4 -m 10 https://api.telegram.org/
```

Таймаут — недоступен. Любой ответ, включая ошибку от самого Telegram, —
доступен, и ничего из этого раздела не нужно.

### Туннель вместо прокси

Нужна любая машина, с которой Telegram виден, и обычный SSH-доступ на неё.
Заграница необязательна: режет конкретный хостер, поэтому подойдёт и
российский VPS у другого провайдера — так и данные остаются где были.

Прокси-сервер с портом наружу ставить НЕ надо: открытый прокси находят
сканерами за сутки и начинают гонять через него чужой трафик. SSH умеет
то же самое без единого открытого порта — `ssh -D` поднимает SOCKS5 на
нашей стороне, соединение исходящее, авторизация по ключу.

```sh
sudo apt install autossh
sudo -u avatar ssh-keygen -t ed25519 -f /var/lib/avatar-tgbot/tunnel_key -N ""
sudo cat /var/lib/avatar-tgbot/tunnel_key.pub
```

Публичную половину — на VPS, в `~/.ssh/authorized_keys` пользователя
`tunnel`, и сразу ограничить, чтобы этим ключом нельзя было ничего, кроме
туннеля:

```
restrict,port-forwarding,command="/bin/false" ssh-ed25519 AAAA...
```

Дальше юнит туннеля (в нём заменить адрес VPS) и строка в настройках бота:

```sh
sudo cp /opt/avatar-miniapp/deploy/telegram-tunnel.service /etc/systemd/system/
sudo nano /etc/systemd/system/telegram-tunnel.service   # адрес VPS
sudo systemctl daemon-reload
sudo systemctl enable --now telegram-tunnel

echo 'TELEGRAM_PROXY=socks5://127.0.0.1:1080' | sudo tee -a /opt/avatar-miniapp/.env.tg
sudo systemctl restart avatar-tgbot
```

Проверить сам туннель, не поднимая бота:

```sh
curl -x socks5h://127.0.0.1:1080 -m 10 -sS -o /dev/null -w '%{http_code}\n' \
     https://api.telegram.org/
```

### Чего делать не нужно

Вебхук вместо опроса не помогает: обновления он и правда принесёт сам,
но отправлять сообщения и скачивать присланные файлы всё равно нам, и
это те же исходящие к `api.telegram.org`.

## Проверка

```sh
sudo journalctl -u avatar-tgbot -n 30 --no-pager
```

В первых строках должно быть `мессенджер: telegram`, имя бота и
`слушаю http://127.0.0.1:8082`. Дальше — написать боту `/start`.

Если в журнале `TELEGRAM_BOT_TOKEN пуст` — systemd не увидел `.env.tg`
(проверьте права и владельца) либо строка в нём с опечаткой.

## Дальше как обычно

Выкладка одна на обоих:

```sh
sudo sh /opt/avatar-miniapp/deploy/update-vm.sh
```

Скрипт сам перезапустит оба юнита и проверит, что оба живы.

Отчёт считает ботов порознь — это разные аудитории, и складывать их
в одну кучу значит не увидеть, ради чего переезжали:

```sh
sudo sh /opt/avatar-miniapp/deploy/report.sh 7                # MAX
sudo sh /opt/avatar-miniapp/deploy/report.sh 7 avatar-tgbot   # Telegram
```
