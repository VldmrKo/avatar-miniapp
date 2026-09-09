#!/bin/sh
# Повторная выкладка. Под sudo:
#
#     sudo sh /opt/avatar-miniapp/deploy/update-vm.sh
#
# Если код взят из git — делает pull. Если копированием — ждёт свежую копию
# в /tmp/deploy. Незавершённые задачи перезапуск переживают: запись о них
# ложится на диск до начала ожидания, и на старте сервис их подбирает.

set -e

APP=/opt/avatar-miniapp
CORE=/opt/avatar-core
SRC=/tmp/deploy
DOMAIN=kandiavatar.duckdns.org
PORT=8081

[ "$(id -u)" = "0" ] || { echo "Запускать под sudo." >&2; exit 1; }

if [ -d "$APP/.git" ]; then
	echo "=== git pull"
	git -C "$APP" pull --ff-only
	[ -d "$CORE/.git" ] && git -C "$CORE" pull --ff-only
elif [ -d "$SRC/miniapp" ]; then
	echo "=== копирую из $SRC"
	cp -r "$SRC/miniapp/." "$APP/"
	[ -d "$SRC/avatar-core" ] && cp -r "$SRC/avatar-core/." "$CORE/"
else
	echo "Нечего выкладывать: нет ни git, ни $SRC/miniapp." >&2
	exit 1
fi

chown -R avatar:avatar "$APP" "$CORE"
chmod 600 "$APP/.env"

# Зависимости могли поменяться вместе с кодом; переустановка пакета,
# который уже стоит, ничего не стоит.
sudo -u avatar "$APP/.venv/bin/pip" install --quiet -e "$CORE"
sudo -u avatar "$APP/.venv/bin/pip" install --quiet aiohttp "maxapi==1.2.2" pillow

echo "=== перезапуск"
systemctl restart avatar-miniapp
sleep 4
systemctl is-active --quiet avatar-miniapp || {
	journalctl -u avatar-miniapp -n 30 --no-pager >&2
	exit 1
}

HEALTH=$(curl -s -m 5 "http://127.0.0.1:$PORT/api/health" || true)
echo "изнутри: $HEALTH"
echo "снаружи: $(curl -s -m 15 "https://$DOMAIN/api/health" || echo 'НЕ ОТВЕЧАЕТ')"

case "$HEALTH" in
*'"dev_unsigned": true'*)
	echo "ПРОВАЛ: dev_unsigned=true — выкладка не считается успешной." >&2
	exit 1
	;;
esac
echo "готово"
