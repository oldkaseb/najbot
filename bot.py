
import asyncio
import os
import re
import uuid
import logging
from datetime import datetime, timedelta, timezone

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, ChatMemberUpdated
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.client.default import DefaultBotProperties
from aiogram.enums.parse_mode import ParseMode
import ssl
from html import escape

# -------------------- Logging --------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("najbot")

# -------------------- Config --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DB_SSL_MODE = os.getenv("DB_SSL_MODE", "require").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")
MAX_TEXT = int(os.getenv("MAX_ALERT_CHARS", "190"))
LOG_ALL_GROUP = os.getenv("LOG_ALL_GROUP", "0") in {"1","true","True","yes","YES"}

if not BOT_TOKEN:
    logger.error("BOT_TOKEN is required")
    raise SystemExit("BOT_TOKEN is required")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
BOT_USERNAME = ""  # filled later

def _ssl_ctx():
    if DB_SSL_MODE.lower() in {"disable", "false", "0", "off"}:
        return None
    return "require"

# -------------------- DB --------------------
CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS whispers(
    token TEXT PRIMARY KEY,
    chat_id BIGINT NOT NULL,
    chat_title TEXT,
    reply_to_message_id BIGINT,
    sender_id BIGINT NOT NULL,
    sender_name TEXT,
    target_id BIGINT NOT NULL,
    target_name TEXT,
    text TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at TIMESTAMPTZ
);
CREATE TABLE IF NOT EXISTS chats(
    chat_id BIGINT PRIMARY KEY,
    type TEXT,
    title TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS users(
    user_id BIGINT PRIMARY KEY,
    name TEXT,
    username TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

MIGRATIONS = [
    "ALTER TABLE IF EXISTS whispers ADD COLUMN IF NOT EXISTS token TEXT;",
    "ALTER TABLE IF EXISTS whispers ADD COLUMN IF NOT EXISTS chat_id BIGINT;",
    "ALTER TABLE IF EXISTS whispers ADD COLUMN IF NOT EXISTS chat_title TEXT;",
    "ALTER TABLE IF EXISTS whispers ADD COLUMN IF NOT EXISTS reply_to_message_id BIGINT;",
    "ALTER TABLE IF EXISTS whispers ADD COLUMN IF NOT EXISTS sender_id BIGINT;",
    "ALTER TABLE IF EXISTS whispers ADD COLUMN IF NOT EXISTS sender_name TEXT;",
    "ALTER TABLE IF EXISTS whispers ADD COLUMN IF NOT EXISTS target_id BIGINT;",
    "ALTER TABLE IF EXISTS whispers ADD COLUMN IF NOT EXISTS target_name TEXT;",
    "ALTER TABLE IF EXISTS whispers ADD COLUMN IF NOT EXISTS text TEXT;",
    "ALTER TABLE IF EXISTS whispers ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT now();",
    "ALTER TABLE IF EXISTS whispers ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;",
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='whispers_pkey') THEN ALTER TABLE whispers ADD PRIMARY KEY (token); END IF; END $$;",
]

CREATE_INDEXES = """
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='whispers' AND column_name='target_id') THEN
        CREATE INDEX IF NOT EXISTS idx_whispers_target ON whispers(target_id);
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='whispers' AND column_name='sender_id') THEN
        CREATE INDEX IF NOT EXISTS idx_whispers_sender ON whispers(sender_id);
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='whispers' AND column_name='expires_at') THEN
        CREATE INDEX IF NOT EXISTS idx_whispers_expires ON whispers(expires_at);
    END IF;
END $$;
"""

pool: asyncpg.Pool | None = None

async def db_init():
    global pool
    logger.info("Connecting to Postgres...")
    pool = await asyncpg.create_pool(DATABASE_URL, ssl=_ssl_ctx())
    async with pool.acquire() as con:
        await con.execute(CREATE_TABLE)
        for stmt in MIGRATIONS:
            await con.execute(stmt)
        await con.execute(CREATE_INDEXES)
    logger.info("DB ready.")

async def reg_chat(chat_id: int, chat_type: str, title: str | None = None):
    async with pool.acquire() as con:
        await con.execute(
            """INSERT INTO chats(chat_id, type, title)
               VALUES($1,$2,$3)
               ON CONFLICT (chat_id) DO UPDATE SET type=EXCLUDED.type, title=EXCLUDED.title, updated_at=now()""",
            chat_id, chat_type, title or ""
        )

async def reg_user(user_id: int, name: str, username: str | None):
    async with pool.acquire() as con:
        await con.execute(
            """INSERT INTO users(user_id, name, username)
               VALUES($1,$2,$3)
               ON CONFLICT (user_id) DO UPDATE SET name=EXCLUDED.name, username=EXCLUDED.username, updated_at=now()""",
            user_id, name, (username or "").lstrip("@")
        )

async def list_groups() -> list[int]:
    async with pool.acquire() as con:
        rows = await con.fetch("SELECT chat_id FROM chats WHERE type IN ('group','supergroup')")
    return [r["chat_id"] for r in rows]

async def list_users() -> list[int]:
    async with pool.acquire() as con:
        rows = await con.fetch("SELECT user_id FROM users")
    return [r["user_id"] for r in rows]

async def save_placeholder(chat_id: int, chat_title: str | None, reply_to_message_id: int | None,
                           sender_id: int, sender_name: str, target_id: int, target_name: str) -> str:
    token = uuid.uuid4().hex
    ex = datetime.now(timezone.utc) + timedelta(hours=2)
    async with pool.acquire() as con:
        await con.execute(
            """INSERT INTO whispers(token, chat_id, chat_title, reply_to_message_id,
                                    sender_id, sender_name, target_id, target_name, text, created_at, expires_at)
               VALUES($1,$2,$3,$4,$5,$6,$7,$8,NULL,now(),$9)
               ON CONFLICT (token) DO NOTHING""",
            token, chat_id, chat_title or "", reply_to_message_id,
            sender_id, sender_name, target_id, target_name, ex
        )
    return token

async def set_text_for_sender(sender_id: int, text: str) -> str | None:
    async with pool.acquire() as con:
        row = await con.fetchrow(
            "SELECT token FROM whispers WHERE sender_id=$1 AND text IS NULL ORDER BY created_at DESC LIMIT 1",
            sender_id
        )
        if not row:
            return None
        token = row["token"]
        await con.execute("UPDATE whispers SET text=$1 WHERE token=$2", text, token)
    return token

async def get_by_token(token: str):
    async with pool.acquire() as con:
        return await con.fetchrow("SELECT * FROM whispers WHERE token=$1", token)

# -------------------- Helpers --------------------
TRIGGERS = {"نجوا", "درگوشی", "سکرت", "whisper", "secret"}


def _unify_ar(text: str) -> str:
    mapping = {
        "\u0643": "ک",  # ك -> ک
        "\u0649": "ی",  # ى -> ی
        "\u064A": "ی",  # ي -> ی
        "\u06CC": "ی",  # ی -> ی (یکسان‌سازی)
        "\u200c": "",   # ZWNJ
        "\u200f": "",   # RLM
        "\u200e": "",   # LRM
        "\u0640": "",   # ـ
    }
    # convert escape codes to actual chars to be safe if author typed literals above
    mapping2 = {}
    for k, v in mapping.items():
        try:
            mapping2[k.encode('utf-8').decode('unicode_escape')] = v
        except Exception:
            mapping2[k] = v
    return "".join(mapping2.get(ch, ch) for ch in text)

@dp.message(F.chat.type == ChatType.PRIVATE, Command("start"))
async def start_pm(msg: Message):
    global BOT_USERNAME
    me = await bot.get_me()
    BOT_USERNAME = (me.username or "").lstrip("@")
    await reg_user(msg.from_user.id, short_name(msg.from_user), msg.from_user.username)

    intro = (
        "سلام! من «<b>درگوشی</b>» هستم.\n"
        "برای پیام محرمانه در گروه:\n"
        "۱) منو به گروه اضافه کن.\n"
        "۲) روی پیام طرف <b>ریپلای</b> کن و «نجوا/درگوشی/سکرت» بفرست.\n"
        "۳) اولین متن توی پی‌وی رو به عنوان نجوا ثبت می‌کنم.\n"
        "۴) فقط گیرنده/فرستنده/مالک می‌تونن متن رو با alert خصوصی ببینن.\n"
    )
    await msg.answer(intro, reply_markup=kb_add_to_group(BOT_USERNAME))
    logger.info("Handled /start for user=%s", msg.from_user.id)

@dp.my_chat_member()
async def bot_added(e: ChatMemberUpdated):
    chat = e.chat
    logger.info("my_chat_member: chat=%s type=%s", chat.id, chat.type)
    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        await reg_chat(chat.id, "group" if chat.type == ChatType.GROUP else "supergroup", chat.title)

@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def register_group_on_any_message(msg: Message):
    if LOG_ALL_GROUP:
        logger.info("Group msg seen chat=%s user=%s text=%r reply=%s", msg.chat.id, msg.from_user and msg.from_user.id, msg.text, bool(msg.reply_to_message))
    await reg_chat(msg.chat.id, "group" if msg.chat.type == ChatType.GROUP else "supergroup", msg.chat.title)

@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.text)
async def group_trigger(msg: Message):
    global BOT_USERNAME
    if not BOT_USERNAME:
        me = await bot.get_me()
        BOT_USERNAME = (me.username or "").lstrip("@")

    toks = _tokens(msg.text or "", BOT_USERNAME)
    matched = (len(toks) == 1 and toks[0] in NORMALIZED_TRIGGERS)
    if not matched:
        return

    if not msg.reply_to_message:
        try:
            await msg.reply("برای شروع نجوا باید روی پیامِ طرف مقابل <b>ریپلای</b> کنی و یکی از کلمات «نجوا/درگوشی/سکرت» را بفرستی.")
        except Exception:
            pass
        return

    target = msg.reply_to_message.from_user
    sender = msg.from_user
    if not target or not sender:
        return

    logger.info("Trigger matched in chat=%s by sender=%s -> target=%s", msg.chat.id, sender.id, target.id)

    await reg_user(sender.id, short_name(sender), sender.username)
    await reg_user(target.id, short_name(target), target.username)
    await reg_chat(msg.chat.id, "group" if msg.chat.type == ChatType.GROUP else "supergroup", msg.chat.title)

    token = await save_placeholder(
        chat_id=msg.chat.id,
        chat_title=msg.chat.title,
        reply_to_message_id=msg.reply_to_message.message_id,
        sender_id=sender.id,
        sender_name=short_name(sender),
        target_id=target.id,
        target_name=short_name(target),
    )

    helper = (
        f"نجوا برای {mention(target.id, short_name(target))} شروع شد.
"
        f"به پی‌وی من بیا و <b>اولین پیام</b> رو بفرست. (حداکثر {MAX_TEXT} کاراکتر)"
    )
    try:
        await msg.reply(helper, reply_markup=kb_dm(BOT_USERNAME))
    except Exception as e:
        logger.warning("Failed to reply helper in group: %s", e)
    try:
        await bot.send_message(
            chat_id=sender.id,
            text=(
                f"در گروه «{msg.chat.title}» یک نجوا برای {mention(target.id, short_name(target))} باز کردی.
"
                "اولین پیام متنی که اینجا بفرستی ثبت می‌شه."
            ),
        )
    except Exception as e:
        logger.warning("Failed to DM sender: %s", e)

@dp.message(F.chat.type == ChatType.PRIVATE, F.text)
async def collect_whisper(msg: Message):
    await reg_user(msg.from_user.id, short_name(msg.from_user), msg.from_user.username)

    text = (msg.text or "").strip()
    if not text:
        return
    if len(text) > MAX_TEXT:
        await msg.answer(f"متن طولانیه؛ حداکثر {MAX_TEXT} کاراکتر.")
        return

    token = await set_text_for_sender(msg.from_user.id, text)
    if not token:
        await msg.answer("نجوای فعالی پیدا نشد. ابتدا در گروه روی پیام طرف، «نجوا/درگوشی/سکرت» را ریپلای کن.")
        logger.info("No active placeholder for user=%s", msg.from_user.id)
        return

    row = await get_by_token(token)
    if not row:
        await msg.answer("خطای داخلی.")
        logger.error("Row for token=%s not found after setting text.", token)
        return

    caption = (
        f"نجوا برای {mention(row['target_id'], row['target_name'])} 🔒
"
        f"فرستنده: {mention(row['sender_id'], row['sender_name'])}"
    )
    try:
        await bot.send_message(
            chat_id=row["chat_id"],
            text=caption,
            reply_markup=kb_read(token),
            reply_to_message_id=row["reply_to_message_id"] or None
        )
        logger.info("Posted button in chat=%s token=%s", row["chat_id"], token)
    except Exception as e:
        logger.error("Failed to send button to group: %s", e)
        await msg.answer("نتوانستم پیام دکمه‌دار را در گروه ارسال کنم.")
        return

    await msg.answer("نجوا ثبت شد و دکمه در همان رشته‌ی گفت‌وگو ارسال شد.")

@dp.callback_query(F.data.startswith("read:"))
async def read_whisper(cb: CallbackQuery):
    token = cb.data.split(":", 1)[1]
    row = await get_by_token(token)
    if not row or not row["text"]:
        await cb.answer("این نجوا معتبر نیست یا منقضی شده.", show_alert=True)
        logger.info("Invalid/expired token read=%s by user=%s", token, cb.from_user.id)
        return

    uid = cb.from_user.id
    allowed = uid in {row["target_id"], row["sender_id"]}
    if ADMIN_ID:
        allowed = allowed or uid == ADMIN_ID

    if not allowed:
        await cb.answer("این نجوا مخصوص گیرنده/فرستنده است.", show_alert=True)
        logger.info("Unauthorized read attempt token=%s by user=%s", token, uid)
        return

    await cb.answer(row["text"], show_alert=True)
    logger.info("Alert shown token=%s to user=%s", token, uid)

# -------------------- Broadcast / Forward (Admin only) --------------------
def admin_only(func):
    async def wrapper(msg: Message, *a, **kw):
        if ADMIN_ID and msg.from_user and msg.from_user.id == ADMIN_ID:
            return await func(msg, *a, **kw)
        await msg.answer("فقط مالک اجازه‌ی این دستور را دارد.")
        logger.warning("Non-admin tried admin command: user=%s", msg.from_user and msg.from_user.id)
    return wrapper

@dp.message(Command("broadcast_groups"))
@admin_only
async def bc_groups(msg: Message):
    groups = await list_groups()
    logger.info("Broadcast to groups count=%d", len(groups))
    if not groups:
        await msg.answer("هیچ گروهی ثبت نشده.")
        return

    count = 0
    if msg.reply_to_message:
        for gid in groups:
            try:
                await bot.forward_message(chat_id=gid, from_chat_id=msg.chat.id, message_id=msg.reply_to_message.message_id)
                count += 1
            except Exception as e:
                logger.warning("Forward to group %s failed: %s", gid, e)
    else:
        parts = (msg.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await msg.answer("متن بعد از دستور بنویس یا روی یک پیام ریپلای کن.")
            return
        payload = parts[1]
        for gid in groups:
            try:
                await bot.send_message(gid, payload)
                count += 1
            except Exception as e:
                logger.warning("Send to group %s failed: %s", gid, e)
    await msg.answer(f"ارسال به {count} گروه انجام شد.")

@dp.message(Command("broadcast_users"))
@admin_only
async def bc_users(msg: Message):
    users = await list_users()
    logger.info("Broadcast to users count=%d", len(users))
    if not users:
        await msg.answer("هیچ کاربری ثبت نشده.")
        return

    count = 0
    if msg.reply_to_message:
        for uid in users:
            try:
                await bot.forward_message(chat_id=uid, from_chat_id=msg.chat.id, message_id=msg.reply_to_message.message_id)
                count += 1
            except Exception as e:
                logger.warning("Forward to user %s failed: %s", uid, e)
    else:
        parts = (msg.text or "").split(maxsplit=1)
        if len(parts) < 2:
            await msg.answer("متن بعد از دستور بنویس یا روی یک پیام ریپلای کن.")
            return
        payload = parts[1]
        for uid in users:
            try:
                await bot.send_message(uid, payload)
                count += 1
            except Exception as e:
                logger.warning("Send to user %s failed: %s", uid, e)
    await msg.answer(f"ارسال به {count} کاربر انجام شد.")

# -------------------- Cleanup & Main --------------------
async def janitor():
    while True:
        try:
            async with pool.acquire() as con:
                await con.execute("DELETE FROM whispers WHERE expires_at IS NOT NULL AND expires_at < now() - interval '1 hour'")
        except Exception as e:
            logger.warning("Janitor error: %s", e)
        await asyncio.sleep(1800)

async def main():
    logger.info("Starting najbot ...")
    await db_init()
    me = await bot.get_me()
    logger.info("Bot is @%s (id=%s)", (me.username or ""), me.id)
    asyncio.create_task(janitor())
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutting down...")


def mention(uid: int, name: str | None) -> str:
    safe = escape((name or "کاربر"), quote=False)
    return f'<a href="tg://user?id={uid}">{safe}</a>'
