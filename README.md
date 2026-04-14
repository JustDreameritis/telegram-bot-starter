# Telegram Bot Starter Kit

A production-ready Telegram bot template with command handlers, scheduled alerts, webhook monitoring, admin controls, and SQLite persistence. Clone, configure, and ship — no boilerplate to write.

---

## Features

- **Full async** — built on python-telegram-bot v20+ (asyncio throughout)
- **Command suite** — `/start`, `/help`, `/status`, `/subscribe`, `/unsubscribe`, `/settings`
- **Admin panel** — `/admin`, `/broadcast`, `/stats` gated behind `ADMIN_IDS`
- **Inline keyboards** — category selection, confirmation dialogs, settings toggles
- **Conversation handlers** — multi-step flows for settings updates and broadcasts
- **Scheduled jobs** — APScheduler: health checks, hourly reports, daily digest, custom alert intervals
- **Webhook receiver** — FastAPI + uvicorn server for real-time external alerts with HMAC / secret auth
- **SQLite persistence** — async aiosqlite: users, subscriptions, alert history, preferences
- **Config validation** — typed Config object, validates all required env vars on startup
- **Graceful shutdown** — closes DB, stops scheduler, cancels polls on SIGINT/SIGTERM
- **Structured logging** — no print statements; configurable log level

---

## Architecture

```
Telegram API
     |
     v
  Bot Core (bot.py)
     |
     +---> handlers/commands.py    User commands
     +---> handlers/admin.py       Admin commands (guarded)
     +---> handlers/callbacks.py   Inline keyboard callbacks
     |
     +---> scheduler.py            APScheduler jobs
     |         |
     |         +---> health check  (every 5 min)
     |         +---> hourly report (every 1 hr)
     |         +---> daily digest  (08:00 UTC)
     |         +---> alert check   (configurable)
     |
     +---> monitoring.py           FastAPI webhook server
               |
               +---> /webhook/{source}   Receive external alerts
               +---> /health             Liveness probe
               +---> /stats             Session stats
     |
     v
  database.py   SQLite via aiosqlite
     |
     +---> users table
     +---> alert_history table
```

---

## Quick Start

### 1. Create your bot

1. Open Telegram and message [@BotFather](https://t.me/BotFather)
2. Send `/newbot` and follow the prompts
3. Copy the **bot token** you receive

### 2. Get your Telegram user ID

Message [@userinfobot](https://t.me/userinfobot) — it replies with your numeric ID. This goes in `ADMIN_IDS`.

### 3. Clone and install

```bash
git clone https://github.com/JustDreameritis/telegram-bot-starter.git
cd telegram-bot-starter
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 4. Configure

```bash
cp .env.example .env
# Edit .env and fill in your values
```

Minimum required values:

```
TELEGRAM_BOT_TOKEN=1234567890:ABCdef...
ADMIN_IDS=your-telegram-id
```

### 5. Run

```bash
python bot.py
```

Open Telegram and send your bot `/start`.

---

## Command Reference

| Command | Description | Access |
|---------|-------------|--------|
| `/start` | Welcome screen with quick-action inline keyboard | All users |
| `/help` | Full command reference | All users |
| `/status` | Bot uptime, user count, subscriber count, last alert time | All users |
| `/subscribe` | Choose alert categories via inline keyboard | All users |
| `/unsubscribe` | Unsubscribe from all alerts (with confirmation) | All users |
| `/settings` | Update timezone, alert frequency, quiet hours | All users |
| `/admin` | Live dashboard: stats, recent alerts | Admins only |
| `/broadcast` | Send a message to all subscribers | Admins only |
| `/stats` | Detailed analytics: category breakdown, timezone distribution | Admins only |

---

## Webhook Integration

The bot runs a FastAPI server (default port 8443) that accepts inbound webhooks from any external service.

### Send a test webhook

```bash
curl -X POST http://localhost:8443/webhook/my-service \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-webhook-secret" \
  -d '{
    "title": "Price Alert",
    "message": "BTC crossed $100,000",
    "severity": "HIGH",
    "category": "Price Alerts"
  }'
```

### HMAC signature (GitHub-style)

For services that sign payloads with HMAC-SHA256 (GitHub, Stripe, Datadog), send:

```
X-Hub-Signature-256: sha256=<hmac-sha256-of-body-with-your-secret>
```

### Alert routing

The `category` field in the payload (or the source name in the URL) determines which subscribers receive the alert. Categories match the ones users choose during `/subscribe`:

- `News`
- `Price Alerts`
- `Market Updates`
- `System Status`

### Health check

```bash
curl http://localhost:8443/health
# {"status": "ok", "timestamp": "2025-01-01T12:00:00Z"}
```

---

## Customisation Guide

### Add a new command

1. Add a handler function to `handlers/commands.py`:

```python
async def my_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Hello from my new command!")
```

2. Register it in `register_command_handlers`:

```python
app.add_handler(CommandHandler("mycommand", my_command))
```

3. Add it to the help text in `HELP_TEXT`.

### Add a new alert source

1. POST to `/webhook/{source}` from your service
2. Add a routing rule in `monitoring.py → CATEGORY_MAP`
3. Customise `_format_alert()` to parse your payload schema

### Add a new scheduled job

In `scheduler.py → build_scheduler`:

```python
scheduler.add_job(
    my_async_job_function,
    trigger=IntervalTrigger(minutes=30),
    args=[bot, db],
    id="my_job",
)
```

### Add a new database table

Add a `CREATE TABLE IF NOT EXISTS` block inside `Database._create_tables()` in `database.py`, then add helper methods following the existing patterns.

---

## Project Structure

```
telegram-bot-starter/
├── bot.py                  Main entry point
├── config.py               Environment-based configuration
├── database.py             SQLite persistence (aiosqlite)
├── scheduler.py            APScheduler jobs
├── monitoring.py           FastAPI webhook server
├── handlers/
│   ├── __init__.py         Exports registration helpers
│   ├── commands.py         User-facing commands
│   ├── admin.py            Admin-only commands + guard decorator
│   └── callbacks.py        Inline keyboard callback router
├── docs/
│   └── SOW-template.md     Statement of Work for client proposals
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

---

## Deployment

### systemd (recommended for VPS)

```ini
# /etc/systemd/system/telegram-bot.service
[Unit]
Description=Telegram Bot Starter Kit
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/opt/telegram-bot-starter
EnvironmentFile=/opt/telegram-bot-starter/.env
ExecStart=/opt/telegram-bot-starter/venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable telegram-bot
sudo systemctl start telegram-bot
sudo journalctl -u telegram-bot -f
```

### Docker

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "bot.py"]
```

```bash
docker build -t telegram-bot-starter .
docker run -d --env-file .env --name mybot telegram-bot-starter
```

### PM2 (Node process manager, cross-platform)

```bash
pip install -r requirements.txt
pm2 start bot.py --interpreter python3 --name telegram-bot
pm2 save
pm2 startup
```

---

## Tech Stack

| Component | Library | Version |
|-----------|---------|---------|
| Telegram API | python-telegram-bot | >=20.7 |
| Webhook server | FastAPI + uvicorn | >=0.115.0 / >=0.30.0 |
| Scheduler | APScheduler | >=3.10.0 |
| Database | aiosqlite | >=0.20.0 |
| HTTP client | httpx | >=0.27.0 |
| Config | python-dotenv | >=1.0.0 |

---

## License

MIT — free to use in commercial projects.
