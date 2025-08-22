
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
from html import escape

# -------------------- Logging --------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("najbot")

DEBUG = os.getenv("DEBUG", "0").strip() in {"1", "true", "yes", "on"}

# -------------------- Config --------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DB_SSL_MODE = os.getenv("DB_SSL_MODE", "require").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or "0")
MAX_TEXT = int(os.getenv("MAX_ALERT_CHARS", "190"))

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
CREATE TABLE IF NOT EXISTS subs(
    chat_id BIGINT NOT NULL,
    user_id BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY(chat_id, user_id)
);
"""

MIGRATIONS = [
    "ALTER TABLE IF EXISTS whispers ADD COLUMN IF NOT EXISTS token TEXT;",
    "DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='whispers_pkey') THEN ALTER TABLE whispers ADD PRIMARY KEY (token); END IF; END $$;",
    "CREATE TABLE IF NOT EXISTS subs(chat_id BIGINT NOT NULL, user_id BIGINT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now(), PRIMARY KEY(chat_id, user_id));"
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
    CREATE INDEX IF NOT EXISTS idx_subs_chat ON subs(chat_id);
END $$;
"""

pool = None

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

# -------------------- DB helpers --------------------
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

async def get_user_by_username(username: str):
    username = (username or "").lstrip("@").lower()
    if not username:
        return None
    async with pool.acquire() as con:
        return await con.fetchrow("SELECT user_id, name, username FROM users WHERE lower(username)=$1", username)

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

async def set_text_for_token(token: str, text: str) -> None:
    async with pool.acquire() as con:
        await con.execute("UPDATE whispers SET text=$1 WHERE token=$2", text, token)

async def get_by_token(token: str):
    async with pool.acquire() as con:
        return await con.fetchrow("SELECT * FROM whispers WHERE token=$1", token)

async def add_sub(chat_id: int, user_id: int):
    async with pool.acquire() as con:
        await con.execute("""INSERT INTO subs(chat_id,user_id) VALUES($1,$2)
                             ON CONFLICT (chat_id,user_id) DO NOTHING""", chat_id, user_id)

async def remove_sub(chat_id: int, user_id: int):
    async with pool.acquire() as con:
        await con.execute("DELETE FROM subs WHERE chat_id=$1 AND user_id=$2", chat_id, user_id)

async def list_subs(chat_id: int) -> list[int]:
    async with pool.acquire() as con:
        rows = await con.fetch("SELECT user_id FROM subs WHERE chat_id=$1", chat_id)
    return [r["user_id"] for r in rows]

# -------------------- Helpers --------------------
TRIGGERS = {"نجوا", "درگوشی", "سکرت", "whisper", "secret"}

def _unify_ar(text: str) -> str:
    mapping = {
        "\u0643": "ک",  "\u0649": "ی",  "\u064A": "ی",  "\u06CC": "ی",
        "\u200c": "",   "\u200f": "",   "\u200e": "",   "\u0640": "",
    }
    for k, v in list(mapping.items()):
        try:
            mapping[k.encode('utf-8').decode('unicode_escape')] = v
        except Exception:
            pass
    return "".join(mapping.get(ch, ch) for ch in text)

def _normalize(s: str) -> str:
    s = _unify_ar(s or "")
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s

NORMALIZED_TRIGGERS = {_normalize(t) for t in TRIGGERS}
TRIGGER_BOUNDARY = r"(^|[^0-9A-Za-z\u0600-\u06FF])"
TRIGGER_END = r"(?=$|[^0-9A-Za-z\u0600-\u06FF])"

def has_trigger(text: str) -> bool:
    norm = _normalize(text or "")
    for t in NORMALIZED_TRIGGERS:
        if re.search(TRIGGER_BOUNDARY + re.escape(t) + TRIGGER_END, norm):
            return True
    return False

def mention(uid: int, name: str | None) -> str:
    safe = escape((name or "کاربر"), quote=False)
    return f'<a href="tg://user?id={uid}">{safe}</a>'

def short_name(user) -> str:
    name = getattr(user, "full_name", None)
    if not name:
        first = getattr(user, "first_name", None) or ""
        last = getattr(user, "last_name", None) or ""
        name = (first + " " + last).strip()
    if not name:
        username = getattr(user, "username", None)
        if username:
            name = f"@{username}"
    if not name:
        name = "کاربر"
    return name[:64]

def kb_add_to_group(bot_user: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="افزودن به گروه", url=f"https://t.me/{bot_user}?startgroup=add")
    kb.adjust(1)
    return kb.as_markup()

def kb_dm(bot_user: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="ارسال متن در پی‌وی", url=f"https://t.me/{bot_user}")
    kb.adjust(1)
    return kb.as_markup()

def kb_read(token: str):
    kb = InlineKeyboardBuilder()
    kb.button(text="باز کردن نجوا 🔒", callback_data=f"read:{token}")
    kb.adjust(1)
    return kb.as_markup()

# -------------------- /start --------------------
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
        "۳) اولین متنِ پی‌وی به عنوان نجوا ثبت می‌شود.\n"
        "۴) فقط گیرنده و فرستنده می‌توانند نجوا را ببینند.\n"
        "\nروش سریع در خودِ گروه: @نام‌کاربری‌من + متن + منشنِ گیرنده."
    )
    await msg.answer(intro, reply_markup=kb_add_to_group(BOT_USERNAME))

# -------------------- Bot added / seen --------------------
@dp.my_chat_member()
async def bot_added(e: ChatMemberUpdated):
    chat = e.chat
    if chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
        await reg_chat(chat.id, "group" if chat.type == ChatType.GROUP else "supergroup", chat.title)

@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}))
async def register_group_on_any_message(msg: Message):
    await reg_chat(msg.chat.id, "group" if msg.chat.type == ChatType.GROUP else "supergroup", msg.chat.title)

# -------------------- Trigger via reply (نجوا/درگوشی/سکرت) --------------------
@dp.message(F.chat.type.in_({ChatType.GROUP, ChatType.SUPERGROUP}), F.text)
async def group_trigger_or_direct(msg: Message):
    global BOT_USERNAME
    if not BOT_USERNAME:
        me = await bot.get_me()
        BOT_USERNAME = (me.username or "").lstrip("@")

    txt_norm = _normalize(msg.text or "")
    mentions_bot = f"@{BOT_USERNAME.lower()}" in txt_norm if BOT_USERNAME else False

    # mode A: reply trigger
    if msg.reply_to_message and has_trigger(msg.text):
        await handle_reply_trigger(msg)
        return

    # mode B: classic in-group
    if mentions_bot:
        await handle_classic_whisper(msg)
        return

async def handle_reply_trigger(msg: Message):
    target = msg.reply_to_message.from_user
    sender = msg.from_user
    if not target or not sender:
        return

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
        f"نجوا برای {mention(target.id, short_name(target))} شروع شد.\n"
        f"به پی‌وی من بیا و <b>اولین پیام</b> را بفرست. (حداکثر {MAX_TEXT} کاراکتر)"
    )
    # Always DM sender so اگر گروپ اجازه‌ی ارسال نداد، کار متوقف نشود
    try:
        await bot.send_message(
            chat_id=sender.id,
            text=(
                f"در گروه «{msg.chat.title}» یک نجوا برای {mention(target.id, short_name(target))} باز کردی.\n"
                "اولین پیام متنی که اینجا بفرستی ثبت می‌شود."
            ),
        )
    except Exception as e:
        logger.warning("Cannot PM sender: %s", e)

    try:
        me_user = (await bot.get_me()).username
        await msg.reply(helper, reply_markup=kb_dm(me_user))
    except Exception as e:
        if DEBUG and ADMIN_ID:
            await safe_dm(ADMIN_ID, f"[DBG] reply_trigger: cannot reply in group {msg.chat.id} → {e}")

async def handle_classic_whisper(msg: Message):
    sender = msg.from_user
    await reg_user(sender.id, short_name(sender), sender.username)
    await reg_chat(msg.chat.id, "group" if msg.chat.type == ChatType.GROUP else "supergroup", msg.chat.title)

    # target: text_mention > @username (db) > reply
    target_id = None
    target_name = None
    if msg.entities:
        for ent in msg.entities:
            try:
                if ent.type == "text_mention" and getattr(ent, "user", None):
                    u = ent.user
                    if u and u.id != (await bot.get_me()).id:
                        target_id, target_name = u.id, short_name(u)
                        break
                if ent.type == "mention":
                    raw = msg.text or ""
                    uname = raw[ent.offset: ent.offset + ent.length].lstrip("@")
                    row = await get_user_by_username(uname)
                    if row:
                        target_id, target_name = int(row["user_id"]), row["name"]
                        break
            except Exception as e:
                logger.debug("Entity parse err: %s", e)

    if (not target_id) and msg.reply_to_message and msg.reply_to_message.from_user:
        u = msg.reply_to_message.from_user
        target_id, target_name = u.id, short_name(u)

    if not target_id:
        try:
            await msg.reply("گیرنده را با ریپلای یا منشنِ قابل کلیک انتخاب کن.", allow_sending_without_reply=True)
        except Exception as e:
            if DEBUG and ADMIN_ID:
                await safe_dm(ADMIN_ID, f"[DBG] classic_whisper: cannot hint in group {msg.chat.id} → {e}")
        return

    # extract message text
    raw = msg.text or ""
    me_user = (await bot.get_me()).username
    raw = re.sub(rf"@{re.escape(me_user)}", "", raw, flags=re.I)
    if msg.entities:
        for ent in sorted([e for e in msg.entities if e.type == "mention"], key=lambda x: -x.offset):
            raw = raw[:ent.offset] + raw[ent.offset + ent.length:]
    text = raw.strip()
    if not text:
        return
    if len(text) > MAX_TEXT:
        text = text[:MAX_TEXT]

    token = await save_placeholder(
        chat_id=msg.chat.id,
        chat_title=msg.chat.title,
        reply_to_message_id=msg.message_id,
        sender_id=sender.id,
        sender_name=short_name(sender),
        target_id=target_id,
        target_name=target_name,
    )
    await set_text_for_token(token, text)

    caption = (
        f"نجوا برای {mention(target_id, target_name)} 🔒\n"
        f"فرستنده: {mention(sender.id, short_name(sender))}"
    )
    try:
        await bot.send_message(
            chat_id=msg.chat.id,
            text=caption,
            reply_markup=kb_read(token),
            reply_to_message_id=msg.message_id
        )
    except Exception as e:
        if DEBUG and ADMIN_ID:
            await safe_dm(ADMIN_ID, f"[DBG] classic_whisper: cannot send button in group {msg.chat.id} → {e}")
    try:
        await bot.send_message(chat_id=sender.id, text="نجوا ثبت شد.")
    except Exception:
        pass
    await silent_report(token)

# -------------------- Collect whisper in PM --------------------
@dp.message(F.chat.type == ChatType.PRIVATE, F.text)
async def collect_or_admin(msg: Message):
    # Admin text-commands (no slash)
    if ADMIN_ID and msg.from_user and msg.from_user.id == ADMIN_ID:
        if msg.reply_to_message and _normalize(msg.text).startswith("به همه گروه‌ها بفرست"):
            await owner_forward_to_groups(msg)
            return
        if msg.reply_to_message and _normalize(msg.text).startswith("به همه کاربرها بفرست"):
            await owner_forward_to_users(msg)
            return
        m = re.match(r"^ارسال به گروه‌ها:\s*(.+)$", msg.text.strip(), flags=re.S)
        if m:
            await owner_send_to_groups(m.group(1), msg)
            return
        m = re.match(r"^ارسال به کاربرها:\s*(.+)$", msg.text.strip(), flags=re.S)
        if m:
            await owner_send_to_users(m.group(1), msg)
            return
        m = re.match(r"^بازکردن گزارش\s*\((\-?\d+)\)\s*برای\s*\((\d+)\)\s*$", _normalize(msg.text))
        if m:
            await add_sub(int(m.group(1)), int(m.group(2)))
            await msg.answer("گزارش فعال شد.")
            return
        m = re.match(r"^بستن گزارش\s*\((\-?\d+)\)\s*برای\s*\((\d+)\)\s*$", _normalize(msg.text))
        if m:
            await remove_sub(int(m.group(1)), int(m.group(2)))
            await msg.answer("گزارش غیرفعال شد.")
            return
        if _normalize(msg.text) == "لیست گزارش‌ها":
            rows = []
            async with pool.acquire() as con:
                rows = await con.fetch("SELECT chat_id,user_id FROM subs ORDER BY chat_id,user_id")
            if not rows:
                await msg.answer("لیستی وجود ندارد.")
            else:
                out = "\n".join([f"گروه {r['chat_id']} → کاربر {r['user_id']}"] for r in rows)
                await msg.answer(out)
            return

    # Normal PM whisper text collection
    await reg_user(msg.from_user.id, short_name(msg.from_user), msg.from_user.username)

    text = (msg.text or "").strip()
    if not text:
        return
    if len(text) > MAX_TEXT:
        await msg.answer(f"متن طولانی است؛ حداکثر {MAX_TEXT} کاراکتر.")
        return

    token = await set_text_for_sender(msg.from_user.id, text)
    if not token:
        await msg.answer("نجوای فعالی پیدا نشد. در گروه روی پیام طرف، «نجوا/درگوشی/سکرت» را ریپلای کن.")
        return

    row = await get_by_token(token)
    if not row:
        await msg.answer("خطای داخلی.")
        return

    caption = (
        f"نجوا برای {mention(row['target_id'], row['target_name'])} 🔒\n"
        f"فرستنده: {mention(row['sender_id'], row['sender_name'])}"
    )
    try:
        await bot.send_message(
            chat_id=row["chat_id"],
            text=caption,
            reply_markup=kb_read(token),
            reply_to_message_id=row["reply_to_message_id"] or None
        )
    except Exception as e:
        await msg.answer("ارسال دکمه در گروه ممکن نشد (مجوز ارسال پیام را چک کن).")
        if DEBUG and ADMIN_ID:
            await safe_dm(ADMIN_ID, f"[DBG] PM collect: cannot send to group {row['chat_id']} → {e}")
        return

    await msg.answer("نجوا ثبت شد و دکمه در همان رشته‌ی گفت‌وگو ارسال شد.")
    await silent_report(token)

# -------------------- Read whisper via alert --------------------
@dp.callback_query(F.data.startswith("read:"))
async def read_whisper(cb: CallbackQuery):
    token = cb.data.split(":", 1)[1]
    row = await get_by_token(token)
    if not row or not row["text"]:
        await cb.answer("این نجوا معتبر نیست یا منقضی شده.", show_alert=True)
        return

    uid = cb.from_user.id
    allowed = uid in {row["target_id"], row["sender_id"]}
    if ADMIN_ID:
        allowed = allowed or uid == ADMIN_ID

    if not allowed:
        await cb.answer("این نجوا مخصوص گیرنده/فرستنده است.", show_alert=True)
        return

    await cb.answer(row["text"], show_alert=True)

# -------------------- Silent reports to owner & subscribers --------------------
async def silent_report(token: str):
    row = await get_by_token(token)
    if not row or not row["text"]:
        return
    txt = f"«{row['chat_title'] or row['chat_id']}»\n{row['sender_name']} → {row['target_name']}\n— {row['text']}"
    if ADMIN_ID:
        try:
            await bot.send_message(ADMIN_ID, txt)
        except Exception:
            pass
    subs_users = await list_subs(row["chat_id"])
    for uid in subs_users:
        if ADMIN_ID and uid == ADMIN_ID:
            continue
        try:
            await bot.send_message(uid, txt)
        except Exception:
            pass

# -------------------- Owner utilities --------------------
def admin_only(func):
    async def wrapper(msg: Message, *a, **kw):
        if ADMIN_ID and msg.from_user and msg.from_user.id == ADMIN_ID:
            return await func(msg, *a, **kw)
        await msg.answer("فقط مالک اجازه‌ی این دستور را دارد.")
    return wrapper

@dp.message(Command("broadcast_groups"))
@admin_only
async def bc_groups(msg: Message):
    await owner_forward_to_groups(msg)

@dp.message(Command("broadcast_users"))
@admin_only
async def bc_users(msg: Message):
    await owner_forward_to_users(msg)

async def owner_forward_to_groups(msg: Message):
    groups = await list_groups()
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
        await msg.answer("روی یک پیام ریپلای کن.")
        return
    await msg.answer(f"ارسال به {count} گروه انجام شد.")

async def owner_forward_to_users(msg: Message):
    users = await list_users()
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
        await msg.answer("روی یک پیام ریپلای کن.")
        return
    await msg.answer(f"ارسال به {count} کاربر انجام شد.")

async def owner_send_to_groups(payload: str, msg: Message):
    groups = await list_groups()
    if not groups:
        await msg.answer("هیچ گروهی ثبت نشده.")
        return
    count = 0
    for gid in groups:
        try:
            await bot.send_message(gid, payload)
            count += 1
        except Exception as e:
            logger.warning("Send to group %s failed: %s", gid, e)
    await msg.answer(f"ارسال به {count} گروه انجام شد.")

async def owner_send_to_users(payload: str, msg: Message):
    users = await list_users()
    if not users:
        await msg.answer("هیچ کاربری ثبت نشده.")
        return
    count = 0
    for uid in users:
        try:
            await bot.send_message(uid, payload)
            count += 1
        except Exception as e:
            logger.warning("Send to user %s failed: %s", uid, e)
    await msg.answer(f"ارسال به {count} کاربر انجام شد.")

async def safe_dm(uid: int, text: str):
    try:
        await bot.send_message(uid, text)
    except Exception:
        pass

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
