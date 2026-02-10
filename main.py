import asyncio
import os
import re
import shlex
import tempfile
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN missing in .env")

# --- Safety limits ---
MAX_MB = 150
ALLOWED_EXT = {"pdf", "mkv", "jpg", "jpeg", "png", "zip"}  # change as you lik
ONION_RE = re.compile(r"^https?://[a-z2-7]{16,56}\.onion(/.*)?$", re.I)

SCRIPT_PATH = Path("./darkweb-file-downloader.py")  # repo file name
TORSOCKS_BIN = "torsocks"


@dataclass
class Job:
    chat_id: int
    url: str
    mode: str  # "size" | "count" | "download"
    ext: str | None = None


job_queue: asyncio.Queue[Job] = asyncio.Queue()


def is_valid_onion_url(url: str) -> bool:
    return bool(ONION_RE.match(url.strip()))


def safe_ext(ext: str) -> str:
    ext = ext.lower().strip().lstrip(".")
    if ext not in ALLOWED_EXT:
        raise ValueError(f"Extension not allowed: {ext}")
    return ext


async def run_cmd_and_stream(chat_id: int, bot: Bot, cmd: list[str], workdir: str | None = None) -> tuple[int, str]:
    """
    Runs a subprocess and streams some output back to user (throttled).
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=workdir,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    collected = []
    last_sent = 0.0

    assert proc.stdout is not None
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        text = line.decode(errors="ignore").rstrip()
        collected.append(text)

        # throttle updates
        now = asyncio.get_event_loop().time()
        if now - last_sent > 2.5:
            last_sent = now
            # keep message short
            snippet = "\n".join(collected[-8:])
            await bot.send_message(chat_id, f"⏳ Running...\n```\n{snippet}\n```", parse_mode="Markdown")

    code = await proc.wait()
    out = "\n".join(collected[-200:])  # keep tail
    return code, out


async def scan_with_clamav(file_path: Path) -> str:
    """
    ClamAV scan (blocking external command). Return short summary.
    """
    cmd = ["clamscan", "--no-summary", str(file_path)]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await proc.communicate()
    text = out.decode(errors="ignore").strip()
    return text or "No output from clamscan"


async def worker(bot: Bot):
    while True:
        job = await job_queue.get()
        try:
            await bot.send_message(job.chat_id, f"🧾 Job started: {job.mode}\n{job.url}")

            # Create isolated temp folder per job
            with tempfile.TemporaryDirectory(prefix="dwbot_") as tmpdir:
                outdir = Path(tmpdir) / "downloads"
                outdir.mkdir(parents=True, exist_ok=True)

                # IMPORTANT: This assumes you refactored script to accept args.
                # Example commands you should implement in the downloader script:
                cmd = [TORSOCKS_BIN, "python3", str(SCRIPT_PATH), "--mode", job.mode, "--url", job.url]

                if job.mode == "download":
                    cmd += ["--out", str(outdir), "--max-mb", str(MAX_MB), "--allow-ext", ",".join(sorted(ALLOWED_EXT))]
                elif job.mode == "count":
                    if job.ext:
                        cmd += ["--ext", job.ext]

                code, out = await run_cmd_and_stream(job.chat_id, bot, cmd)

                if code != 0:
                    await bot.send_message(job.chat_id, f"❌ Failed (exit {code})\n```\n{out}\n```", parse_mode="Markdown")
                    continue

                # If download mode: scan downloaded files and report
                if job.mode == "download":
                    files = list(outdir.rglob("*"))
                    files = [p for p in files if p.is_file()]

                    if not files:
                        await bot.send_message(job.chat_id, "ℹ️ Hech narsa yuklanmadi (yoki limit/filtr sabab).")
                        continue

                    # Scan each file
                    report_lines = []
                    for fp in files[:30]:  # avoid huge spam
                        res = await scan_with_clamav(fp)
                        report_lines.append(f"{fp.name}: {res}")

                    report = "\n".join(report_lines)
                    await bot.send_message(job.chat_id, f"🛡️ Scan report (first 30 files):\n```\n{report}\n```", parse_mode="Markdown")

                    # OPTIONAL: send files only if clean + small (Telegram limits apply)
                    # I recommend NOT auto-sending by default.

                else:
                    # size/count modes: just show output tail
                    await bot.send_message(job.chat_id, f"✅ Done\n```\n{out}\n```", parse_mode="Markdown")

        except Exception as e:
            await bot.send_message(job.chat_id, f"⚠️ Error: {e}")
        finally:
            job_queue.task_done()


async def main():
    bot = Bot(BOT_TOKEN)
    dp = Dispatcher()

    @dp.message(Command("start"))
    async def start(m: Message):
        await m.answer(
            "👋 Darkweb tool bot.\n"
            "Buyruqlar:\n"
            "/size <onion_url>\n"
            "/count <onion_url> [ext]\n"
            "/download <onion_url>\n"
            "⚠️ Faqat qonuniy va xavfsiz kontent uchun."
        )

    @dp.message(Command("size"))
    async def size(m: Message):
        parts = (m.text or "").split(maxsplit=1)
        if len(parts) < 2 or not is_valid_onion_url(parts[1]):
            return await m.answer("Misol: /size http://xxxx.onion/path")
        await job_queue.put(Job(chat_id=m.chat.id, url=parts[1].strip(), mode="size"))
        await m.answer("✅ Queuega qo‘shildi.")

    @dp.message(Command("count"))
    async def count(m: Message):
        parts = (m.text or "").split()
        if len(parts) < 2 or not is_valid_onion_url(parts[1]):
            return await m.answer("Misol: /count http://xxxx.onion/path pdf")
        ext = None
        if len(parts) >= 3:
            try:
                ext = safe_ext(parts[2])
            except ValueError as e:
                return await m.answer(str(e))
        await job_queue.put(Job(chat_id=m.chat.id, url=parts[1].strip(), mode="count", ext=ext))
        await m.answer("✅ Queuega qo‘shildi.")

    @dp.message(Command("download"))
    async def download(m: Message):
        parts = (m.text or "").split(maxsplit=1)
        if len(parts) < 2 or not is_valid_onion_url(parts[1]):
            return await m.answer("Misol: /download http://xxxx.onion/path")
        await job_queue.put(Job(chat_id=m.chat.id, url=parts[1].strip(), mode="download"))
        await m.answer("✅ Queuega qo‘shildi. (Limit + filter ishlaydi)")

    # Start worker(s)
    asyncio.create_task(worker(bot))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
