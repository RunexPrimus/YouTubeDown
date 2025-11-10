# handlers/callback_handler.py
import re
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from utils.scraper import fetch_gallery_metadata, build_image_url
from utils.state_manager import get_user_state
from aiohttp import ClientSession
import aiofiles
import asyncio

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    chat_id = update.effective_chat.id
    state = get_user_state(chat_id)

    # 1. Qidiruv sahifalash: search_page:term:page
    if data.startswith("search_page:"):
        parts = data.split(":", 2)
        if len(parts) < 3:
            return
        term, page_str = parts[1], parts[2]
        try:
            page = int(page_str)
        except ValueError:
            return
        state.current_page = page
        results = await search_manga(term, page=page)
        state.search_results = results
        await send_search_results(query, state, term)
        return

    # 2. Manga tanlash: select_manga:12345
    if data.startswith("select_manga:"):
        gallery_id = data.split(":", 1)[1]
        meta = await fetch_gallery_metadata(gallery_id)
        state.current_gallery = meta
        state.total_images = meta["total_images"]
        state.current_image_index = 0  # 0-indexed (1-rasm = index 0)

        # Rasmlar URLlarini yaratamiz (barchasi uchun)
        state.image_urls = [
            build_image_url(
                gallery_id,
                meta["folder"],
                meta["subfolder"],
                i + 1  # 1-indexed
            )
            for i in range(meta["total_images"])
        ]

        await send_image(update, state)
        return

    # 3. Rasm navigatsiya: next_img / prev_img
    if data == "next_img":
        if state.current_image_index < state.total_images - 1:
            state.current_image_index += 1
            await send_image(update, state)
        return

    if data == "prev_img":
        if state.current_image_index > 0:
            state.current_image_index -= 1
            await send_image(update, state)
        return

    if data == "noop":
        return

async def send_image(update: Update, state):
    idx = state.current_image_index
    total = state.total_images
    url = state.image_urls[idx]

    kb = []
    row = []
    if idx > 0:
        row.append(InlineKeyboardButton("⬅️ Oldingi rasm", callback_data="prev_img"))
    if idx < total - 1:
        row.append(InlineKeyboardButton("Keyingi rasm ➡️", callback_data="next_img"))
    if row:
        kb.append(row)

    caption = f"Rasm {idx+1}/{total}"

    # Rasmni yuklab olish va yuborish (inline emas — to'g'ridan-to'g'ri photo)
    try:
        async with ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    content = await resp.read()
                    if update.callback_query:
                        await update.callback_query.message.reply_photo(
                            photo=content,
                            caption=caption,
                            reply_markup=InlineKeyboardMarkup(kb)
                        )
                        # Eski xabarni o'chirish (rasmga o'xshash emas)
                        try:
                            await update.callback_query.message.delete()
                        except:
                            pass
                    else:
                        await update.effective_message.reply_photo(
                            photo=content,
                            caption=caption,
                            reply_markup=InlineKeyboardMarkup(kb)
                        )
                else:
                    raise Exception(f"HTTP {resp.status}")
    except Exception as e:
        print(f"[IMG ERROR] {url} → {e}")
        fallback_msg = f"❌ Rasm yuklanmadi: `{url}`\nIltimos, boshqa manga tanlang."
        if update.callback_query:
            await update.callback_query.message.reply_text(fallback_msg, parse_mode="Markdown")
        else:
            await update.effective_message.reply_text(fallback_msg, parse_mode="Markdown")
