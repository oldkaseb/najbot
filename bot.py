import asyncio
import html
import os
import secrets
from datetime import datetime, timedelta, timezone
from contextlib import suppress
from typing import Optional, Set

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType, ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, InlineKeyboardMarkup, InlineKeyboardButton, Update, User, ChatMemberUpdated
)
from aiogram.utils.chat_action import ChatActionSender
from aiogram.client.default import DefaultBotProperties
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
BOT_USERNAME = os.getenv("BOT_USERNAME")  # without @
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN or not BOT_USERNAME or not DATABASE_URL:
    raise RuntimeError("Please set BOT_TOKEN, BOT_USERNAME and DATABASE_URL env vars")

bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
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

CREATE TABLE IF NOT EXISTS groups (
  chat_id BIGINT PRIMARY KEY,
  title TEXT,
  active BOOLEAN NOT NULL DEFAULT TRUE,
  joined_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

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

# ------------ DB helpers ------------
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

TOKEN_TTL = 15 * 60  # 15 minutes

# ------------ Group tracking via my_chat_member updates ------------
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
    elif new_status in (ChatMemberStatus.KICKED, ChatMemberStatus.LEFT, ChatMemberStatus.RESTRICTED):
        await groups_set_active(chat.id, False)
        with suppress(Exception):
            await bot.send_message(ADMIN_ID, f"➖ بات از گروه «{html.escape(chat.title or str(chat.id))}» حذف/غیرفعال شد. (chat_id: <code>{chat.id}</code>)")

# ------------ Triggers ------------
@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.reply_to_message, F.text.regexp(r'^\s*(نجوا(?:\s*ربات)?|whisper)\s*$'))
async def whisper_trigger(msg: Message):
    await gc()
    await groups_upsert(msg.chat.id, msg.chat.title or "گروه", True)

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
    await asyncio.sleep(2)
    with suppress(TelegramBadRequest):
        await bot.delete_message(msg.chat.id, msg.message_id)
    with suppress(TelegramBadRequest):
        await bot.delete_message(bot_msg.chat.id, bot_msg.message_id)

# ------------ Onboarding & Start ------------
def start_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ افزودن به گروه", url=f"https://t.me/{BOT_USERNAME}?startgroup=true")]
    ])

@dp.message(CommandStart())
async def start(msg: Message):
    await gc()
    parts = msg.text.split(maxsplit=1)
    if len(parts) == 2 and parts[1].startswith("wh_"):
        token = parts[1][3:]
        row = await pending_token_pop(token)
        if not row:
            return await msg.answer("⛔️ این لینک منقضی شده. دوباره در گروه «نجوا» بفرست.", reply_markup=start_kb())
        from_id, target_id, chat_id, chat_title, _ = row
        if msg.from_user.id != from_id:
            return await msg.answer("⛔️ این لینک متعلق به شما نیست.", reply_markup=start_kb())
        await waiting_set(msg.from_user.id, target_id, chat_id, chat_title, 10*60)
        return await msg.answer("✍️ متن نجوا را ارسال کن. (اولین پیام متنی بعد از این، به‌عنوان نجوا ارسال می‌شود)\nبرای انصراف: «انصراف» یا /cancel")

    intro = (
        "سلام! 👋\n"
        "<b>ربات نجوا</b> برای ارسال پیام خصوصی (نجوا) در گروه‌هاست.\n\n"
        "چطور کار می‌کند؟\n"
        "1) من را به گروه اضافه کن (دکمهٔ زیر).\n"
        "2) در گروه، روی پیامِ یک نفر <b>ریپلای</b> بزن و بنویس <code>نجوا</code>.\n"
        "3) دکمه‌ای بهت می‌دهم تا بیایی پی‌وی و متن نجوا را بنویسی؛ من آن را خصوصی برای گیرنده می‌فرستم.\n\n"
        "گزارش: هر نجوا برای مدیر (و مشترک‌های آن گروه) گزارش می‌شود.\n\n"
        "ارسال جمعی (فقط ادمین): در پی‌وی بنویس <code>ارسال جمعی</code> و سپس پیام بعدی‌ات به همهٔ گروه‌های فعال فوروارد می‌شود. برای لغو بگو <code>انصراف</code>."
    )
    await msg.answer(intro, reply_markup=start_kb())

# cancel handlers
@dp.message(Command("cancel"))
async def slash_cancel(msg: Message):
    popped = await waiting_pop(msg.from_user.id)
    if popped:
        await msg.answer("✅ لغو شد.")
    else:
        await msg.answer("چیزی برای لغو نبود.")

@dp.message((F.text == "انصراف") | (F.text == "لغو"))
async def fa_cancel(msg: Message):
    popped1 = await waiting_pop(msg.from_user.id)
    popped2 = await broadcast_wait_pop(msg.from_user.id)
    if popped1 or popped2:
        await msg.answer("✅ لغو شد.")
    else:
        await msg.answer("چیزی برای لغو نبود.")

# Admin broadcast arming
@dp.message(F.chat.type == ChatType.PRIVATE, F.text.casefold() == "ارسال جمعی")
async def admin_broadcast_arm(msg: Message):
    if msg.from_user.id != ADMIN_ID:
        return await msg.answer("⛔️ این قابلیت مخصوص مدیر است.")
    await broadcast_wait_set(msg.from_user.id)
    await msg.answer("📣 حالت <b>ارسال جمعی</b> فعال شد.\nپیام بعدی شما (متن/عکس/ویدیو/فایل/ویس/...) به همهٔ گروه‌های فعال فوروارد می‌شود.\nبرای لغو: «انصراف».")

# Private router: admin commands / broadcast / whisper
@dp.message(F.chat.type == ChatType.PRIVATE)
async def private_router(msg: Message):
    await gc()

    # Admin subscription commands
    if msg.from_user.id == ADMIN_ID and msg.text:
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

    # Broadcast
    if msg.from_user.id == ADMIN_ID and await broadcast_wait_exists(msg.from_user.id):
        await broadcast_wait_pop(msg.from_user.id)
        groups = await groups_all_active()
        if not groups:
            return await msg.answer("هیچ گروه فعالی ثبت نشده است. ابتدا من را به گروه‌ها اضافه کن و یک‌بار تریگر «نجوا» را اجرا کن.")
        ok, fail = 0, 0
        for gid in groups:
            try:
                await bot.forward_message(chat_id=gid, from_chat_id=msg.chat.id, message_id=msg.message_id)
                ok += 1
            except Exception:
                fail += 1
        return await msg.answer(f"✅ ارسال جمعی تمام شد. موفق: {ok} | ناموفق: {fail}")

    # Whisper flow
    row = await waiting_pop(msg.from_user.id)
    if row:
        target_id, group_id, group_title, _ = row
        text = msg.text if msg.text else "(غیرمتنی)"
        async with ChatActionSender.typing(bot=bot, chat_id=msg.chat.id):
            try:
                if msg.text:
                    body_to_target = "🔒 <b>یک نجوا برای شما:</b>\n" + html.escape(msg.text)
                    await bot.send_message(target_id, body_to_target)
                else:
                    await bot.forward_message(chat_id=target_id, from_chat_id=msg.chat.id, message_id=msg.message_id)
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
        return

    await msg.answer("برای شروع، من را به گروه اضافه کن و روی پیام کسی ریپلای کن و بنویس «نجوا».\nادمین: در پی‌وی بنویس «ارسال جمعی» برای فوروارد پیام بعدی‌ات به همهٔ گروه‌ها.")

# ------------- Error swallow -------------
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
