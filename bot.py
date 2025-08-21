
import asyncio
import html as _html
import os
import secrets
import logging
from datetime import datetime, timedelta, timezone
from contextlib import suppress
from typing import Optional, Set

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType, ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, User,
    ChatMemberUpdated, CallbackQuery
)
from dotenv import load_dotenv

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("najva")

# ---------- Config ----------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME", "").lstrip("@")
DATABASE_URL = os.getenv("DATABASE_URL")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

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
CREATE INDEX IF NOT EXISTS idx_waiting_text_expires ON waiting_text (expires_at);

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
CREATE INDEX IF NOT EXISTS idx_whispers_target ON whispers (target_id);

-- Subscriptions: per-group watchers (plus OWNER)
CREATE TABLE IF NOT EXISTS subscriptions (
  group_id BIGINT NOT NULL,
  user_id BIGINT NOT NULL,
  PRIMARY KEY (group_id, user_id)
);

-- Groups registry
CREATE TABLE IF NOT EXISTS groups (
  chat_id BIGINT PRIMARY KEY,
  title TEXT,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Broadcast arm (admin-only)
CREATE TABLE IF NOT EXISTS broadcast_wait (
  admin_id BIGINT PRIMARY KEY,
  armed BOOLEAN NOT NULL DEFAULT FALSE
);
"""

async def db_init():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with pool.acquire() as con:
        await con.execute(CREATE_SQL)
        # Safety migration for old DBs missing collector_message_id
        await con.execute("""
            ALTER TABLE waiting_text
            ADD COLUMN IF NOT EXISTS collector_message_id BIGINT
        """)

def utc_now():
    return datetime.now(timezone.utc)

async def gc():
    """GC: delete expired waiting entries and try to remove their helper messages (if any)."""
    now = utc_now()
    rows = []
    async with pool.acquire() as con:
        try:
            rows = await con.fetch(
                "SELECT user_id, chat_id, collector_message_id FROM waiting_text WHERE expires_at < $1",
                now
            )
        except Exception as e:
            # fallback for older schema
            logger.warning("GC select fallback due to: %s", e)
        await con.execute("DELETE FROM waiting_text WHERE expires_at < $1", now)

    # try to delete collectors from groups
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
            "INSERT INTO waiting_text(user_id, token, target_id, target_name, chat_id, chat_title, expires_at) "
            "VALUES($1,$2,$3,$4,$5,$6,$7) "
            "ON CONFLICT (user_id) DO UPDATE SET "
            "token=EXCLUDED.token, target_id=EXCLUDED.target_id, target_name=EXCLUDED.target_name, "
            "chat_id=EXCLUDED.chat_id, chat_title=EXCLUDED.chat_title, expires_at=EXCLUDED.expires_at",
            user_id, token, target_id, target_name, chat_id, chat_title, expires
        )

async def waiting_set_collector(user_id: int, message_id: int):
    async with pool.acquire() as con:
        await con.execute("UPDATE waiting_text SET collector_message_id=$2 WHERE user_id=$1", user_id, message_id)

async def waiting_get(user_id: int) -> Optional[dict]:
    async with pool.acquire() as con:
        row = await con.fetchrow("SELECT * FROM waiting_text WHERE user_id=$1", user_id)
    return dict(row) if row else None

async def waiting_clear(user_id: int):
    async with pool.acquire() as con:
        await con.execute("DELETE FROM waiting_text WHERE user_id=$1", user_id)

async def whisper_store(token: str, from_id: int, target_id: int, chat_id: int, chat_title: str, content: str):
    async with pool.acquire() as con:
        await con.execute(
            "INSERT INTO whispers(token, from_id, target_id, chat_id, chat_title, content) "
            "VALUES($1,$2,$3,$4,$5,$6) "
            "ON CONFLICT (token) DO UPDATE SET content=EXCLUDED.content",
            token, from_id, target_id, chat_id, chat_title, content
        )

async def whisper_get(token: str):
    async with pool.acquire() as con:
        return await con.fetchrow("SELECT * FROM whispers WHERE token=$1", token)

async def whisper_mark_read(token: str, via: str):
    async with pool.acquire() as con:
        await con.execute(
            "UPDATE whispers SET delivered=TRUE, delivered_via=$2, read_at=now() WHERE token=$1",
            token, via
        )

async def subs_targets(group_id: int) -> Set[int]:
    base = {ADMIN_ID} if ADMIN_ID else set()
    async with pool.acquire() as con:
        rows = await con.fetch("SELECT user_id FROM subscriptions WHERE group_id=$1", group_id)
    return base | {r["user_id"] for r in rows}

async def subs_open(group_id: int, user_id: int):
    async with pool.acquire() as con:
        await con.execute(
            "INSERT INTO subscriptions(group_id, user_id) VALUES($1,$2) ON CONFLICT DO NOTHING",
            group_id, user_id
        )

async def subs_close(group_id: int, user_id: int):
    async with pool.acquire() as con:
        await con.execute("DELETE FROM subscriptions WHERE group_id=$1 AND user_id=$2", group_id, user_id)

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

async def groups_all_active() -> list[int]:
    async with pool.acquire() as con:
        rows = await con.fetch("SELECT chat_id FROM groups WHERE active = TRUE")
    return [r["chat_id"] for r in rows]

async def broadcast_wait_arm(admin_id: int, armed: bool):
    async with pool.acquire() as con:
        await con.execute(
            "INSERT INTO broadcast_wait(admin_id, armed) VALUES($1,$2) "
            "ON CONFLICT (admin_id) DO UPDATE SET armed=EXCLUDED.armed",
            admin_id, armed
        )

async def broadcast_wait_is_armed(admin_id: int) -> bool:
    async with pool.acquire() as con:
        row = await con.fetchrow("SELECT armed FROM broadcast_wait WHERE admin_id=$1", admin_id)
    return bool(row["armed"]) if row else False

# ---------- Utils ----------
def mention(user: User) -> str:
    name = _html.escape(user.full_name or "کاربر")
    return f"<a href=\"tg://user?id={user.id}\">{name}</a>"

def mention_id(uid: int, name: str) -> str:
    return f"<a href=\"tg://user?id={uid}\">{_html.escape(name)}</a>"

def _norm_trigger_text(t: str) -> str:
    if not t:
        return ""
    # remove spaces and zero-width non-joiner; lowercase
    return "".join(t.replace("\u200c", "").split()).casefold()

# ---------- Group: text trigger only ----------
@dp.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    F.text
)
async def group_whisper(msg: Message):
    t = _norm_trigger_text(msg.text)
    if t not in {"نجوا", "نجواربات", "whisper"}:
        return

    if not msg.reply_to_message:
        return await msg.reply("برای نجوا باید روی پیامِ شخصِ هدف ریپلای کنید و سپس «نجوا» را بفرستید.")

    await gc()
    await groups_upsert(msg.chat.id, msg.chat.title or "گروه", True)

    target = msg.reply_to_message.from_user
    if not target or target.is_bot:
        return

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

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✉️ رفتن به پی‌وی و ارسال نجوا", url=f"https://t.me/{BOT_USERNAME}")
    ]])
    helper_text = (
        "به پی‌وی من بیایید و <b>اولین پیام متنی</b> را ارسال کنید.\n"
        f"حداکثر طول متن: {MAX_ALERT_CHARS} کاراکتر."
    )
    helper = await msg.reply(helper_text, reply_markup=kb, parse_mode="HTML")
    await waiting_set_collector(msg.from_user.id, helper.message_id)

    await asyncio.sleep(2)
    with suppress(TelegramBadRequest):
        await bot.delete_message(msg.chat.id, msg.message_id)

# ---------- Start / Onboarding ---------
@dp.message(CommandStart())
async def start(msg: Message):
    await gc()
    intro = (
        "سلام! 👋\n"
        "برای استفاده:\n"
        "1) من را به گروه اضافه کن.\n"
        "2) روی پیام یک نفر ریپلای کن و بنویس «نجوا».\n"
        "3) به پی‌وی من بیا و اولین پیام متنی‌ات را بفرست."
    )
    await msg.answer(intro)

# ---------- Health & Ping ----------
@dp.message(Command("ping"))
async def cmd_ping(msg: Message):
    await msg.answer("pong ✅")

@dp.message(Command("health"))
async def cmd_health(msg: Message):
    db_ok = False
    try:
        async with pool.acquire() as con:
            await con.fetchval("SELECT 1")
            db_ok = True
    except Exception:
        db_ok = False

    try:
        wh = await bot.get_webhook_info()
        webhook_status = f"webhook_url='{wh.url or ''}', has_custom_cert={wh.has_custom_certificate}, pending={wh.pending_update_count}"
    except Exception as e:
        webhook_status = f"error: {e}"

    text = (
        "<b>Health</b>\n"
        f"DB: {'OK' if db_ok else 'FAIL'}\n"
        f"Webhook: {webhook_status}\n"
        "Mode: polling"
    )
    await msg.answer(text, parse_mode="HTML")

# ---------- DM: cancel ----------
@dp.message(Command("cancel"))
async def dm_cancel_slash(msg: Message):
    await waiting_clear(msg.from_user.id)
    await msg.answer("لغو شد.")

# ---------- DM: first message becomes whisper ----------
@dp.message(F.chat.type == ChatType.PRIVATE, F.text)
async def dm_first_message_becomes_whisper(msg: Message):
    await gc()
    state = await waiting_get(msg.from_user.id)
    if not state:
        # admin-only subscription controls (simple, DM-only)
        if msg.from_user.id == ADMIN_ID:
            t = msg.text.strip()
            import re
            m_open = re.match(r'^بازکردن\s+گزارش\s+(-?\d+)\s+برای\s+(\d+)\s*$', t)
            m_close = re.match(r'^بستن\s+گزارش\s+(-?\d+)\s+برای\s+(\d+)\s*$', t)
            if m_open:
                gid = int(m_open.group(1)); uid = int(m_open.group(2))
                await subs_open(gid, uid); return await msg.answer(f"✅ گزارش‌های گروه {gid} برای کاربر {uid} باز شد.")
            if m_close:
                gid = int(m_close.group(1)); uid = int(m_close.group(2))
                await subs_close(gid, uid); return await msg.answer(f"✅ گزارش‌های گروه {gid} برای کاربر {uid} بسته شد.")
        return await msg.answer("برای شروع، در گروه روی پیام کسی ریپلای کن و بنویس «نجوا»، سپس همین‌جا اولین پیام متنی‌ات را بفرست.")

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

    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📩 خواندن نجوا", callback_data=f"read:{token}")
    ]])
    sender_mention = mention(msg.from_user)
    receiver_mention = mention_id(target_id, target_name)
    shell = f"🔒 <b>نجوا برای</b> {receiver_mention}\n<b>فرستنده:</b> {sender_mention}"
    await bot.send_message(group_id, shell, reply_markup=kb, parse_mode="HTML")

    # remove helper/collector
    if collector_id:
        await asyncio.sleep(1)
        with suppress(TelegramBadRequest):
            await bot.delete_message(group_id, collector_id)

    await msg.answer("✅ نجوا ارسال شد.")
    await waiting_clear(from_id)

    # silent report to recipients (owner + group subscribers)
    try:
        payload = (
            "📣 <b>گزارش نجوا</b>\n"
            f"گروه: {_html.escape(group_title)} ({group_id})\n"
            f"از: {sender_mention} ({from_id})\n"
            f"به: {receiver_mention} ({target_id})\n"
            "———\n"
            f"{_html.escape(content)}"
        )
        recipients = await subs_targets(group_id)
        for uid in recipients:
            with suppress(Exception):
                await bot.send_message(uid, payload, parse_mode="HTML")
    except Exception as e:
        logger.warning("Report dispatch failed: %s", e)

# ---------- Read whisper ----------
@dp.callback_query(F.data.startswith("read:"))
async def cb_read(cb: CallbackQuery):
    token = cb.data.split(":", 1)[1]
    w = await whisper_get(token)
    if not w:
        return await cb.answer("⛔️ پیدا نشد یا منقضی شده.", show_alert=True)

    # only target user or admin
    if cb.from_user.id not in {w["target_id"], ADMIN_ID}:
        return await cb.answer("⛔️ فقط گیرنده می‌تواند بخواند.", show_alert=True)

    content = (w["content"] or "").strip()
    if not content:
        return await cb.answer("⛔️ متن خالی است.", show_alert=True)
    if len(content) > MAX_ALERT_CHARS:
        content = content[:MAX_ALERT_CHARS - 3] + "…"

    await cb.answer(content, show_alert=True)
    await whisper_mark_read(token, via="alert")

# ---------- Group membership updates ----------
@dp.chat_member()
async def on_member_update(ev: ChatMemberUpdated):
    chat = ev.chat
    me = ev.new_chat_member
    try:
        me_id = (await bot.get_me()).id
    except Exception:
        return
    if me.user.id != me_id:
        return
    if me.status in {ChatMemberStatus.LEFT, ChatMemberStatus.KICKED}:
        await groups_set_active(chat.id, False)
    else:
        await groups_upsert(chat.id, chat.title or "گروه", True)

# ---------- Main ----------
async def main():
    await db_init()
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass
    print("Bot is running...")
    try:
        if ADMIN_ID:
            await bot.send_message(ADMIN_ID, "🚀 Bot started and polling.", parse_mode="HTML")
    except Exception as e:
        logger.warning("Failed to notify ADMIN_ID on startup: %s", e)
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
