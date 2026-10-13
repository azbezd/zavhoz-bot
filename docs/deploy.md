# Развёртывание и эксплуатация

Для тех, кто держит бота на сервере. Быстрый запуск на своей машине — в [README](../README.md#установка).

## Переменные окружения

| Переменная | Обязательна | По умолчанию | Что делает |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | да | — | токен бота от @BotFather |
| `OPENAI_API_KEY` | да | — | ключ OpenAI API |
| `ALLOWED_TELEGRAM_USER_IDS` | да | пусто | id пользователей через запятую; пусто — бот никому не отвечает |
| `OPENAI_MODEL` | нет | `gpt-5.4-mini` | модель для разбора и ответов |
| `OPENAI_BASE_URL` | нет | `https://api.openai.com/v1` | совместимый с OpenAI адрес API |
| `INVENTORY_ENABLE_WEB_SEARCH` | нет | `1` | поиск в сети при обогащении карточек |
| `INVENTORY_DB` | нет | `inventory_bot/inventory.db` | путь к базе SQLite |
| `INVENTORY_REPO_DIR` | нет | текущая папка | куда складываются фото и выгрузка данных |
| `INVENTORY_AUTO_GIT` | нет | `0` | `1` — после применения черновика коммитить и пушить данные |
| `ZAVHOZ_SITE_BASE` | нет | каталог автора | адрес вашего каталога: на него ведут ссылки из ответов бота |
| `HTTPS_PROXY`, `NO_PROXY` | нет | — | прокси для исходящих запросов, если OpenAI недоступен из сети сервера |

Шаблон — [`inventory_bot/env.example`](../inventory_bot/env.example). На сервере храните файл вне репозитория с правами `0600`.

## Служба systemd

```ini
# /etc/systemd/system/zavhoz-bot.service
[Unit]
Description=Zavhoz Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
EnvironmentFile=/etc/zavhoz-bot.env
WorkingDirectory=/opt/zavhoz/zavhoz-bot
ExecStart=/usr/bin/python3 -m inventory_bot.bot
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
systemctl daemon-reload
systemctl enable --now zavhoz-bot
journalctl -u zavhoz-bot -f
```

Обновление: `git pull` в рабочей папке и `systemctl restart zavhoz-bot`.

## Каталог

```bash
python3 -m inventory_bot.build_site /путь/к/клону-сайта
```

Генератор перезаписывает папку целиком: `index.html`, `item/<id>.html`, `404.html`, `assets/style.css`, копию `photos/`. Дальше — обычный `git commit` и `git push` в репозиторий с включённым GitHub Pages. Удобно запускать по cron и пушить только при изменениях.

База работает в режиме WAL: свежие записи сначала попадают в `inventory.db-wal`. Если ваш скрипт пересборки решает «есть ли изменения» по времени изменения `inventory.db`, перед проверкой выполните `sqlite3 inventory.db "PRAGMA wal_checkpoint(TRUNCATE);"`.

## Данные в отдельном репозитории

`/export` выгружает базу в файлы внутри `INVENTORY_REPO_DIR`. При `INVENTORY_AUTO_GIT=1` бот после каждого применённого черновика делает `git add`, `git commit` и `git push` в этой папке. Для этого дайте серверу deploy key с правом записи только в репозиторий данных — не в репозиторий кода.

## Если бот молчит

1. `journalctl -u zavhoz-bot -n 50` — повторяющиеся `timed out` у `getUpdates` значат, что сервер не достаёт до Telegram: проверьте сеть и прокси.
2. В логе есть `message … user_id=…`, а ответа нет — этого id нет в `ALLOWED_TELEGRAM_USER_IDS`.
3. Ошибки `OpenAI HTTP 4xx` — ключ, модель или `OPENAI_BASE_URL`.
