#!/bin/sh
# Установка мини-аппа на ВМ. Запускать под sudo:
#
#     sudo sh /tmp/deploy/miniapp/deploy/bootstrap-vm.sh
#
# Скрипт идемпотентный: повторный запуск ничего не сломает и не продублирует.
# Секреты он НЕ создаёт — их надо написать руками до запуска (см. шаг 2 ниже),
# потому что локальный .env с флагом отладки на сервер попадать не должен.

set -e

APP=/opt/avatar-miniapp
CORE=/opt/avatar-core
DATA=/var/lib/avatar-miniapp
SRC=/tmp/deploy
DOMAIN=kandiavatar.duckdns.org
PORT=8081

say() { printf '\n=== %s\n' "$1"; }

# --- 0. проверки до того, как что-то менять ---------------------------------

if [ "$(id -u)" != "0" ]; then
	echo "Запускать под sudo." >&2
	exit 1
fi
if [ -z "$GIT_MINIAPP" ] && { [ ! -d "$SRC/miniapp" ] || [ ! -d "$SRC/avatar-core" ]; }; then
	cat >&2 <<'HINT'
Нет исходников. Два способа, оба рабочие.

Через git (так деплой дальше в одну команду):
    sudo GIT_MINIAPP=https://github.com/<вы>/avatar-miniapp.git \
         GIT_CORE=https://github.com/<вы>/avatar-core.git \
         sh /tmp/deploy/miniapp/deploy/bootstrap-vm.sh

Копированием (быстрее один раз), с рабочей машины из C:\Avatars:
    scp -r avatar-core miniapp <вы>@<IP>:/tmp/deploy/
HINT
	exit 1
fi
if [ ! -f "$APP/.env" ] && [ ! -f "$SRC/.env" ]; then
	cat >&2 <<'HINT'
Нет файла с секретами. Создайте его НА СЕРВЕРЕ (локальный не копируем —
в нём флаг отладки, который открывает наши генерации всем желающим):

    sudo mkdir -p /opt/avatar-miniapp
    sudo tee /opt/avatar-miniapp/.env >/dev/null <<'EOF'
    H3_BASE_URL=http://<адрес инстанса H3>
    H3_API_KEY=ключ_от_H3
    MAX_BOT_TOKEN=токен_бота
    MAX_WEBAPP_URL=https://kandiavatar.duckdns.org
    MINIAPP_HOST=127.0.0.1
    MINIAPP_PORT=8081
    MINIAPP_DATA_DIR=/var/lib/avatar-miniapp
    MINIAPP_CHAT_ENABLED=1
    EOF

Строки MINIAPP_DEV_ALLOW_UNSIGNED здесь быть не должно.
HINT
	exit 1
fi
if grep -q '^MINIAPP_DEV_ALLOW_UNSIGNED=1' "$APP/.env" 2>/dev/null; then
	echo "В $APP/.env есть MINIAPP_DEV_ALLOW_UNSIGNED=1 — это открытая дверь." >&2
	echo "Уберите строку и запустите снова." >&2
	exit 1
fi

# --- 1. своп ----------------------------------------------------------------

say "своп"
if [ -f /swapfile ]; then
	echo "уже есть"
else
	fallocate -l 2G /swapfile
	chmod 600 /swapfile
	mkswap /swapfile >/dev/null
	swapon /swapfile
	grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >>/etc/fstab
	echo "создан, 2 ГБ"
fi
free -m | sed -n '1p;3p'

# --- 2. пользователь и каталоги ---------------------------------------------

say "пользователь и каталоги"
id avatar >/dev/null 2>&1 || useradd --system --home "$APP" --shell /usr/sbin/nologin avatar
mkdir -p "$APP" "$DATA"
chown -R avatar:avatar "$DATA"

# --- 3. код -----------------------------------------------------------------

say "код"
[ -f "$SRC/.env" ] && [ ! -f "$APP/.env" ] && mv "$SRC/.env" "$APP/.env"

# git clone не умеет в непустой каталог, а там уже лежит .env. Поэтому
# клонируем во временный и переносим содержимое, .env не трогая.
clone_into() {
	url=$1
	dest=$2
	if [ -d "$dest/.git" ]; then
		# От владельца каталога: root на чужом репозитории получает
		# «dubious ownership» и не делает ничего.
		sudo -u avatar git -C "$dest" pull --ff-only
		return
	fi
	command -v git >/dev/null || apt-get install -y -qq git
	tmp=$(mktemp -d)
	git clone --depth 1 "$url" "$tmp/repo"
	mkdir -p "$dest"
	rm -rf "$tmp/repo/.git-keep"
	cp -r "$tmp/repo/." "$dest/"
	rm -rf "$tmp"
}

if [ -n "$GIT_MINIAPP" ]; then
	clone_into "$GIT_MINIAPP" "$APP"
	clone_into "${GIT_CORE:?нужен и GIT_CORE — адрес репозитория avatar-core}" "$CORE"
	echo "взят из git"
else
	cp -r "$SRC/miniapp/." "$APP/"
	rm -rf "$CORE"
	cp -r "$SRC/avatar-core" "$CORE"
	echo "скопирован из $SRC"
fi

chown -R avatar:avatar "$APP" "$CORE"
chmod 600 "$APP/.env"

# --- 4. окружение python -----------------------------------------------------

say "окружение python"
[ -x "$APP/.venv/bin/python" ] || sudo -u avatar python3 -m venv "$APP/.venv"
sudo -u avatar "$APP/.venv/bin/pip" install --quiet --upgrade pip
sudo -u avatar "$APP/.venv/bin/pip" install --quiet -e "$CORE"
sudo -u avatar "$APP/.venv/bin/pip" install --quiet aiohttp "maxapi==1.2.2" pillow
command -v ffmpeg >/dev/null || { echo "ставлю ffmpeg"; apt-get install -y -qq ffmpeg; }
"$APP/.venv/bin/python" -c "import avatar_core, aiohttp, maxapi; print('зависимости на месте')"

# --- 5. caddy ---------------------------------------------------------------

say "caddy"
if ! getent hosts "$DOMAIN" >/dev/null; then
	echo "ВНИМАНИЕ: $DOMAIN не резолвится. Сертификат Caddy получить не сможет." >&2
fi
if grep -q "$DOMAIN" /etc/caddy/Caddyfile 2>/dev/null; then
	echo "блок уже есть"
else
	cat "$APP/deploy/Caddyfile.fragment" >>/etc/caddy/Caddyfile
	echo "блок добавлен"
fi
caddy validate --config /etc/caddy/Caddyfile >/dev/null && systemctl reload caddy
echo "перечитан"

# --- 6. сервис ---------------------------------------------------------------

say "сервис"
cp "$APP/deploy/avatar-miniapp.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --quiet avatar-miniapp
systemctl restart avatar-miniapp
sleep 4
systemctl is-active --quiet avatar-miniapp || {
	echo "сервис не поднялся:" >&2
	journalctl -u avatar-miniapp -n 30 --no-pager >&2
	exit 1
}

# --- 7. проверка — она же критерий «выложилось» -------------------------------

say "проверка"
LOCAL=$(curl -s -m 5 "http://127.0.0.1:$PORT/api/health" || true)
echo "изнутри:  $LOCAL"
echo "снаружи:  $(curl -s -m 15 "https://$DOMAIN/api/health" || echo 'НЕ ОТВЕЧАЕТ')"
echo "без подписи: $(curl -s -m 15 -o /dev/null -w '%{http_code}' "https://$DOMAIN/api/state" || true) (ожидаем 401)"

case "$LOCAL" in
*'"dev_unsigned": true'*)
	echo >&2
	echo "ПРОВАЛ: dev_unsigned=true. На сервер уехал локальный .env." >&2
	echo "Уберите MINIAPP_DEV_ALLOW_UNSIGNED из $APP/.env и перезапустите сервис." >&2
	exit 1
	;;
esac

cat <<EOF

Готово. Дальше:
  journalctl -u avatar-miniapp -f      смотреть логи
  systemctl restart avatar-miniapp     перезапустить

И на рабочей машине в %USERPROFILE%\\.avatars\\secrets.env поставьте
MINIAPP_CHAT_ENABLED=0 — бот теперь крутится здесь, а один токен
терпит только один процесс long polling.
EOF
