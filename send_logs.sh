#!/bin/bash

# ---- Конфигурация ----
LOG_DIR="/var/log/"          # Ваши прикладные логи
TEMP_DIR="/tmp/remote_logs"        # Временная папка для сбора системных логов
REMOTE_USER="loguser"
REMOTE_HOST="172.16.12.51"
REMOTE_PATH="/home/loguser/device_logs/rsync/$(hostname)/"
SSH_KEY="/root/.ssh/id_dropbear_rsa"

# ---- Создаём временную папку для сбора логов ----
rm -rf "$TEMP_DIR"
mkdir -p "$TEMP_DIR"

# ---- Сохраняем dmesg (кольцевой буфер ядра) ----
dmesg > "$TEMP_DIR/dmesg.log"

# ---- Сохраняем journalctl (системный журнал) ----
# --since "1 hour ago"  - можно ограничить период, чтобы файл не разрастался
# --no-pager            - отключаем постраничный вывод
journalctl --since "1 hour ago" --no-pager > "$TEMP_DIR/journalctl.log"

# ---- Если нужен полный журнал (осторожно, большой) ----
# journalctl --no-pager > "$TEMP_DIR/journalctl_full.log"

# ---- (Опционально) Добавляем логи загрузки ----
journalctl -b 0 --no-pager > "$TEMP_DIR/journalctl_boot.log"

# ---- Запуск rsync: синхронизируем оба источника ----
# Синхронизируем прикладные логи и временную папку с системными логами
rsync -avz --delete \
    -e "dbclient -i $SSH_KEY -y" \
    "$LOG_DIR" \
    "$TEMP_DIR/" \
    "$REMOTE_USER@$REMOTE_HOST:$REMOTE_PATH"

# ---- Очистка временной папки (опционально) ----
rm -rf "$TEMP_DIR"