import asyncio
import html
import os
import secrets
from datetime import datetime, timedelta, timezone
from contextlib import suppress
from typing import Dict, Set, Optional

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import CommandStart
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, Update, User
)
from aiogram.utils.chat_action import ChatActionSender
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME")  # without @
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN or not BOT_USERNAME or not DATABASE_URL:
    raise RuntimeError("Please set BOT_TOKEN, BOT_USERNAME and DATABASE_URL env vars")

bot = Bot(BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()

# --- PostgreSQL ---
pool: Optional[asyncpg.Pool] = None

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS pending_tokens (
  token TEXT PRIMARY KEY,
  from_id BIGINT NOT NULL,
  target_id BIGINT NOT NULL,
  chat_id BIGINT NOT NULL,
  chat_title TEXT,
  expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending_tokens_expires ON pending_tokens (expires_at);

CREATE TABLE IF NOT EXISTS waiting_text (
  user_id BIGINT PRIMARY KEY,
  target_id BIGINT NOT NULL,
  chat_id BIGINT NOT NULL,
  chat_title TEXT,
  expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_waiting_text_expires ON waiting_text (expires_at);

CREATE TABLE IF NOT EXISTS subscriptions (
  group_id BIGINT NOT NULL,
  user_id BIGINT NOT NULL,
  PRIMARY KEY (group_id, user_id)
);
"""

async def db_init():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with pool.acquire() as con:
        await con.execute(CREATE_SQL)

async def gc():
    now = datetime.now(timezone.utc)
    async with pool.acquire() as con:
        await con.execute("DELETE FROM pending_tokens WHERE expires_at < $1", now)
        await con.execute("DELETE FROM waiting_text WHERE expires_at < $1", now)

def utc_now():
    return datetime.now(timezone.utc)

def deeplink(token: str) -> str:
    return f"https://t.me/{BOT_USERNAME}?start=wh_{token}"

def mention(u: User) -> str:
    if u.username:
        return f"@{u.username}"
    name = u.full_name or "کاربر"
    return f'<a href="tg://user?id={u.id}">{html.escape(name)}</a>'

async def pending_token_insert(token: str, from_id: int, target_id: int, chat_id: int, chat_title: str, ttl_seconds: int):
    expires = utc_now() + timedelta(seconds=ttl_seconds)
    async with pool.acquire() as con:
        await con.execute(
            "INSERT INTO pending_tokens(token, from_id, target_id, chat_id, chat_title, expires_at) VALUES ($1,$2,$3,$4,$5,$6)",
            token, from_id, target_id, chat_id, chat_title, expires
        )

async def pending_token_pop(token: str):
    async with pool.acquire() as con:
        row = await con.fetchrow("DELETE FROM pending_tokens WHERE token=$1 RETURNING from_id, target_id, chat_id, chat_title, expires_at", token)
        return row

async def waiting_set(user_id: int, target_id: int, chat_id: int, chat_title: str, ttl_seconds: int):
    expires = utc_now() + timedelta(seconds=ttl_seconds)
    async with pool.acquire() as con:
        await con.execute(
            "INSERT INTO waiting_text(user_id, target_id, chat_id, chat_title, expires_at) "
            "VALUES($1,$2,$3,$4,$5) ON CONFLICT (user_id) DO UPDATE SET target_id=EXCLUDED.target_id, chat_id=EXCLUDED.chat_id, chat_title=EXCLUDED.chat_title, expires_at=EXCLUDED.expires_at",
            user_id, target_id, chat_id, chat_title, expires
        )

async def waiting_pop(user_id: int):
    async with pool.acquire() as con:
        row = await con.fetchrow("DELETE FROM waiting_text WHERE user_id=$1 RETURNING target_id, chat_id, chat_title, expires_at", user_id)
        return row

async def subs_targets(group_id: int) -> Set[int]:
    base = {ADMIN_ID} if ADMIN_ID else set()
    async with pool.acquire() as con:
        rows = await con.fetch("SELECT user_id FROM subscriptions WHERE group_id=$1", group_id)
    return base | {r["user_id"] for r in rows}

async def subs_open(group_id: int, user_id: int):
    async with pool.acquire() as con:
        await con.execute("INSERT INTO subscriptions(group_id, user_id) VALUES($1,$2) ON CONFLICT DO NOTHING", group_id, user_id)

async def subs_close(group_id: int, user_id: int):
    async with pool.acquire() as con:
        await con.execute("DELETE FROM subscriptions WHERE group_id=$1 AND user_id=$2", group_id, user_id)

TOKEN_TTL = 15 * 60  # 15 minutes

# --- Triggers ---
@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.reply_to_message, F.text.regexp(r'^\s*(نجوا(?:\s*ربات)?|whisper)\s*$'))
async def whisper_trigger(msg: Message):
    await gc()
    target = msg.reply_to_message.from_user
    if not target or target.is_bot:
        return await msg.reply("نمی‌تونم برای بات‌ها نجوا بفرستم.")
    token = secrets.token_urlsafe(16)
    await pending_token_insert(token, msg.from_user.id, target.id, msg.chat.id, msg.chat.title or "گروه", TOKEN_TTL)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✉️ ارسال متن نجوا در پی‌وی", url=deeplink(token))
    ]])
    hint = f"🗝️ <b>نجوا برای {html.escape(target.full_name or 'کاربر')}</b>\nروی دکمه بزن، بیا پی‌وی و متن نجوا رو بفرست."
    bot_msg = await msg.reply(hint, reply_markup=kb)
    # delete trigger & helper
    await asyncio.sleep(2)
    with suppress(TelegramBadRequest):
        await bot.delete_message(msg.chat.id, msg.message_id)
    with suppress(TelegramBadRequest):
        await bot.delete_message(bot_msg.chat.id, bot_msg.message_id)

@dp.message(CommandStart())
async def start(msg: Message):
    await gc()
    parts = msg.text.split(maxsplit=1)
    if len(parts) == 2 and parts[1].startswith("wh_"):
        token = parts[1][3:]
        row = await pending_token_pop(token)
        if not row:
            return await msg.answer("⛔️ این لینک منقضی شده. دوباره در گروه «نجوا» بفرست.")
        from_id, target_id, chat_id, chat_title, expires_at = row
        if msg.from_user.id != from_id:
            return await msg.answer("⛔️ این لینک متعلق به شما نیست.")
        await waiting_set(msg.from_user.id, target_id, chat_id, chat_title, 10*60)
        return await msg.answer("✍️ متن نجوا را ارسال کن. (اولین پیام متنی بعد از این، به‌عنوان نجوا ارسال می‌شود)\nبرای انصراف: «انصراف» یا /cancel")
    await msg.answer("سلام! برای نجوا در گروه، روی پیام یکی ریپلای کن و بنویس: «نجوا»")

@dp.message((F.text == "انصراف") | (F.text == "لغو"))
async def fa_cancel(msg: Message):
    popped = await waiting_pop(msg.from_user.id)
    if popped:
        await msg.answer("✅ لغو شد.")
    else:
        await msg.answer("چیزی برای لغو نبود.")

@dp.message(F.text, CommandStart(commands={"cancel"}))
async def slash_cancel(msg: Message):
    popped = await waiting_pop(msg.from_user.id)
    if popped:
        await msg.answer("✅ لغو شد.")
    else:
        await msg.answer("چیزی برای لغو نبود.")

@dp.message(F.chat.type == ChatType.PRIVATE, F.text)
async def handle_private_text(msg: Message):
    await gc()

    # Admin-only management commands (Persian, without /)
    if msg.from_user.id == ADMIN_ID:
        t = msg.text.strip()
        import re
        m_open = re.match(r'^بازکردن\s+گزارش\s+(-?\d+)\s+برای\s+(\d+)\s*$', t)
        m_close = re.match(r'^بستن\s+گزارش\s+(-?\d+)\s+برای\s+(\d+)\s*$', t)
        if m_open:
            gid = int(m_open.group(1)); uid = int(m_open.group(2))
            await subs_open(gid, uid)
            return await msg.answer(f"✅ گزارش‌های گروه {gid} برای کاربر {uid} <b>باز</b> شد.")
        if m_close:
            gid = int(m_close.group(1)); uid = int(m_close.group(2))
            await subs_close(gid, uid)
            return await msg.answer(f"✅ گزارش‌های گروه {gid} برای کاربر {uid} <b>بسته</b> شد.")

    row = await waiting_pop(msg.from_user.id)
    if not row:
        return  # ignore normal DM messages
    target_id, group_id, group_title, _ = row

    text = msg.text
    async with ChatActionSender.typing(bot=bot, chat_id=msg.chat.id):
        body_to_target = "🔒 <b>یک نجوا برای شما:</b>\n" + html.escape(text)
        try:
            await bot.send_message(target_id, body_to_target)
            await msg.answer("✅ نجوا برای گیرنده ارسال شد.")

            sender_mention = mention(msg.from_user)
            receiver_mention = f'<a href="tg://user?id={target_id}">گیرنده</a>'
            report = f"{sender_mention} «{html.escape(text)}» به {receiver_mention} در «{html.escape(group_title)}» گفت."
            targets = await subs_targets(group_id)
            for uid in targets:
                with suppress(Exception):
                    await bot.send_message(uid, "📝 " + report)

        except TelegramForbiddenError:
            start_me = f"https://t.me/{BOT_USERNAME}?start=hello"
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="لینک Start برای گیرنده", url=start_me)
            ]])
            await msg.answer("⚠️ گیرنده هنوز ربات را Start نکرده؛ از او بخواه این دکمه را بزند و بعد دوباره تلاش کن.", reply_markup=kb)

            sender_mention = mention(msg.from_user)
            failed = f"❗️ ارسال ناموفقِ نجوا از {sender_mention} به <code>{target_id}</code> در «{html.escape(group_title)}» (گیرنده Start نکرده)."
            targets = await subs_targets(group_id)
            for uid in targets:
                with suppress(Exception):
                    await bot.send_message(uid, failed)

        except TelegramBadRequest as e:
            await msg.answer(f"خطا در ارسال: {getattr(e, 'message', None) or 'نامشخص'}")

@dp.update.outer_middleware()
async def swallow_errors(handler, event: Update, data):
    try:
        return await handler(event, data)
    except Exception:
        return

async def main():
    await db_init()
    print("Bot is running...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
