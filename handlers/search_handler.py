# handlers/search_handler.py
import re
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from utils.scraper import search_manga
from utils.state_manager import get_user_state

async def search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    term = " ".join(context.args).strip() if context.args else ""
    if not term:
        await update.message.reply_text("🔍 Iltimos, qidiruv so'zini kiriting: `/search so'z`", parse_mode="Markdown")
        return

    state = get_user_state(update.effective_chat.id)
    state.current_page = 1
    results = await search_manga(term, page=1)
    state.search_results = results

    if not results:
        await update.message.reply_text("❌ Hech narsa topilmadi.")
        return

    await send_search_results(update, state, term)

async def send_search_results(update: Update, state, term: str):
    kb = []
    for i, item in enumerate(state.search_results):
        kb.append([InlineKeyboardButton(
            f"{i+1}. {item['title'][:40]}",
            callback_data=f"select_manga:{item['gallery_id']}"
        )])

    nav = []
    if state.current_page > 1:
        nav.append(InlineKeyboardButton("⬅️ Oldingi sahifa", callback_data=f"search_page:{term}:{state.current_page-1}"))
    nav.append(InlineKeyboardButton(f"Sahifa: {state.current_page}", callback_data="noop"))
    # Keyingi sahifani sinab ko'rish uchun:
    # Natijalar soni == 10 bo'lsa, ehtimol keyingi sahifa bor
    if len(state.search_results) == 10:
        nav.append(InlineKeyboardButton("Keyingi sahifa ➡️", callback_data=f"search_page:{term}:{state.current_page+1}"))
    kb.append(nav)

    text = f"🔍 *{term}* bo‘yicha natijalar (Sahifa {state.current_page}):"
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="Markdown")
