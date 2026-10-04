#!/bin/sh
# Установка/удаление backupd. Запускать от root:  ./install.sh [install|uninstall]
set -eu

PREFIX=/opt/backupd
CONF_DIR=/etc/backupd
UNIT=/etc/systemd/system/backupd.service
HERE=$(cd "$(dirname "$0")" && pwd)

[ "$(id -u)" -eq 0 ] || { echo "Нужны права root" >&2; exit 1; }

case "${1:-install}" in
install)
    command -v python3 >/dev/null || { echo "Не найден python3" >&2; exit 1; }
    install -d -m 755 "$PREFIX"
    install -m 755 "$HERE/backupd.py" "$HERE/backupctl.py" "$PREFIX/"
    install -m 644 "$HERE/README.md" "$PREFIX/" 2>/dev/null || true
    ln -sf "$PREFIX/backupd.py"   /usr/local/sbin/backupd
    ln -sf "$PREFIX/backupctl.py" /usr/local/bin/backupctl

    install -d -m 700 -o root -g root "$CONF_DIR"
    if [ ! -e "$CONF_DIR/backupd.conf" ]; then
        install -m 600 -o root -g root "$HERE/backupd.conf" "$CONF_DIR/backupd.conf"
        echo "Создан $CONF_DIR/backupd.conf — ОТРЕДАКТИРУЙТЕ source/destination перед запуском."
    fi

    # каталог назначения берём из конфига и прописываем в unit-файл
    DEST=$(sed -n 's/^[[:space:]]*destination[[:space:]]*=[[:space:]]*\([^#;]*[^#; ]\).*/\1/p' "$CONF_DIR/backupd.conf" | head -n1)
    DEST=${DEST:-/var/backups/backupd}
    install -d -m 700 -o root -g root "$DEST"
    install -m 644 "$HERE/backupd.service" "$UNIT"
    sed -i "s|^ReadWritePaths=.*|ReadWritePaths=$DEST|" "$UNIT"

    /usr/local/sbin/backupd --check-config -c "$CONF_DIR/backupd.conf"
    systemctl daemon-reload
    systemctl enable --now backupd
    echo "Готово. Проверка: backupctl status"
    ;;
uninstall)
    systemctl disable --now backupd 2>/dev/null || true
    rm -f "$UNIT" /usr/local/sbin/backupd /usr/local/bin/backupctl
    rm -rf "$PREFIX"
    systemctl daemon-reload
    echo "Удалено. Конфигурация ($CONF_DIR) и копии сохранены."
    ;;
*)  echo "Использование: $0 [install|uninstall]" >&2; exit 2 ;;
esac
