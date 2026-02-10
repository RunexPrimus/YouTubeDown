import asyncio
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message

import aiohttp

try:
    from aiohttp_socks import ProxyConnector  # pip install aiohttp-socks
except Exception:
    ProxyConnector = None  # fallback if not installed


# =======================
# CONFIG
# =======================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN env missing")

# If you run Dockerfile with tor inside container -> default works:
# socks5h://127.0.0.1:9050
# If you have remote tor node (VPS) -> set TOR_PROXY=socks5h://<ip>:9050
TOR_PROXY = os.getenv("TOR_PROXY", "socks5h://127.0.0.1:9050").strip()

# Downloader script path (put in repo root)
DOWNLOADER_SCRIPT = Path(os.getenv("DOWNLOADER_SCRIPT", "darkweb-file-downloader.py"))

# Safety
MAX_MB = int(os.getenv("MAX_MB", "150"))
ALLOWED_EXT = set(
    x.strip().lower().lstrip(".")
    for x in os.getenv("ALLOWED_EXT", "pdf,txt,jpg,jpeg,png,zip").split(",")
    if x.strip()
)

# Onion URL validator (v2/v3)
ONION_RE = re.compile(r"^https?://[a-z2-7]{16,56}\.onion(?:/.*)?$", re.I)

# =======================
# JOB QUEUE
# =======================
@dataclass
class Job:
    chat_id: int
    url: str
    mode: str                 # size | count | download
    ext: Optional[str] = None


queue: asyncio.Queue[Job] = asyncio.Queue()


# =======================
# HELPERS
# =======================
def is_onion(url: str) -> bool:
    return bool(ONION_RE.match(url.strip()))

def normalize_ext(ext: str) -> str:
    e = ext.lower().strip().lstrip(".")
    if e not in ALLOWED_EXT:
        raise ValueError(f"❌ Extension ruxsat etilmagan: {e}\n✅ Ruxsat: {', '.join(sorted(ALLOWED_EXT))}")
    return e

def torsocks_exists() -> bool:
    return shutil.which("torsocks") is not None

def python_bin() -> str:
    # Prefer python3 if exists, else python
    return shutil.which("python3") or shutil.which("python") or "python"

async def run_subprocess(cmd: list[str], cwd: Optional[str] = None, timeout: int = 600) -> tuple[int, str]:
    """
    Run subprocess, capture combined output. Timeout in seconds.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT
    )
    try:
        out_bytes, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "TIMEOUT"
    out = (out_bytes or b"").decode(errors="ignore")
    return proc.returncode, out[-8000:]  # tail

async def fetch_via_tor(url: str, timeout: int = 60) -> tuple[bool, str]:
    """
    Quick connectivity test / simple fetch.
    Uses TOR_PROXY with socks5h.
    """
    if not ProxyConnector:
        return False, "aiohttp-socks o'rnatilmagan (pip install aiohttp-socks)"

    connector = ProxyConnector.from_url(TOR_PROXY)
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(url, timeout=timeout) as resp:
                # we don't download the whole thing here; just check it's reachable
                if resp.status >= 400:
                    return False, f"HTTP {resp.status}"
                await resp.content.readexactly(64) if resp.content_length and resp.content_length >= 64 else await resp.content.read(64)
                return True, "OK"
    except Exception as e:
        return False, str(e)


# =======================
# CORE: run downloader
# =======================
async def run_downloader(job: Job) -> tuple[bool, str]:
    """
    Prefer torsocks + downloader script, else fallback: just test via TOR_PROXY.
    """
    if not DOWNLOADER_SCRIPT.exists():
        return False, f"Downloader skript topilmadi: {DOWNLOADER_SCRIPT}"

    py = python_bin()

    # You must implement/patch your downloader to accept args like:
    # --mode size|count|download --url ... [--ext pdf] [--out path] [--max-mb N] [--allow-ext csv]
    # If your downloader is still interactive -> this will not work properly.
    base_cmd = [py, str(DOWNLOADER_SCRIPT), "--mode", job.mode, "--url", job.url]

    if job.mode == "count" and job.ext:
        base_cmd += ["--ext", job.ext]

    with tempfile.TemporaryDirectory(prefix="dwbot_") as tmpdir:
        outdir = Path(tmpdir) / "downloads"
        outdir.mkdir(parents=True, exist_ok=True)

        if job.mode == "download":
            base_cmd += [
                "--out", str(outdir),
                "--max-mb", str(MAX_MB),
                "--allow-ext", ",".join(sorted(ALLOWED_EXT)),
            ]

        # If torsocks exists -> use torsocks (best when tor installed inside container)
        if torsocks_exists():
            cmd = ["torsocks"] + base_cmd
            code, out = await run_subprocess(cmd, timeout=1200)
            if code == 0:
                return True, out
            return False, f"Downloader (torsocks) exit={code}\n{out}"

        # If no torsocks -> fallback: require TOR_PROXY and aiohttp-socks
        # We can't transparently wrap arbitrary script without torsocks,
        # so we at least verify that the onion is reachable via TOR_PROXY
        ok, info = await fetch_via_tor(job.url)
        if not ok:
            return False, (
                "torsocks yo'q va TOR proxy orqali ham ulanib bo'lmadi.\n"
                f"TOR_PROXY={TOR_PROXY}\n"
                f"DETAIL: {info}"
            )

        return False, (
            "✅ Tor proxy orqali .onion reachable.\n"
            "❗ Lekin torsocks yo'qligi sabab downloader skriptni ishga tushira olmadim.\n"
            "Yechim: Dockerfile bilan tor+torsocks o'rnating (tavsiya) yoki downloader'ni to'g'ridan-to'g'ri socks proxy'dan foydalanadigan qilib yozing."
        )


# =======================
# WORKER LOOP
# =======================
async def worker(bot: Bot):
    while True:
        job = await queue.get()
        try:
            await bot.send_message(job.chat_id, f"⏳ Start: `{job.mode}`\n{job.url}", parse_mode="Markdown")

            success, output = await run_downloader(job)
            if success:
                await bot.send_message(job.chat_id, f"✅ Done\n```\n{output}\n```", parse_mode="Markdown")
            else:
                await bot.send_message(job.chat_id, f"⚠️ {output}")
        except Exception as e:
            await bot.send_message(job.chat_id, f"❌ Error: {e}")
        finally:
            queue.task_done()


# =======================
# BOT
# =======================
dp = Dispatcher()

@dp.message(Command("start"))
async def start_cmd(m: Message):
    await m.answer(
        "👋 Darkweb bot.\n\n"
        "Buyruqlar:\n"
        "/size <onion_url>\n"
        "/count <onion_url> [ext]\n"
        "/download <onion_url>\n\n"
        f"Limit: {MAX_MB}MB | Ext: {', '.join(sorted(ALLOWED_EXT))}\n"
        f"Tor proxy: {TOR_PROXY}\n"
    )

@dp.message(Command("size"))
async def size_cmd(m: Message):
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2 or not is_onion(parts[1]):
        return await m.answer("Misol: `/size http://xxxx.onion/path`", parse_mode="Markdown")
    await queue.put(Job(chat_id=m.chat.id, url=parts[1].strip(), mode="size"))
    await m.answer("✅ Queuega qo‘shildi.")

@dp.message(Command("count"))
async def count_cmd(m: Message):
    parts = (m.text or "").split()
    if len(parts) < 2 or not is_onion(parts[1]):
        return await m.answer("Misol: `/count http://xxxx.onion/path pdf`", parse_mode="Markdown")
    ext = None
    if len(parts) >= 3:
        try:
            ext = normalize_ext(parts[2])
        except ValueError as e:
            return await m.answer(str(e))
    await queue.put(Job(chat_id=m.chat.id, url=parts[1].strip(), mode="count", ext=ext))
    await m.answer("✅ Queuega qo‘shildi.")

@dp.message(Command("download"))
async def download_cmd(m: Message):
    parts = (m.text or "").split(maxsplit=1)
    if len(parts) < 2 or not is_onion(parts[1]):
        return await m.answer("Misol: `/download http://xxxx.onion/path`", parse_mode="Markdown")
    await queue.put(Job(chat_id=m.chat.id, url=parts[1].strip(), mode="download"))
    await m.answer("✅ Queuega qo‘shildi.")

@dp.message(F.text)
async def text_fallback(m: Message):
    text = (m.text or "").strip()
    if is_onion(text):
        # default behavior: treat onion link as /download (or you can choose /size)
        await queue.put(Job(chat_id=m.chat.id, url=text, mode="download"))
        return await m.answer("✅ Onion link qabul qilindi. Download queuega qo‘shildi.")
    await m.answer("Onion link yubor yoki /start bosing.")


async def main():
    bot = Bot(BOT_TOKEN)
    asyncio.create_task(worker(bot))
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
