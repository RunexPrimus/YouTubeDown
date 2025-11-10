# main.py
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, CallbackQueryHandler
from handlers.start_handler import start_handler
from handlers.search_handler import search_handler
from handlers.callback_handler import callback_handler

# Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)

TOKEN = "8571420841:AAHy2j_GhUMRqOFDixbGhnQ6z3b6NswcU1E"  # ← Shu yerni o'zgartiring!

def main():
    app = Application.builder().token(TOKEN).build()

    # Handlers
    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("search", search_handler))
    app.add_handler(CallbackQueryHandler(callback_handler))

    print("✅ Bot ishga tushdi!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
