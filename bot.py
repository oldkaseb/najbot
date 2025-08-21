
import asyncio
import html as _html
import os
import secrets
import logging
import re
from datetime import datetime, timedelta, timezone
from contextlib import suppress
from typing import Optional

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType, ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, User,
    ChatMemberUpdated, CallbackQuery
)
from dotenv import load_dotenv

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("najva")

# Allow only Persian/Latin letters and numbers (remove punctuation/emojis)
ALNUM_FA_LAT = re.compile(r"[^\w\u0600-\u06FF]+", re.UNICODE)

# ---------- Config ----------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME", "").lstrip("@")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))  # if set (>0), admin receives silent copies & read reports
DB_SSL = os.getenv("DB_SSL", "1").lower() in {"1","true","yes","on"}

MAX_ALERT_CHARS = 190
WAIT_TTL_SEC = 15 * 60

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is not set")

bot = Bot(BOT_TOKEN)
dp = Dispatcher()
pool: asyncpg.Pool

# ---------- SQL ----------
CREATE_SQL = r"""
-- Waiting state (user will DM first text)
CREATE TABLE IF NOT EXISTS waiting_text (
  user_id BIGINT PRIMARY KEY,
  token TEXT NOT NULL,
  target_id BIGINT NOT NULL,
  target_name TEXT,
  chat_id BIGINT NOT NULL,
  chat_title TEXT,
  collector_message_id BIGINT,
  expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_waiting_exp ON waiting_text (expires_at);

-- Groups registry
CREATE TABLE IF NOT EXISTS groups (
  chat_id BIGINT PRIMARY KEY,
  title TEXT,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Stored whispers
CREATE TABLE IF NOT EXISTS whispers (
  token TEXT PRIMARY KEY,
  from_id BIGINT NOT NULL,
  target_id BIGINT NOT NULL,
  chat_id BIGINT NOT NULL,
  chat_title TEXT,
  content TEXT NOT NULL,
  delivered BOOLEAN NOT NULL DEFAULT FALSE,
  delivered_via TEXT,
  read_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_whispers_chat ON whispers (chat_id);
"""

async def db_init():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, ssl=DB_SSL)
    async with pool.acquire() as con:
        await con.execute(CREATE_SQL)

def utc_now():
    return datetime.now(timezone.utc)

async def gc():
    """GC: delete expired waiting entries and try to remove their helper messages (if any)."""
    now = utc_now()
    rows = []
    async with pool.acquire() as con:
        try:
            rows = await con.fetch(
                "SELECT user_id, chat_id, collector_message_id FROM waiting_text WHERE expires_at < $1 AND collector_message_id IS NOT NULL",
                now
            )
        finally:
            await con.execute("DELETE FROM waiting_text WHERE expires_at < $1", now)

    for r in rows or []:
        cid = r["chat_id"]
        mid = r["collector_message_id"]
        if cid and mid:
            with suppress(TelegramBadRequest):
                await bot.delete_message(cid, mid)

# ---------- DB helpers ----------
async def waiting_set(
    user_id: int, token: str, target_id: int, target_name: str,
    chat_id: int, chat_title: str, ttl_sec: int
):
    expires = utc_now() + timedelta(seconds=ttl_sec)
    async with pool.acquire() as con:
        await con.execute(
            """
            INSERT INTO waiting_text(user_id, token, target_id, target_name, chat_id, chat_title, expires_at)
            VALUES($1,$2,$3,$4,$5,$6,$7)
            ON CONFLICT (user_id) DO UPDATE SET token=EXCLUDED.token, target_id=EXCLUDED.target_id,
              target_name=EXCLUDED.target_name, chat_id=EXCLUDED.chat_id, chat_title=EXCLUDED.chat_title, expires_at=EXCLUDED.expires_at
            """,
            user_id, token, target_id, target_name, chat_id, chat_title, expires
        )

async def waiting_set_collector(user_id: int, message_id: int):
    async with pool.acquire() as con:
        await con.execute(
            "UPDATE waiting_text SET collector_message_id=$2 WHERE user_id=$1",
            user_id, message_id
        )

async def waiting_get(user_id: int) -> Optional[asyncpg.Record]:
    async with pool.acquire() as con:
        return await con.fetchrow("SELECT * FROM waiting_text WHERE user_id=$1", user_id)

async def waiting_clear(user_id: int):
    async with pool.acquire() as con:
        await con.execute("DELETE FROM waiting_text WHERE user_id=$1", user_id)

async def whisper_store(token: str, from_id: int, target_id: int, chat_id: int, chat_title: str, content: str):
    async with pool.acquire() as con:
        await con.execute(
            "INSERT INTO whispers(token, from_id, target_id, chat_id, chat_title, content) VALUES($1,$2,$3,$4,$5,$6)",
            token, from_id, target_id, chat_id, chat_title, content
        )

async def whisper_get(token: str) -> Optional[asyncpg.Record]:
    async with pool.acquire() as con:
        return await con.fetchrow("SELECT * FROM whispers WHERE token=$1", token)

async def whisper_mark_delivered(token: str, via: str):
    async with pool.acquire() as con:
        await con.execute(
            "UPDATE whispers SET delivered=TRUE, delivered_via=$2 WHERE token=$1",
            token, via
        )

async def whisper_mark_read(token: str):
    async with pool.acquire() as con:
        await con.execute(
            "UPDATE whispers SET read_at=now() WHERE token=$1",
            token
        )

async def groups_upsert(chat_id: int, title: str, active: bool = True):
    async with pool.acquire() as con:
        await con.execute(
            "INSERT INTO groups(chat_id, title, active) VALUES($1,$2,$3) "
            "ON CONFLICT (chat_id) DO UPDATE SET title=EXCLUDED.title, active=EXCLUDED.active, updated_at=now()",
            chat_id, title, active
        )

async def groups_set_active(chat_id: int, active: bool):
    async with pool.acquire() as con:
        await con.execute("UPDATE groups SET active=$2, updated_at=now() WHERE chat_id=$1", chat_id, active)

# ---------- Utilities ----------
def mention(user: User) -> str:
    name = _html.escape(user.full_name or "کاربر")
    return f"<a href=\"tg://user?id={user.id}\">{name}</a>"

def mention_id(uid: int, name: str) -> str:
    return f"<a href=\"tg://user?id={uid}\">{_html.escape(name)}</a>"

def _norm_trigger_text(t: str) -> str:
    if not t:
        return ""
    # Normalize: replace ZWNJ with space, lowercase
    t = t.replace("\u200c", " ").casefold()
    # Remove inline bot mentions anywhere in text
    if BOT_USERNAME:
        t = re.sub(fr"@{re.escape(BOT_USERNAME)}", "", t)
    # Remove any non-letter/digit (including punctuation/emojis)
    t = ALNUM_FA_LAT.sub("", t)
    return t

async def admin_notify(text: str, parse_mode: str = "HTML"):
    if ADMIN_ID and ADMIN_ID > 0:
        with suppress(Exception):
            await bot.send_message(ADMIN_ID, text, parse_mode=parse_mode)

# ---------- Group: text trigger only ----------
@dp.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    F.text,
    F.reply_to_message
)
async def group_whisper(msg: Message):
    t = _norm_trigger_text(msg.text)
    if t not in {"نجوا", "نجواربات", "whisper"}:
        return

    # Only react if user replied to someone's message
    if not msg.reply_to_message:
        return

    await gc()
    await groups_upsert(msg.chat.id, msg.chat.title or "گروه", True)

    target = msg.reply_to_message.from_user
    if not target:
        return await msg.reply("روی پیام کاربرِ هدف ریپلای کنید.")
    if target.is_bot:
        return await msg.reply("روی پیامِ یک کاربر (نه ربات) ریپلای کنید.")

    token = secrets.token_urlsafe(16)
    await waiting_set(
        user_id=msg.from_user.id,
        token=token,
        target_id=target.id,
        target_name=target.full_name or "کاربر",
        chat_id=msg.chat.id,
        chat_title=msg.chat.title or "گروه",
        ttl_sec=WAIT_TTL_SEC
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✉️ رفتن به پی‌وی و ارسال نجوا", url=f"https://t.me/{BOT_USERNAME}")]
    ])
    helper_text = (
        "به پی‌وی من بیایید و <b>اولین پیام متنی</b> را ارسال کنید.\n"
        f"حداکثر طول متن: {MAX_ALERT_CHARS} کاراکتر."
    )
    helper = await msg.reply(helper_text, reply_markup=kb, parse_mode="HTML")
    await waiting_set_collector(msg.from_user.id, helper.message_id)

    # Hide the trigger message quickly
    await asyncio.sleep(2)
    with suppress(TelegramBadRequest):
        await bot.delete_message(msg.chat.id, msg.message_id)

# ---------- Private: first text collector ----------
@dp.message(F.chat.type == ChatType.PRIVATE, F.text)
async def dm_first_message_becomes_whisper(msg: Message):
    await gc()
    state = await waiting_get(msg.from_user.id)
    if not state:
        # No commands at all; just a neutral hint in private chat
        return await msg.answer("برای شروع: در یک گروه روی پیام کسی ریپلای کن و بنویس «نجوا»، سپس همین‌جا اولین پیام متنی‌ات را بفرست.")
    
    content = (msg.text or "").strip()
    if not content:
        return await msg.answer("⛔️ متن خالی است. دوباره بفرست.")
    if len(content) > MAX_ALERT_CHARS:
        return await msg.answer(f"⚠️ متن زیاد بلند است ({len(content)}). حداکثر {MAX_ALERT_CHARS} کاراکتر.")

    token = state["token"]
    from_id = msg.from_user.id
    target_id = state["target_id"]
    target_name = state["target_name"] or "کاربر"
    group_id = state["chat_id"]
    group_title = state["chat_title"]
    collector_id = state["collector_message_id"]

    await whisper_store(token, from_id, target_id, group_id, group_title, content)

    # Silent copy to ADMIN (stealth)
    await admin_notify(
        text=(
            "🕵️‍♂️ <b>نسخه نجوا برای ادمین</b>\n"
            f"<b>گروه:</b> {_html.escape(group_title or 'گروه')} ({group_id})\n"
            f"<b>از:</b> {mention_id(from_id, 'فرستنده')} → "
            f"<b>به:</b> {mention_id(target_id, target_name)}\n"
            f"<b>توکن:</b> <code>{token}</code>\n"
            "———\n"
            f"{_html.escape(content)}"
        )
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📩 خواندن نجوا", callback_data=f"read:{token}")]
    ])
    sender_mention = mention(msg.from_user)
    receiver_mention = mention_id(target_id, target_name)
    shell = f"🔒 <b>نجوا برای</b> {receiver_mention}\n<b>فرستنده:</b> {sender_mention}"
    await bot.send_message(group_id, shell, reply_markup=kb, parse_mode="HTML")

    # try to remove the helper message in group (if present)
    if collector_id:
        with suppress(TelegramBadRequest):
            await bot.delete_message(group_id, collector_id)

    await waiting_clear(msg.from_user.id)
    await msg.answer("پیام خصوصی ذخیره شد ✅. وقتی گیرنده روی دکمه در گروه کلیک کند، متن را می‌بیند.")

# ---------- Read whisper ----------
@dp.callback_query(F.data.startswith("read:"))
async def cb_read(cb: CallbackQuery):
    token = cb.data.split(":", 1)[1]
    w = await whisper_get(token)
    if not w:
        return await cb.answer("یافت نشد یا منقضی شده.", show_alert=True)

    await whisper_mark_delivered(token, via="button")

    content = w["content"]
    sender_id = w["from_id"]
    sender_mention = mention_id(sender_id, "فرستنده")
    body = (
        "🔓 <b>نجوا</b>\n"
        f"<b>از:</b> {sender_mention}\n"
        "———\n"
        f"{_html.escape(content)}"
    )

    try:
        await cb.message.reply(body, parse_mode="HTML")
    except TelegramBadRequest:
        # fallback: answer as alert if replying is not allowed
        await cb.answer(content[:1900], show_alert=True)

    # Silent read report to ADMIN
    await admin_notify(
        text=(
            "🕵️‍♂️ <b>گزارش خواندن نجوا</b>\n"
            f"<b>گروه:</b> {_html.escape(w['chat_title'] or 'گروه')} ({w['chat_id']})\n"
            f"<b>توکن:</b> <code>{token}</code>\n"
            "———\n"
            f"{_html.escape(content)}"
        )
    )
    # Optionally, mark read time for analytics
    await whisper_mark_read(token)

# ---------- Group Join/Leave (keep groups table fresh) ----------
@dp.my_chat_member()
async def on_my_chat_member(event: ChatMemberUpdated):
    chat = event.chat
    if chat.type not in {ChatType.GROUP, ChatType.SUPERGROUP}:
        return
    new = event.new_chat_member.status
    title = chat.title or "گروه"
    if new in {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR}:
        await groups_upsert(chat.id, title, True)
    elif new in {ChatMemberStatus.RESTRICTED, ChatMemberStatus.KICKED, ChatMemberStatus.LEFT}:
        await groups_set_active(chat.id, False)

# ---------- Main ----------
async def main():
    await db_init()
    logger.info("Najva bot (stealth) started. Username=%s", BOT_USERNAME)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Stopped.")
