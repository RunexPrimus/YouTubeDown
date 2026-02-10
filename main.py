import asyncio
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

# ---- dynamic import for filename with '-' ----
import importlib.util

DOWNLOADER_PATH = Path(__file__).with_name("darkweb-file-downloader.py")
spec = importlib.util.spec_from_file_location("dwd", str(DOWNLOADER_PATH))
if spec is None or spec.loader is None:
    raise RuntimeError(f"Cannot load downloader module from {DOWNLOADER_PATH}")
dwd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dwd)

# =========================
# ENV CONFIG
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env missing")

TOR_PROXY = os.getenv("TOR_PROXY", "socks5://127.0.0.1:9050").strip()
MAX_MB = int(os.getenv("MAX_MB", "150"))
ALLOWED_EXT = os.getenv("ALLOWED_EXT", "pdf,txt,jpg,jpeg,png,zip,mp4,mkv,avi,webm,mov")
MAX_FILES = int(os.getenv("MAX_FILES", "300"))
MAX_DEPTH = int(os.getenv("MAX_DEPTH", "2"))
TIMEOUT = int(os.getenv("TIMEOUT", "60"))

DOWNLOAD_ROOT = os.getenv("DOWNLOAD_ROOT", "/tmp/dw_downloads").strip()

ONION_RE = re.compile(r"^https?://[a-z2-7]{16,56}\.onion(?:/.*)?$", re.I)

def is_onion(url: str) -> bool:
    return bool(ONION_RE.match(url.strip()))

def build_settings() -> "dwd.Settings":
    allow = set(x.strip().lower().lstrip(".") for x in ALLOWED_EXT.split(",") if x.strip())
    return dwd.Settings(
        tor_proxy=TOR_PROXY,
        max_mb=MAX_MB,
        allow_ext=allow,
        max_files=MAX_FILES,
        max_depth=MAX_DEPTH,
        timeout=TIMEOUT,
    )

# =========================
# QUEUE
# =========================
@dataclass
class Job:
    chat_id: int
    mode: str
    url: str
    ext: Optional[str] = None

queue: asyncio.Queue[Job] = asyncio.Queue()

dp = Dispatcher()

async def worker(bot: Bot):
    while True:
        job = await queue.get()
        try:
            settings = build_settings()

            if job.mode == "list":
                result = await dwd.mode_list(job.url, settings, limit=50)
                await bot.send_message(job.chat_id, f"✅\n{result}")

            elif job.mode == "size":
                result = await dwd.mode_size(job.url, settings)
                await bot.send_message(job.chat_id, f"✅\n{result}")

            elif job.mode == "count":
                result = await dwd.mode_count(job.url, job.ext, settings)
                await bot.send_message(job.chat_id, f"✅\n{result}")

            elif job.mode == "download":
                Path(DOWNLOAD_ROOT).mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix="dwjob_", dir=DOWNLOAD_ROOT) as tmpdir:
                    result = await dwd.mode_download(job.url, tmpdir, settings)
                    await bot.send_message(job.chat_id, f"✅ Download done\n{result}")

            else:
                await bot.send_message(job.chat_id, "❌ Unknown mode")

        except Exception as e:
            await bot.send_message(job.chat_id, f"❌ Error: {e}")
        finally:
            queue.task_done()

@dp.message(Command("start"))
async def start_cmd(m: Message):
    await m.answer(
        "👋 Bot ishlayapti.\n\n"
        "Komandalar:\n"
        "/list <onion_dir_url>  (direct linklar)\n"
        "/count <onion_dir_url> [ext]\n"
        "/size <onion_dir_url>\n"
        "/download <onion_dir_url>\n\n"
        "Onion linkni shunchaki yuborsang ham /list qiladi.\n\n"
        f"TOR_PROXY: {TOR_PROXY}\n"
        f"MAX_MB: {MAX_MB}\n"
        f"ALLOWED_EXT: {ALLOWED_EXT}\n"
    )

@dp.message(Command("list"))
async def list_cmd(m: Message):
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2 or not is_onion(parts[1]):
        return await m.answer("Misol: /list http://xxxx.onion/path/")
    await queue.put(Job(chat_id=m.chat.id, mode="list", url=parts[1].strip()))
    await m.answer("✅ Queuega qo‘shildi (list).")

@dp.message(Command("size"))
async def size_cmd(m: Message):
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2 or not is_onion(parts[1]):
        return await m.answer("Misol: /size http://xxxx.onion/path/")
    await queue.put(Job(chat_id=m.chat.id, mode="size", url=parts[1].strip()))
    await m.answer("✅ Queuega qo‘shildi (size).")

@dp.message(Command("count"))
async def count_cmd(m: Message):
    parts = (m.text or "").split()
    if len(parts) < 2 or not is_onion(parts[1]):
        return await m.answer("Misol: /count http://xxxx.onion/path/ mp4")
    ext = parts[2].strip().lower().lstrip(".") if len(parts) >= 3 else None
    await queue.put(Job(chat_id=m.chat.id, mode="count", url=parts[1].strip(), ext=ext))
    await m.answer("✅ Queuega qo‘shildi (count).")

@dp.message(Command("download"))
async def download_cmd(m: Message):
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2 or not is_onion(parts[1]):
        return await m.answer("Misol: /download http://xxxx.onion/path/")
    await queue.put(Job(chat_id=m.chat.id, mode="download", url=parts[1].strip()))
    await m.answer("✅ Queuega qo‘shildi (download).")

@dp.message(F.text)
async def onion_auto(m: Message):
    text = (m.text or "").strip()
    if is_onion(text):
        # default: direct links
        await queue.put(Job(chat_id=m.chat.id, mode="list", url=text))
        return await m.answer("✅ Onion link qabul qilindi. Direct linklar olinmoqda (/list).")
    await m.answer("Onion link yubor yoki /start.")

async def main():
    bot = Bot(BOT_TOKEN)
    asyncio.create_task(worker(bot))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
