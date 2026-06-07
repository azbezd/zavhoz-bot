# Завхоз Bot

Telegram AI-ассистент для ведения личного склада электроники: детали, модули, инструменты, провода, мануалы, фото, проекты и статусы использования.

Проект ведётся по-русски. Технические идентификаторы остаются латиницей, чтобы Git, YAML и интеграции работали предсказуемо.

## Что умеет

- Принимать текст и фото из Telegram.
- Разбирать покупки, поставки, остатки и использование деталей через OpenAI API.
- Создавать черновик изменений, а не менять склад молча.
- Вносить изменения только после `/apply <id>`.
- Хранить рабочее состояние в SQLite.
- Экспортировать данные склада в отдельный GitHub-репозиторий `zavhoz-inventory`.
- Вести краткий список проектов: FreeNet, FreeNetBox, NetBox, Ideas Lab и будущие проекты.

## Принцип безопасности

- Секреты не хранятся в Git.
- Telegram token, OpenAI key и GitHub deploy keys лежат только на сервере.
- `INVENTORY_AUTO_GIT=0` по умолчанию, чтобы не пушить в GitHub до явного включения.
- Любое изменение склада сначала становится черновиком.

## Команды

- `/start` — приветствие и краткая справка.
- `/list` — полный список склада.
- `/projects` — список проектов.
- `/pending` — активные черновики.
- `/show <id>` — показать черновик.
- `/apply <id>` — применить черновик.
- `/discard <id>` — удалить черновик.
- `/export` — экспортировать текущую базу в файлы.
- `/style <text>` — запомнить стиль общения, например `/style коротко, по-русски, без канцелярита`.

## Переменные окружения

Файл на сервере: `/etc/freenet-inventory-bot.env`, права `0600`.

```bash
TELEGRAM_BOT_TOKEN=replace-me
OPENAI_API_KEY=replace-me
OPENAI_BASE_URL=https://api.openai.com/v1
OPENAI_MODEL=gpt-5.4-mini
INVENTORY_ENABLE_WEB_SEARCH=1
ALLOWED_TELEGRAM_USER_IDS=123456789
INVENTORY_REPO_DIR=/opt/zavhoz/zavhoz-inventory
INVENTORY_DB=/opt/zavhoz/inventory.db
INVENTORY_AUTO_GIT=0
```

## Systemd

```bash
cp inventory_bot/systemd/freenet-inventory-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now freenet-inventory-bot.service
journalctl -u freenet-inventory-bot.service -f
```

## GitHub-хранение

Код бота живёт в `zavhoz-bot`.

Данные склада живут отдельно в `zavhoz-inventory`. При включённом `INVENTORY_AUTO_GIT=1` бот после `/apply` делает:

```bash
git add .
git commit -m "inventory: apply telegram proposal <id>"
git push
```

Для этого на сервере нужен deploy key с write-доступом только к `zavhoz-inventory`.

