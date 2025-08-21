import asyncio
import html
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
    Message, InlineKeyboardMarkup, InlineKeyboardButton, Update, User, ChatMemberUpdated, CallbackQuery
)
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv

# ---------- Logging ----------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("najva")

# ---------- Config ----------
load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME")  # without @
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN or not BOT_USERNAME or not DATABASE_URL:
    raise RuntimeError("Please set BOT_TOKEN, BOT_USERNAME and DATABASE_URL env vars")

MAX_ALERT_CHARS = 190           # Telegram alert limit is ~200; we use 190 to be safe
WAIT_TTL_SEC = 15 * 60          # Sender has 15 minutes to DM the text

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

# ---------- PostgreSQL ----------
pool: Optional[asyncpg.Pool] = None

CREATE_SQL = """-- Waiting state in DM per user
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
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  delivered BOOLEAN NOT NULL DEFAULT FALSE,
  delivered_via TEXT,
  read_at TIMESTAMPTZ
);

-- Report subscribers
CREATE TABLE IF NOT EXISTS subscriptions (
  group_id BIGINT NOT NULL,
  user_id BIGINT NOT NULL,
  PRIMARY KEY (group_id, user_id)
);

-- Track groups (for broadcast/logging)
CREATE TABLE IF NOT EXISTS groups (
  chat_id BIGINT PRIMARY KEY,
  title TEXT,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Admin broadcast arming
CREATE TABLE IF NOT EXISTS broadcast_wait (
  user_id BIGINT PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

async def db_init():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with pool.acquire() as con:
        await con.execute(CREATE_SQL)

def utc_now():
    return datetime.now(timezone.utc)

async def gc():
    now = utc_now()
    async with pool.acquire() as con:
        rows = await con.fetch("SELECT user_id, chat_id, collector_message_id FROM waiting_text WHERE expires_at < $1", now)
        for r in rows:
            if r["collector_message_id"]:
                with suppress(TelegramBadRequest):
                    await bot.delete_message(r["chat_id"], r["collector_message_id"])
        await con.execute("DELETE FROM waiting_text WHERE expires_at < $1", now)

# ---------- DB helpers ----------
async def waiting_set(user_id: int, token: str, target_id: int, target_name: str, chat_id: int, chat_title: str, ttl_sec: int):
    async with pool.acquire() as con:
        await con.execute(
            "INSERT INTO waiting_text(user_id, token, target_id, target_name, chat_id, chat_title, expires_at) "
            "VALUES($1,$2,$3,$4,$5,$6,$7) "
            "ON CONFLICT (user_id) DO UPDATE SET token=EXCLUDED.token, target_id=EXCLUDED.target_id, "
            "target_name=EXCLUDED.target_name, chat_id=EXCLUDED.chat_id, chat_title=EXCLUDED.chat_title, expires_at=EXCLUDED.expires_at",
            user_id, token, target_id, target_name, chat_id, chat_title, utc_now() + timedelta(seconds=ttl_sec)
        )

async def waiting_set_collector(user_id: int, msg_id: int):
    async with pool.acquire() as con:
        await con.execute("UPDATE waiting_text SET collector_message_id=$2 WHERE user_id=$1", user_id, msg_id)

async def waiting_get(user_id: int):
    async with pool.acquire() as con:
        return await con.fetchrow("SELECT * FROM waiting_text WHERE user_id=$1", user_id)

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
        await con.execute("UPDATE whispers SET delivered=TRUE, delivered_via=$2, read_at=now() WHERE token=$1", token, via)

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

async def broadcast_wait_set(user_id: int):
    async with pool.acquire() as con:
        await con.execute("INSERT INTO broadcast_wait(user_id) VALUES($1) ON CONFLICT (user_id) DO NOTHING", user_id)

async def broadcast_wait_pop(user_id: int) -> bool:
    async with pool.acquire() as con:
        row = await con.fetchrow("DELETE FROM broadcast_wait WHERE user_id=$1 RETURNING user_id", user_id)
    return bool(row)

async def broadcast_wait_exists(user_id: int) -> bool:
    async with pool.acquire() as con:
        row = await con.fetchrow("SELECT 1 FROM broadcast_wait WHERE user_id=$1", user_id)
    return bool(row)

def mention(u: User) -> str:
    if u.username:
        return f"@{u.username}"
    name = u.full_name or "کاربر"
    return f'<a href="tg://user?id={u.id}">{html.escape(name)}</a>'

def mention_id(uid: int, name: Optional[str] = None) -> str:
    label = html.escape(name) if name else "کاربر"
    return f'<a href="tg://user?id={uid}">{label}</a>'

# ---------- Group membership tracking ----------
@dp.my_chat_member()
async def track_group_membership(event: ChatMemberUpdated):
    chat = event.chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return
    new_status = event.new_chat_member.status
    if new_status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
        await groups_upsert(chat.id, chat.title or "گروه", True)
        with suppress(Exception):
            await bot.send_message(ADMIN_ID, f"➕ بات به گروه «{html.escape(chat.title or str(chat.id))}» اضافه شد. (chat_id: <code>{chat.id}</code>)")
    else:
        await groups_set_active(chat.id, False)
        with suppress(Exception):
            await bot.send_message(ADMIN_ID, f"➖ بات از گروه «{html.escape(chat.title or str(chat.id))}» حذف/غیرفعال شد. (chat_id: <code>{chat.id}</code>)")

# ---------- Trigger in group ----------
@dp.message(
    F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}),
    F.reply_to_message,
    F.text.regexp(r'^\s*(نجوا(?:\s*ربات)?|whisper)\s*$')
)
async def whisper_trigger(msg: Message):
    await gc()
    await groups_upsert(msg.chat.id, msg.chat.title or "گروه", True)

    target = msg.reply_to_message.from_user
    if not target or target.is_bot:
        return await msg.reply("نمی‌تونم برای بات‌ها نجوا بفرستم.")

    # set waiting state immediately
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
        InlineKeyboardButton(text="✍️ ارسال متن نجوا در پی‌وی", url=f"https://t.me/{BOT_USERNAME}?start")
    ]])
    helper_text = (
        "برای نوشتن متن نجوا به پی‌وی من بیایید و <b>اولین پیام متنی</b> را ارسال کنید.\n"
        f"حداکثر طول متن: {MAX_ALERT_CHARS} کاراکتر."
    )
    helper = await msg.reply(helper_text, reply_markup=kb)
    await waiting_set_collector(msg.from_user.id, helper.message_id)
    logger.info("whisper_trigger: set waiting for user=%s target=%s in chat=%s", msg.from_user.id, target.id, msg.chat.id)

    # delete trigger message
    await asyncio.sleep(2)
    with suppress(TelegramBadRequest):
        await bot.delete_message(msg.chat.id, msg.message_id)

# ---------- Start / Onboarding ----------
def start_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ افزودن به گروه", url=f"https://t.me/{BOT_USERNAME}?startgroup=true")]
    ])

@dp.message(CommandStart())
async def start(msg: Message):
    await gc()
    intro = f"""سلام! 👋
<b>ربات نجوا</b> — متن در پی‌وی جمع می‌شود، اما نجوا به‌صورت پیام در گروه ثبت می‌شود.

روش استفاده:
1) من را به گروه اضافه کن.
2) روی پیام یک نفر ریپلای بزن و بنویس «نجوا».
3) به پی‌وی من بیا و اولین پیام متنی‌ات را ارسال کن (حداکثر {MAX_ALERT_CHARS} کاراکتر)؛ همان نجوا می‌شود.
4) در گروه پیامی ایجاد می‌کنم: «نجوا برای … / فرستنده: …» و فقط گیرنده (و مالک ربات) می‌تواند متن را با دکمه ببیند.
"""
    await msg.answer(intro, reply_markup=start_kb())

# ---------- DM: first message becomes whisper ----------
@dp.message(F.chat.type == ChatType.PRIVATE, F.text)
async def dm_first_message_becomes_whisper(msg: Message):
    await gc()
    state = await waiting_get(msg.from_user.id)
    if not state:
        # admin report commands
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

    content = msg.text.strip()
    if not content:
        return await msg.answer("⛔️ متن خالی است. دوباره بفرست.")
    if len(content) > MAX_ALERT_CHARS:
        return await msg.answer(f"⚠️ متن زیاد بلند است ({len(content)}). لطفاً زیر {MAX_ALERT_CHARS} کاراکتر کوتاه کن.")

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
    await bot.send_message(group_id, shell, reply_markup=kb)

    if collector_id:
        with suppress(TelegramBadRequest):
            await bot.delete_message(group_id, collector_id)

    await msg.answer("✅ نجوا ثبت شد و در گروه قرار گرفت.")
    await waiting_clear(from_id)

    payload = (
        "📣 <b>گزارش نجوا</b>
"
        f"گروه: {html.escape(group_title)} ({group_id})
"
        f"از: {sender_mention} ({from_id})
"
        f"به: {receiver_mention} ({target_id})
"
        "———
"
        f"{html.escape(content)}"
    )
    recipients = await subs_targets(group_id)
    for uid in recipients:
        with suppress(Exception):
            await bot.send_message(uid, payload, parse_mode="HTML")
# ---------- Cancel ----------
@dp.message(F.chat.type == ChatType.PRIVATE, (F.text == "انصراف") | (F.text == "لغو"))
async def dm_cancel_fa(msg: Message):
    await waiting_clear(msg.from_user.id)
    await msg.answer("✅ لغو شد.")

@dp.message(Command("cancel"))
async def dm_cancel_slash(msg: Message):
    await waiting_clear(msg.from_user.id)
    await msg.answer("✅ لغو شد.")

# ---------- Read whisper (only target or ADMIN) ----------
@dp.callback_query(F.data.regexp(r'^read:(.+)$'))
async def cb_read(cq: CallbackQuery):
    token = cq.data.split(":", 1)[1]
    wr = await whisper_get(token)
    if not wr:
        return await cq.answer("⛔️ این نجوا منقضی شده.", show_alert=True)

    if cq.from_user.id not in (wr["target_id"], ADMIN_ID):
        return await cq.answer("⛔️ این نجوا برای شما نیست.", show_alert=True)

    content = wr["content"]
    txt = content if len(content) <= MAX_ALERT_CHARS else (content[:MAX_ALERT_CHARS] + "…")
    await cq.answer("🔒 " + txt, show_alert=True)
    await whisper_mark_read(token, "alert")

# ---------- Admin broadcast (optional) ----------
@dp.message(F.chat.type == ChatType.PRIVATE, F.text.casefold() == "ارسال جمعی")
async def admin_broadcast_arm(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return await msg.answer("⛔️ این قابلیت مخصوص مدیر است.")
    await broadcast_wait_set(msg.from_user.id)
    await msg.answer("📣 حالت <b>ارسال جمعی</b> فعال شد. پیام بعدی شما به همهٔ گروه‌های فعال فوروارد می‌شود. لغو: «انصراف».")

@dp.message(F.chat.type == ChatType.PRIVATE)
async def admin_or_help(msg: Message):
    if msg.from_user.id == ADMIN_ID and await broadcast_wait_exists(msg.from_user.id):
        await broadcast_wait_pop(msg.from_user.id)
        groups = await groups_all_active()
        if not groups:
            return await msg.answer("هیچ گروه فعالی ثبت نشده است.")
        ok, fail = 0, 0
        for gid in groups:
            try:
                await bot.forward_message(chat_id=gid, from_chat_id=msg.chat.id, message_id=msg.message_id)
                ok += 1
            except Exception:
                fail += 1
        return await msg.answer(f"✅ ارسال جمعی تمام شد. موفق: {ok} | ناموفق: {fail}")

# ---------- Error swallow ----------
@dp.update.outer_middleware()
async def swallow_errors(handler, event: Update, data):
    try:
        return await handler(event, data)
    except Exception as e:
        logger.exception("Unhandled error: %s", e)
        return

async def main():
    await db_init()
    # ensure webhook is off to avoid getUpdates conflict
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception:
        pass
    print("Bot is running...")
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())

if __name__ == "__main__":
    asyncio.run(main())
