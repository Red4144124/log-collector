# Сервис для сбора логов с устройства на сервер

# 📡 ISS Log Collector — Push-модель сбора логов через rsync

Скрипт для автоматической отправки системных и пользовательских логов с удалённого устройства (платы) на центральный сервер с использованием `rsync` и SSH (Dropbear на клиенте, OpenSSH на сервере).

## 📦 Состав репозитория

iss_log_collector/
│ └── send_logs.sh # основной скрипт отправки логов (устанавливается на плату)

# На устройстве

`dropbearkey -t ed25519 -f /etc/dropbear/id_ed25519`


`dropbearkey -y -f /etc/dropbear/id_ed25519`

# На сервере

`echo 'ssh-ed25519 AAAA...' >> /home/loguser/.ssh/authorized_keys`


```
scp device_script/send_logs.sh root@<IP_платы>:/usr/local/bin/
```

`chmod +x /usr/local/bin/send_logs.sh`

Далее добавим скрипт в планировщик:

`crontab -e`

`*/5 * * * * /usr/local/bin/send_logs.sh` - пример для запуска скрипта каждые 5 минут