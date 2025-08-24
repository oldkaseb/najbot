import os

BOT_TOKEN = os.getenv("BOT_TOKEN")  # Telegram bot token
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # numeric Telegram user id
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/postgres")
FORCE_CHANNEL = os.getenv("FORCE_CHANNEL", "@SLSHEXED")  # username or numeric id of public channel to join
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "supersecret")
WEBHOOK_BASE = os.getenv("WEBHOOK_BASE", "")  # e.g. https://your-railway-app.up.railway.app
BOT_USERNAME = os.getenv("BOT_USERNAME", "DareGushi_BOT")
BOT_NAME_FA = os.getenv("BOT_NAME_FA", "درگوشی")

READ_LIMIT_MINUTES = int(os.getenv("PENDING_TIMEOUT_MINUTES", "5"))
