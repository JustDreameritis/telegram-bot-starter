# Case Study: Telegram Bot Starter Kit

## Overview

Production-ready Telegram bot framework with async architecture, scheduled tasks, webhook support, and SQLite persistence. Designed for rapid deployment of interactive bots with admin controls, callback handlers, and comprehensive monitoring.

## Technical Implementation

### Architecture

```
telegram-bot-starter/
├── bot.py                  # Application bootstrap + polling loop
├── config.py               # Pydantic settings with validation
├── database.py             # Async SQLite with aiosqlite
├── scheduler.py            # APScheduler job configuration
├── monitoring.py           # FastAPI webhook server + health checks
└── handlers/
    ├── __init__.py         # Handler registration
    ├── commands.py         # User command handlers (/start, /help, etc.)
    ├── admin.py            # Admin-only commands
    └── callbacks.py        # Inline button callbacks
```

### Core Components

**Application Bootstrap (bot.py)**
- python-telegram-bot 20.x ApplicationBuilder pattern
- Async lifecycle hooks (post_init, post_shutdown)
- Graceful signal handling (SIGINT/SIGTERM)
- Global error handler with user notification
- Bot data injection for shared state

```python
def build_application(cfg: Config) -> Application:
    db = Database(cfg.database_path)

    application = (
        ApplicationBuilder()
        .token(cfg.bot_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    # Shared state for all handlers
    application.bot_data["config"] = cfg
    application.bot_data["db"] = db
    application.bot_data["start_time"] = datetime.now(timezone.utc)

    register_command_handlers(application)
    register_admin_handlers(application)
    register_callback_handlers(application)
    application.add_error_handler(error_handler)

    return application
```

**Webhook Server (monitoring.py)**
- FastAPI server running in daemon thread
- Webhook endpoint for external integrations
- Health check endpoint for container orchestration
- Alert counter and status metrics

```python
@app.post("/webhook/{secret}")
async def webhook(secret: str, request: Request):
    if secret != app.state.webhook_secret:
        raise HTTPException(status_code=403, detail="Invalid secret")

    data = await request.json()
    # Process webhook payload
    app.state.alert_count += 1
    return {"status": "received"}

@app.get("/health")
async def health():
    return {
        "status": "healthy",
        "uptime_seconds": (datetime.now() - app.state.start_time).total_seconds(),
        "alerts_processed": app.state.alert_count
    }
```

**Scheduler (scheduler.py)**
- APScheduler with async job execution
- Configurable job intervals
- Access to bot and database via bot_data
- Graceful shutdown on application exit

```python
def build_scheduler(application: Application) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()

    # Daily cleanup job
    scheduler.add_job(
        cleanup_old_sessions,
        trigger="cron",
        hour=3,
        kwargs={"bot_data": application.bot_data}
    )

    # Hourly stats collection
    scheduler.add_job(
        collect_stats,
        trigger="interval",
        hours=1,
        kwargs={"bot_data": application.bot_data}
    )

    return scheduler
```

**Database (database.py)**
- Async SQLite via aiosqlite
- Connection pooling with context manager
- Schema migrations on init
- Common CRUD operations

### Handler System

```python
# handlers/commands.py
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    user_id = update.effective_user.id

    await db.upsert_user(user_id, update.effective_user.username)

    keyboard = [[InlineKeyboardButton("Get Started", callback_data="onboard")]]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await update.message.reply_text(
        "Welcome! Tap the button below to begin.",
        reply_markup=reply_markup
    )

# handlers/admin.py
async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    cfg: Config = context.bot_data["config"]

    if update.effective_user.id not in cfg.admin_ids:
        await update.message.reply_text("Unauthorized")
        return

    message = " ".join(context.args)
    db: Database = context.bot_data["db"]
    users = await db.get_all_users()

    for user_id in users:
        await context.bot.send_message(user_id, message)
```

## Key Features

| Feature | Implementation |
|---------|----------------|
| Framework | python-telegram-bot 20.x (async) |
| Persistence | SQLite via aiosqlite |
| Scheduling | APScheduler (AsyncIOScheduler) |
| Webhooks | FastAPI in daemon thread |
| Commands | /start, /help, /settings, /admin |
| Callbacks | Inline button handlers |
| Monitoring | /health endpoint, alert counter |

## Configuration

```env
# Bot Settings
BOT_TOKEN=1234567890:ABCdefGHIjklMNOpqrsTUVwxyz
ADMIN_IDS=123456789,987654321

# Database
DATABASE_PATH=data/bot.db

# Webhook Server
WEBHOOK_PORT=8080
WEBHOOK_SECRET=your-secret-here

# Logging
LOG_LEVEL=INFO
```

## Deployment Options

### Polling Mode (Development)
```bash
python bot.py
```

### Webhook Mode (Production)
```bash
# Set webhook URL via Telegram API
curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://your-domain.com/webhook/<SECRET>"

# Run with webhook handler
python bot.py --mode webhook
```

### Docker
```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY . .
CMD ["python", "bot.py"]
```

## Technical Stats

- **Lines of Code**: ~2,341
- **Python Version**: 3.10+
- **Dependencies**: python-telegram-bot, aiosqlite, apscheduler, fastapi, uvicorn
- **Architecture**: Async/await throughout

## Extending the Bot

### Adding a Command
```python
# handlers/commands.py
async def my_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text("Command response")

# handlers/__init__.py
def register_command_handlers(application: Application) -> None:
    application.add_handler(CommandHandler("mycommand", my_command))
```

### Adding a Scheduled Job
```python
# scheduler.py
async def my_job(bot_data: dict) -> None:
    bot = bot_data["config"].bot
    await bot.send_message(chat_id=ADMIN_ID, text="Scheduled alert")

scheduler.add_job(my_job, trigger="interval", minutes=30, kwargs={"bot_data": app.bot_data})
```

---

**Author**: JustDreameritis
**Repository**: [github.com/JustDreameritis/telegram-bot-starter](https://github.com/JustDreameritis/telegram-bot-starter)
