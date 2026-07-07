# ansible-vps

Ansible-проект: **VLESS+Reality VPN** + **Telegram-бот** для статистики и управления.

## Стек

| Компонент | Что делает |
|-----------|-----------|
| **xray-core** | VLESS+Reality, Stats gRPC API |
| **tgbot** | aiogram 3, SQLite, APScheduler |
| **common** | Swap, BBR, UFW, fail2ban, SSH hardening |
| **node_exporter** | Prometheus метрики хоста (отключён, для Pi) |

## Структура

```
ansible-vps/
├── site.yml                    # Главный playbook
├── gen-links.yml               # Генерация VLESS-ссылок клиентов
├── Makefile                    # make deploy / links / vault-edit …
├── inventory/hosts.yml         # IP сервера
├── group_vars/vps/
│   ├── vars.yml                # Публичные настройки
│   └── vault.yml               # 🔐 Зашифрованные секреты (ключи, токен, UUID)
└── roles/
    ├── common/                 # Базовая настройка сервера
    ├── xray/                   # VPN-сервер
    ├── tgbot/                  # Telegram-бот
    └── node_exporter/          # Prometheus экспортер (отключён)
```

---

## Быстрый старт

### 1. Установить Ansible

```bash
pip install ansible
ansible-galaxy collection install community.general ansible.posix
```

### 2. Прописать IP сервера

```bash
# inventory/hosts.yml
vi inventory/hosts.yml        # ansible_host: "1.2.3.4"

# group_vars/vps/vars.yml
vi group_vars/vps/vars.yml    # vps_host: "1.2.3.4"
```

### 3. Создать бота в Telegram

Написать @BotFather → `/newbot` → скопировать токен.  
Узнать свой Telegram ID: написать @userinfobot.

### 4. Сгенерировать ключи и UUID

```bash
chmod +x scripts/gen-keys.sh
./scripts/gen-keys.sh           # требует Docker
```

Вывод скрипта — все значения для vault.yml.

### 5. Заполнить и зашифровать vault

```bash
cp group_vars/vps/vault.yml.example group_vars/vps/vault.yml
vi group_vars/vps/vault.yml     # вставить ключи, UUID, токен, свой TG ID
ansible-vault encrypt group_vars/vps/vault.yml
```

### 6. Сохранить пароль от vault

```bash
echo "твой_пароль" > .vault_pass
chmod 600 .vault_pass
# файл уже в .gitignore
```

### 7. Запустить деплой

```bash
make deploy
# или: ansible-playbook site.yml --ask-vault-pass
```

### 8. Получить ссылки для клиентов

```bash
make links
```

Скопировать ссылку в **v2rayN** / **NekoBox** / **v2rayNG** / **Shadowrocket**.

---

## Управление пользователями

### Добавить пользователя

```bash
# Сгенерировать UUID и short_id
python3 -c "import uuid; print(uuid.uuid4())"
openssl rand -hex 4

# Отредактировать vault
make vault-edit
# Добавить блок:
# - name:     "masha"
#   uuid:     "<сгенерированный UUID>"
#   short_id: "<сгенерированный short_id>"

# Применить только xray (без перезапуска бота)
make xray-only

# Напечатать ссылку для нового пользователя
make links
```

### Удалить пользователя

```bash
make vault-edit     # удалить блок пользователя
make xray-only      # применить
```

---

## Telegram-бот — команды

| Команда | Описание |
|---------|---------|
| `/stats` | Трафик за последнюю минуту |
| `/stats_hour` | Трафик за час |
| `/stats_day` | Трафик за сутки |
| `/stats_month` | Трафик за 30 дней |
| `/users` | Живые счётчики xray (без сброса) |
| `/status` | CPU / RAM / Disk / сетевой трафик |
| `/help` | Список команд |

Бот отвечает только Telegram ID из `tgbot_admin_ids` в vault.

---

## Makefile — быстрая справка

```bash
make deploy       # Полный деплой
make check        # Dry-run (--check --diff)
make ping         # Проверить SSH
make xray-only    # Только роль xray
make bot-only     # Только роль tgbot
make links        # VLESS-ссылки для всех клиентов
make vault-edit   # Редактировать vault
make help         # Эта справка
```

---

## Подключение node_exporter к Pi

Когда мониторинг на Pi будет готов:

1. Раскомментировать в `site.yml`:
   ```yaml
   - role: node_exporter
     tags: [node_exporter]
   ```
2. В `roles/node_exporter/tasks/main.yml` вписать IP малинки в UFW-правило.
3. Добавить scrape в Prometheus на Pi:
   ```yaml
   - job_name: vps
     static_configs:
       - targets: ['YOUR_VPS_IP:9100']
   ```
4. `make deploy --tags node_exporter`

---

## Безопасность

- `.vault_pass` в `.gitignore`, не попадёт в git
- `vault.yml` зашифрован → безопасно коммитить
- UFW: только порты 22 (SSH) и 443 (xray) открыты
- fail2ban: SSH — 5 попыток → бан на 1 час
- xray работает от `nobody`, бот от `tgbot`
- Stats API только на `127.0.0.1:10085`
- SSH: отключена аутентификация по паролю
