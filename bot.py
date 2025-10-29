# -*- coding: utf-8 -*-
"""
Shyraq Translator Bot — автоматты аудару + жазылым тексеру
"""

import os
import asyncio
import uuid
import logging
from typing import Optional
import re

import aiosqlite
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram import F
from typing import List, Tuple
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    InlineQuery, InputTextMessageContent, InlineQueryResultArticle
)
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.filters.state import State, StatesGroup
from aiogram.utils.keyboard import InlineKeyboardBuilder, InlineKeyboardButton
from aiogram.client.bot import DefaultBotProperties
from aiogram import Router

# ---------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN орта айнымалысы орнатылмаған. export BOT_TOKEN='...'")

DB_PATH = os.getenv("SHYRAQ_DB", "shyraq_bot.db")

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
router = Router()

# ---------------------
class ConvertStates(StatesGroup):
    direction = State()  # Біз тек бағыт сақтау үшін
    waiting_for_text = State()
# ---------------------
cyrillic_to_shyraq = {
    "А":"A","а":"a","Ә":"Á","ә":"á","Б":"B","б":"b","В":"V","в":"v",
    "Г":"G","г":"g","Ғ":"Ǵ","ғ":"ǵ","Д":"D","д":"d","Е":"E","е":"e",
    "Ё":"É","ё":"é","Ж":"ZH","ж":"zh","З":"Z","з":"z","И":"Í","и":"í",
    "Й":"Ì","й":"ì","К":"K","к":"k","Қ":"Q","қ":"q","Л":"L","л":"l",
    "М":"M","м":"m","Н":"N","н":"n","Ң":"Ŋ","ң":"ŋ","О":"O","о":"o",
    "Ө":"Ó","ө":"ó","П":"P","п":"p","Р":"R","р":"r","С":"S","с":"s",
    "Т":"T","т":"t","У":"Ý","у":"ý","Ұ":"U","ұ":"u","Ү":"Ú","ү":"ú",
    "Ф":"F","ф":"f","Х":"KH","х":"kh","Һ":"H","һ":"h","Ц":"C","ц":"c",
    "Ч":"CH","ч":"ch","Ш":"SH","ш":"sh","Щ":"Ş","щ":"ş","Ы":"Y","ы":"y",
    "І":"I","і":"i","Э":"Ě","э":"ě","Ю":"Ü","ю":"ü","Я":"À","я":"à",
    "Ь":"","ь":"","Ъ":"","ъ":""
}

shyraq_to_cyrillic = {}
for cyr, shy in cyrillic_to_shyraq.items():
    if not shy:
        continue
    shyraq_to_cyrillic[shy.lower()] = cyr.lower()
    shyraq_to_cyrillic[shy.upper()] = cyr.upper()
    shyraq_to_cyrillic[shy.capitalize()] = cyr.upper()

# Regex
cyrillic_patterns = sorted(cyrillic_to_shyraq.keys(), key=lambda x: -len(x))
cyrillic_regex = re.compile("|".join(re.escape(k) for k in cyrillic_patterns))

shyraq_patterns = sorted(shyraq_to_cyrillic.keys(), key=lambda x: -len(x))
shyraq_regex = re.compile("|".join(re.escape(k) for k in shyraq_patterns), re.IGNORECASE)

# ---------------------
# DB
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_prefs (
                user_id INTEGER PRIMARY KEY,
                direction TEXT NOT NULL
            )
        """)
        await db.commit()
    logger.info("DB initialized at %s", DB_PATH)

async def set_user_direction(user_id: int, direction: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO user_prefs(user_id, direction) VALUES (?, ?)
            ON CONFLICT(user_id) DO UPDATE SET direction=excluded.direction
        """, (user_id, direction))
        await db.commit()

async def get_user_direction(user_id: int) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT direction FROM user_prefs WHERE user_id = ?", (user_id,))
        row = await cur.fetchone()
        return row[0] if row else None

# ---------------------
def word_bounds(text: str, idx: int):
    n = len(text)
    s = idx
    while s > 0 and text[s-1].isalpha():
        s -= 1
    e = idx
    while e < n and text[e].isalpha():
        e += 1
    return s, e

def word_is_all_upper(text: str, start: int, end: int) -> bool:
    has_alpha = False
    for ch in text[start:end]:
        if ch.isalpha():
            has_alpha = True
            if ch.islower():
                return False
    return has_alpha

# ---------------------
def translate_text(text: str, to_shyraq: bool = True) -> str:
    if to_shyraq:
        def repl(match):
            ch = match.group(0)
            mapped = cyrillic_to_shyraq.get(ch, ch)
            if ch.isupper():
                s, e = word_bounds(text, match.start())
                all_upper = word_is_all_upper(text, s, e)
                return mapped.upper() if all_upper else mapped.capitalize()
            else:
                return mapped.lower()
        return cyrillic_regex.sub(repl, text)
    else:
        def repl(match):
            chunk = match.group(0)
            base = shyraq_to_cyrillic.get(chunk.lower(), chunk)
            if chunk.isupper():
                return base.upper()
            elif chunk[0].isupper():
                return base.upper()
            else:
                return base.lower()
        return shyraq_regex.sub(repl, text)

# ---------------------
SUBSCRIBE_CHANNELS = ["Zhora08", "Shyraq_Tech"]

async def check_subscriptions(user_id: int, bot: Bot) -> tuple[bool, list[str]]:
    """Қолданушы барлық арнаға жазылған ба — тексереді.
    True -> бәріне жазылған
    False -> тізіммен қай арнаға жазылмағанын қайтарады
    """
    not_subscribed = []
    for channel in SUBSCRIBE_CHANNELS:
        try:
            member = await bot.get_chat_member(f"@{channel}", user_id)
            if member.status in ["left", "kicked"]:
                not_subscribed.append(channel)
        except Exception:
            not_subscribed.append(channel)
    return (len(not_subscribed) == 0, not_subscribed)

async def send_subscribe_prompt(message: types.Message, missing_channels: list[str] | None = None):
    if missing_channels:
        text = (
            "⚠️ Сіз әлі де барлық арнаға жазылмаған екенсіз!\n\n"
            "Төмендегі арналарға жазылыңыз 👇"
        )
        await message.answer(
            text,
            reply_markup=get_incomplete_keyboard(missing_channels)
        )
    else:
        text = (
            "📢 Ботты қолдану үшін төмендегі арналарға жазылыңыз:\n\n"
            "Жазылған соң, «Тексеру ✅️» батырмасын басыңыз."
        )
        await message.answer(
            text,
            reply_markup=get_subscribe_keyboard()
        )

def get_control_buttons(current_direction: str):
    opposite = "to_cyrillic" if current_direction == "to_shyraq" else "to_shyraq"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Бағытын ауыстыру", callback_data=f"switch:{opposite}")],
        [InlineKeyboardButton(text="ℹ️ Толығырақ", url="https://telegra.ph/Help-Shyraq-Translator-Bot-09-30")]
    ])

def get_subscribe_keyboard():
    kb = InlineKeyboardBuilder()
    for i, ch in enumerate(SUBSCRIBE_CHANNELS, start=1):
        kb.button(text=f"📢 {i}-арна", url=f"https://t.me/{ch}")
    kb.button(text="Тексеру ✅️", callback_data="check_subs")
    kb.adjust(1)
    return kb.as_markup()

def get_incomplete_keyboard(missing_channels: list[str]):
    kb = InlineKeyboardBuilder()
    for i, ch in enumerate(missing_channels, start=1):
        kb.button(text=f"📢 {i}-арна", url=f"https://t.me/{ch}")
    kb.button(text="Тексеру ✅️", callback_data="check_subs")
    kb.adjust(1)
    return kb.as_markup()

# ---------------------
# Пәрмендер — Міндетті жазылым қосылды
@router.message(Command("start"))
async def cmd_start(message: types.Message, bot: Bot):
    # Міндетті жазылым тексеру
    is_subscribed, missing = await check_subscriptions(message.from_user.id, bot)
    if not is_subscribed:
        await send_subscribe_prompt(message, missing)
        return

    await message.answer(
        "<b>Сәлем! Мен Shyraq Translator Botпын.</b>\n\n"
        "Пәрмендер:\n"
        "/convert - мәтінді аудару\n"
        "/help - қысқаша көмек\n"
        "/about - қысқаша таныстыру\n"
        "/donate - қолдау\n"
        "/feedback - кері байланыс\n\n"
        "Инлайн режимде қолдану: @ShyraqTranslatorBot &lt;мәтін&gt;"
    )

@router.message(Command("about"))
async def cmd_about(message: types.Message, bot: Bot):
    is_subscribed, missing = await check_subscriptions(message.from_user.id, bot)
    if not is_subscribed:
        await send_subscribe_prompt(message, missing)
        return

    await message.answer(
        "<b>Shyraq әліпбиі</b> — қазақ тілін латын негізінде цифрлық ортаға бейімдейтін жазу жүйесі. "
        "<b>Бұл бот</b> — кирилл мен Shyraq арасында тез әрі дұрыс аударуға арналған құрал.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="ℹ️ Толығырақ", url="https://telegra.ph/About-Shyraq-Translator-Bot-09-30")]
            ]
        )
    )

@router.message(Command("help"))
async def cmd_help(message: types.Message, bot: Bot):
    is_subscribed, missing = await check_subscriptions(message.from_user.id, bot)
    if not is_subscribed:
        await send_subscribe_prompt(message, missing)
        return

    await message.answer(
        "<b>Аудармашыны қолдану</b>\n\n"
        "1️⃣ <b>Бағытын таңдау</b>\nАударма бағытын ауысқыш батырма арқылы таңдаңыз.\n\n"
        "2️⃣ <b>Мәтінді енгізу</b>\nМәтінді енгізіңіз — аударма өздігінен жүзеге асады.\n\n"
        "3️⃣ <b>Бағытын ауыстыру</b>\nҚажет болса, бағытты ауыстыру батырмасын бассаңыз, аударма кері қайтады.\n\n",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="ℹ️ Толығырақ", url="https://telegra.ph/Help-Shyraq-Translator-Bot-09-30")]
            ]
        )
    )

@router.message(Command("donate"))
async def cmd_donate(message: types.Message, bot: Bot):
    is_subscribed, missing = await check_subscriptions(message.from_user.id, bot)
    if not is_subscribed:
        await send_subscribe_prompt(message, missing)
        return

    text = (
        "<b>Ботты қолдаудың 2 жолы бар:</b>\n\n"
        "1) Достарыңызға бөлісу немесе арналарға жазылу: @Zhora08 @Shyraq_Tech\n"
        "2) Қаржылай қолдау: +7 778 764 4508 (Болат Ж.)"
    )

    await message.answer(
        text=text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(
                    text="💰Қаржылай қолдау",
                    url="https://t.me/ShyraqPayBot?start=donate"
                )]
            ]
        )
    )

@router.message(Command("feedback"))
async def cmd_feedback(message: types.Message, bot: Bot):
    is_subscribed, missing = await check_subscriptions(message.from_user.id, bot)
    if not is_subscribed:
        await send_subscribe_prompt(message, missing)
        return

    await message.answer(
        "Кері байланыс жіберу үшін: @shyraq_zheke_bailanys_bot\n\n"
        "✍ Бот туралы сұрақтарыңыз, ұсыныстарыңыз болса, бот жұмысында ақаулар болса хабарласыңыз.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📨Кері байланыс", url="https://t.me/shyraq_zheke_bailanys_bot?start")]
            ]
        )
    )


# ---------------------
# /convert логикасы (Міндетті жазылым қосылған)
@router.message(Command("convert"))
async def cmd_convert(message: types.Message, state: FSMContext, bot: Bot):
    # Міндетті жазылым тексеру
    is_subscribed, missing = await check_subscriptions(message.from_user.id, bot)
    if not is_subscribed:
        await send_subscribe_prompt(message, missing)
        return

    # Егер жазылым дұрыс болса — бағытты көрсету
    current = await get_user_direction(message.from_user.id) or "to_shyraq"
    await set_user_direction(message.from_user.id, current)

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🅰️ Кириллица → Shyraq", callback_data="dir:to_shyraq")],
        [InlineKeyboardButton(text="🔤 Shyraq → Кириллица", callback_data="dir:to_cyrillic")]
    ])

    await message.answer("Мәтінді аудару бағытын таңдаңыз:", reply_markup=kb)
    await state.set_state(ConvertStates.direction)

# Callback Өңдеу
@router.callback_query(F.data.startswith("dir:"))
async def callback_convert_direction(query: types.CallbackQuery, state: FSMContext):
    direction = query.data.split(":", 1)[1]
    await set_user_direction(query.from_user.id, direction)
    await state.update_data(direction=direction)
    await query.message.answer("📥 Аударғыңыз келетін мәтінді жіберіңіз:")
    await state.set_state(ConvertStates.waiting_for_text)
    await query.answer()

@router.callback_query(F.data == "check_subs")
async def check_subs_callback(query: types.CallbackQuery, bot: Bot):
    is_subscribed, missing = await check_subscriptions(query.from_user.id, bot)
    if is_subscribed:
        await query.message.edit_text(
            "✅ Сіз барлық арналарға жазылдыңыз!\n\nЕнді ботты еркін пайдалана аласыз."
        )
        await query.answer("✅ Барлығы дұрыс!")
    else:
        await query.message.edit_text(
            "⚠️ Сіз әлі де барлық арнаға жазылмаған екенсіз!",
            reply_markup=get_incomplete_keyboard(missing)
        )
        await query.answer("Арналарға жазылып болған соң қайта тексеріңіз.")

# Мәтін қабылдау және аудару
@router.message(ConvertStates.waiting_for_text)
async def do_translate(message: types.Message, state: FSMContext):
    if not message.text:
        await message.answer("Мәтін бос болмауы керек.")
        return

    direction = await get_user_direction(message.from_user.id) or "to_shyraq"
    to_shyraq = (direction == "to_shyraq")
    translated = translate_text(message.text, to_shyraq=to_shyraq)

    max_len = 4096
    total_len = len(translated)
    for start in range(0, total_len, max_len):
        part = translated[start:start+max_len]
        info = f"\n\n🔢 Таңба саны (бөлік/барлығы): {len(part)}/{total_len}"
        await message.answer(part + info, reply_markup=get_control_buttons(direction))

    await state.clear()

# ---------------------
# Автоматты аудару
@router.message()
async def auto_translate(message: types.Message, state: FSMContext, bot: Bot):
    # Тек мәтіндерге ғана
    if not message.text:
        return

    # Міндетті жазылым тексеру
    is_subscribed, missing = await check_subscriptions(message.from_user.id, bot)
    if not is_subscribed:
        # Пайдаланушы жазылмаған болса — арналар мен "Тексеру ✅️" батырмасын шығару
        await send_subscribe_prompt(message, missing)
        return

    # Бағытты алу немесе әдепкі "to_shyraq"
    direction = await get_user_direction(message.from_user.id) or "to_shyraq"
    translated = translate_text(message.text, to_shyraq=(direction == "to_shyraq"))

    # Ұзын мәтіндерді бөлікке бөлу
    max_len = 4096
    total_len = len(translated)
    for start in range(0, total_len, max_len):
        part = translated[start:start+max_len]
        info = f"\n\n🔢 Таңба саны (бөлік/барлығы): {len(part)}/{total_len}"
        await message.answer(part + info, reply_markup=get_control_buttons(direction))

@router.callback_query(F.data.startswith("switch:"))
async def callback_switch_direction(query: types.CallbackQuery, state: FSMContext):
    new_dir = query.data.split(":", 1)[1]
    await set_user_direction(query.from_user.id, new_dir)

    if query.message and query.message.text:
        original = query.message.text.split("\n\n🔢")[0]
        retranslated = translate_text(original, to_shyraq=(new_dir == "to_shyraq"))
        info = f"\n\n🔢 Таңба саны: {len(retranslated)}"
        try:
            await query.message.edit_text(
                retranslated + info,
                reply_markup=get_control_buttons(new_dir)
            )
        except Exception:
            await query.message.answer(
                retranslated + info,
                reply_markup=get_control_buttons(new_dir)
            )

    await query.answer("✅ Бағыт өзгертілді")

# ---------------------
# Инлайн режим
@router.inline_query()
async def inline_translate(inline_query: types.InlineQuery, bot: Bot):
    q = inline_query.query or ""

    # ✅ Міндетті жазылым тексеру
    is_subscribed, missing = await check_subscriptions(inline_query.from_user.id, bot)
    if not is_subscribed:
        # Егер жазылмаған болса — арналарға жазылу туралы ескерту көрсетіледі
        not_subscribed_text = (
            "⚠️ Ботты инлайн режимде қолдану үшін төмендегі арналарға жазылыңыз:\n\n"
            + "\n".join(f"📢 https://t.me/{ch}" for ch in missing)
            + "\n\nЖазылған соң — сұрауыңызды қайта енгізіңіз."
        )

        # Инлайн режимде батырмалары бар жауап қайтарылады
        buttons = InlineKeyboardMarkup(inline_keyboard=[
            *[[InlineKeyboardButton(text=f"📢 {i+1}-арна", url=f"https://t.me/{ch}")]
              for i, ch in enumerate(missing)],
        ])
        result = types.InlineQueryResultArticle(
            id=str(uuid.uuid4()),
            title="⚠️ Міндетті жазылым қажет",
            input_message_content=types.InputTextMessageContent(message_text=not_subscribed_text),
            description="Арналарға жазылыңыз да, қайта көріңіз.",
            reply_markup=buttons,
            thumb_url="https://cdn-icons-png.flaticon.com/512/939/939382.png",
            thumb_width=48,
            thumb_height=48,
        )
        await inline_query.answer(results=[result], cache_time=0, is_personal=True)
        return

    # ✅ Егер жазылған болса — бұрынғыдай аудару логикасы
    if not q.strip():
        res_to_shy = "Мәтінді енгізіңіз..."
        res_to_cyr = "Mátindi engiziŋiz..."
    else:
        res_to_shy = translate_text(q, to_shyraq=True)
        res_to_cyr = translate_text(q, to_shyraq=False)

    switch_buttons = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="🔄 Бағытты ауыстыру",
            switch_inline_query_current_chat=q
        )]
    ])

    results = [
        types.InlineQueryResultArticle(
            id=str(uuid.uuid4()),
            title="🅰️ Кириллица → Shyraq",
            input_message_content=types.InputTextMessageContent(message_text=res_to_shy),
            description=(res_to_shy[:256] + '...') if len(res_to_shy) > 256 else res_to_shy,
            thumb_url="https://cdn-icons-png.flaticon.com/512/12452/12452357.png",
            thumb_width=48,
            thumb_height=48,
            reply_markup=switch_buttons
        ),
        types.InlineQueryResultArticle(
            id=str(uuid.uuid4()),
            title="🔤 Shyraq → Кириллица",
            input_message_content=types.InputTextMessageContent(message_text=res_to_cyr),
            description=(res_to_cyr[:256] + '...') if len(res_to_cyr) > 256 else res_to_cyr,
            thumb_url="https://cdn-icons-png.flaticon.com/512/10050/10050517.png",
            thumb_width=48,
            thumb_height=48,
            reply_markup=switch_buttons
        )
    ]

    await inline_query.answer(results=results, cache_time=0, is_personal=True)

# ---------------------
# Router қосу
dp.include_router(router)

# ---------------------
# Ботты іске қосу
async def main():
    logger.info("Дерекқорды қалпына келтіру...")
    await init_db()
    logger.info("Бот дайын. Polling басталды...")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()
        logger.info("Bot stopped.")

if __name__ == "__main__":
    asyncio.run(main())
