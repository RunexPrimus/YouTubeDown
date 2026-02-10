import asyncio
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

# Import downloader module functions
import darkweb_file_downloader as dwd  # IMPORTANT: file name must be importable


# =========================
# CONFIG (.env)
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env missing")

TOR_PROXY = os.getenv("TOR_PROXY", "socks5h://127.0.0.1:9050").strip()
MAX_MB = int(os.getenv("MAX_MB", "150"))
ALLOWED_EXT = os.getenv("ALLOWED_EXT", "pdf,txt,jpg,jpeg,png,zip")
MAX_FILES = int(os.getenv("MAX_FILES", "200"))
MAX_DEPTH = int(os.getenv("MAX_DEPTH", "2"))
TIMEOUT = int(os.getenv("TIMEOUT", "60"))

# where to store downloads
DOWNLOAD_ROOT = os.getenv("DOWNLOAD_ROOT", "/tmp/dw_downloads").strip()


def build_settings() -> dwd.Settings:
    return dwd.Settings(
        tor_proxy=TOR_PROXY,
        max_mb=MAX_MB,
        allow_ext=set(x.strip().lower().lstrip(".") for x in ALLOWED_EXT.split(",") if x.strip()),
        max_files=MAX_FILES,
        max_depth=MAX_DEPTH,
        timeout=TIMEOUT,
    )


# =========================
# JOB QUEUE
# =========================
@dataclass
class Job:
    chat_id: int
    mode: str             # size | count | download
    url: str
    ext: Optional[str] = None


queue: asyncio.Queue[Job] = asyncio.Queue()


async def worker(bot: Bot):
    while True:
        job = await queue.get()
        try:
            settings = build_settings()

            await bot.send_message(
                job.chat_id,
                f"⏳ Start: {job.mode}\n{job.url}\nTor: {TOR_PROXY or 'OFF'}\n"
                f"Limit: {MAX_MB}MB | Ext: {ALLOWED_EXT}"
            )

            if job.mode == "size":
                result = await dwd.mode_size(job.url, settings)
                await bot.send_message(job.chat_id, f"✅\n{result}")

            elif job.mode == "count":
                result = await dwd.mode_count(job.url, job.ext, settings)
                await bot.send_message(job.chat_id, f"✅\n{result}")

            elif job.mode == "download":
                # Save in per-job folder
                Path(DOWNLOAD_ROOT).mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(prefix="dwjob_", dir=DOWNLOAD_ROOT) as tmpdir:
                    result = await dwd.mode_download(job.url, tmpdir, settings)

                    # We do NOT auto-send all files (Telegram size limits + safety).
                    # Instead, show summary and path.
                    await bot.send_message(job.chat_id, f"✅ Download done\n{result}")

            else:
                await bot.send_message(job.chat_id, "❌ Unknown mode")

        except Exception as e:
            await bot.send_message(job.chat_id, f"❌ Error: {e}")
        finally:
            queue.task_done()


# =========================
# BOT
# =========================
dp = Dispatcher()


@dp.message(Command("start"))
async def start_cmd(m: Message):
    await m.answer(
        "👋 Darkweb File Bot\n\n"
        "Buyruqlar:\n"
        "/size <onion_url>\n"
        "/count <onion_url> [ext]\n"
        "/download <onion_url>\n\n"
        "Onion linkni shunchaki yuborsang ham /download qiladi.\n"
        f"TOR_PROXY: {TOR_PROXY}\n"
        f"MAX_MB: {MAX_MB}\n"
        f"ALLOWED_EXT: {ALLOWED_EXT}\n"
    )


@dp.message(Command("size"))
async def size_cmd(m: Message):
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2 or not dwd.is_onion_url(parts[1]):
        return await m.answer("Misol: /size http://xxxx.onion/path/")
    await queue.put(Job(chat_id=m.chat.id, mode="size", url=parts[1].strip()))
    await m.answer("✅ Queuega qo‘shildi.")


@dp.message(Command("count"))
async def count_cmd(m: Message):
    parts = (m.text or "").split()
    if len(parts) < 2 or not dwd.is_onion_url(parts[1]):
        return await m.answer("Misol: /count http://xxxx.onion/path/ pdf")
    ext = None
    if len(parts) >= 3:
        ext = parts[2].strip().lower().lstrip(".")
    await queue.put(Job(chat_id=m.chat.id, mode="count", url=parts[1].strip(), ext=ext))
    await m.answer("✅ Queuega qo‘shildi.")


@dp.message(Command("download"))
async def download_cmd(m: Message):
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2 or not dwd.is_onion_url(parts[1]):
        return await m.answer("Misol: /download http://xxxx.onion/path/")
    await queue.put(Job(chat_id=m.chat.id, mode="download", url=parts[1].strip()))
    await m.answer("✅ Queuega qo‘shildi.")


@dp.message(F.text)
async def onion_auto(m: Message):
    text = (m.text or "").strip()
    if dwd.is_onion_url(text):
        await queue.put(Job(chat_id=m.chat.id, mode="download", url=text))
        return await m.answer("✅ Onion link qabul qilindi. Download queuega qo‘shildi.")
    await m.answer("Onion link yubor yoki /start.")


async def main():
    bot = Bot(BOT_TOKEN)
    asyncio.create_task(worker(bot))
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
