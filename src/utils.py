from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from .config import BOT_USERNAME, READ_LIMIT_MINUTES

def start_keyboard(force=False):
    kb = []
    if force:
        kb.append([InlineKeyboardButton(text="✅ تایید عضویت", callback_data="check_sub")])
    kb.append([InlineKeyboardButton(text="➕ افزودن ربات به گروه", url=f"https://t.me/{BOT_USERNAME}?startgroup=true")])
    kb.append([InlineKeyboardButton(text="🆘 ارتباط با پشتیبان", url="https://t.me/SOULSOWNERBOT")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def whisper_button(token: str, again: bool = False):
    text = "🔒 نمایش مجدد" if again else "🔒 نمایش پیام"
    kb = [[InlineKeyboardButton(text=text, callback_data=f"open:{token}")]]
    return InlineKeyboardMarkup(inline_keyboard=kb)

TRIGGERS = {"درگوشی", "نجوا", "سکرت"}

def is_trigger(text: str) -> bool:
    if not text:
        return False
    t = text.strip().lower().replace("/", "")
    return t in TRIGGERS
