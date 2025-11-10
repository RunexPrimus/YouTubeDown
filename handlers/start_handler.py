# handlers/start_handler.py
from telegram import Update
from telegram.ext import ContextTypes

async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Salom! Manga nomini kiriting yoki quyidagicha qidiring:\n"
        "`/search Naruto`",
        parse_mode="Markdown"
    )
