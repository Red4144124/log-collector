#!/bin/bash
# /usr/local/bin/sync-logs-rsync.sh
# Каждый запуск создаёт на сервере: device_logs/rsync/имя_устройства/YYYYMMDD_HHMMSS/
# Внутри — все логи, собранные в этот момент

REMOTE_USER="loguser"
REMOTE_HOST="172.16.12.51"
REMOTE_BASE="/home/loguser/device_logs/rsync/$(hostname)/"
SSH_KEY="/root/.ssh/id_dropbear_rsa"

# Используем SD-карту для временных файлов
WORK_BASE="/media/card/logs_upload"
mkdir -p "$WORK_BASE"

# Проверка монтирования SD-карты
if ! mountpoint -q /media/card; then
    echo "$(date): SD card not mounted, exiting" >> /var/log/sync-logs.log
    exit 1
fi

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
WORK_DIR="${WORK_BASE}/remote_logs_${TIMESTAMP}"
mkdir -p "$WORK_DIR"

# ---- Генерация дампов ----
dmesg > "${WORK_DIR}/dmesg.log"
journalctl --since "5 minutes ago" --no-pager > "${WORK_DIR}/journalctl.log"

# ---- Копирование пользовательских логов (без добавления метки в имя) ----
# Имена файлов остаются оригинальными, т.к. папка уже имеет метку времени
for logfile in kernel.log syslog auth.log \
               gst_demo.log hls_stream.log \
               main_web_server.log main_web_server_image_encoder.log \
               sys_settings.log; do
    [ -f "/var/log/$logfile" ] && cp "/var/log/$logfile" "${WORK_DIR}/"
done

# ---- Отправка на сервер (создаётся папка с TIMESTAMP) ----
rsync -avz --ignore-existing --mkpath \
    -e "dbclient -i $SSH_KEY -y" \
    "$WORK_DIR/" \
    "$REMOTE_USER@$REMOTE_HOST:${REMOTE_BASE}${TIMESTAMP}/"

# ---- Очистка временной папки на SD-карте ----
rm -rf "$WORK_DIR"