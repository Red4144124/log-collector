# Сервис для сбора логов с устройства на сервер

Python + JSON

## Инструкция по настройке взаимодействия устройства и сервера через SSH по ключу

На сервере необходимо выполнить следующие шаги:

`sudo useradd -m -s /bin/bash loguser`
`sudo mkdir -p /home/loguser/.ssh`
`sudo chmod 700 /home/loguser/.ssh`

На устройстве:

`dropbearkey -t rsa -s 4096 -f /root/.ssh/id_dropbear_rsa`

Далее копируем сгенерированный ключ из вывода:

`dropbearkey -y -f /root/.ssh/id_dropbear_rsa`

На сервере:

`echo "your_key" >> authorized_keys`

`sudo chmod 700 /home/loguser/.ssh`
`sudo chmod 600 /home/loguser/.ssh/authorized_keys`
`sudo chown -R loguser:loguser /home/loguser/.ssh`

_____________________________________________________________________________________

# Добавление сервиса на плату:

`mkdir -p /var/log/live /var/log/collected /var/lib/log-collector`

В директорию (/etc/log-collector/) добавить конфиг из директории /config (важно в конфиг-файле указать IP адрес сервера)

В директорию (/usr/local/bin) разместить файл scp_log_collector.py (из директории /device_script)

В директорию (/etc/systemd/system/) разместить файл сервиса из директории /systemctl (log-collector.service)

Выполнить команды:

`systemctl daemon-reload`
`systemctl enable log-collector`
`systemctl start log-collector`

Логи будут размещаться на сервере по пути:

/home/loguser/device_logs/"Device_name"/*.log

Добавить скрипт для ротации логов (/rotate):

/usr/local/bin/rotate_server_logs.sh

`chmod +x /usr/local/bin/rotate_server_logs.sh`

