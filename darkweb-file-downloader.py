#!/usr/bin/env python3
from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urldefrag, urlparse

import aiofiles
import aiohttp
from bs4 import BeautifulSoup

try:
    from aiohttp_socks import ProxyConnector  # pip install aiohttp-socks
except Exception:
    ProxyConnector = None


ONION_RE = re.compile(r"^https?://[a-z2-7]{16,56}\.onion(?:/.*)?$", re.I)


@dataclass
class Settings:
    tor_proxy: str
    timeout: int = 60
    max_mb: int = 150
    allow_ext: set[str] = None  # type: ignore
    max_files: int = 300
    max_depth: int = 2
    user_agent: str = "Mozilla/5.0 (compatible; DWBot/1.0)"

    def __post_init__(self):
        if self.allow_ext is None:
            self.allow_ext = {
                "pdf", "txt", "jpg", "jpeg", "png", "zip",
                "mp4", "mkv", "avi", "webm", "mov"
            }


@dataclass
class FileItem:
    url: str
    name: str
    ext: str
    path_parts: tuple[str, ...]


def is_onion_url(url: str) -> bool:
    return bool(ONION_RE.match(url.strip()))


def normalize_proxy(proxy: str) -> str:
    """
    Some libs reject socks5h://. Convert to socks5://.
    """
    p = (proxy or "").strip()
    if p.startswith("socks5h://"):
        p = "socks5://" + p[len("socks5h://"):]
    return p


def norm_ext_from_name(name: str) -> str:
    name = name.strip().lower()
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].lstrip(".")


def should_skip_href(href: str) -> bool:
    if not href:
        return True
    href = href.strip()
    if href.startswith("#"):
        return True
    h = href.lower()
    if h.startswith("javascript:") or h.startswith("mailto:"):
        return True
    return False


def safe_join(base_url: str, href: str) -> str:
    href, _ = urldefrag(href)
    return urljoin(base_url, href)


def looks_like_directory_path(path: str) -> bool:
    return path.endswith("/")


def guess_name_from_url(url: str) -> str:
    p = urlparse(url).path
    if not p or p.endswith("/"):
        return "index"
    return p.rstrip("/").split("/")[-1] or "file"


def url_path_parts(url: str) -> tuple[str, ...]:
    p = urlparse(url).path.strip("/")
    if not p:
        return tuple()
    return tuple(x for x in p.split("/") if x)


def bytes_to_human(n: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    x = float(n)
    for u in units:
        if x < 1024.0 or u == units[-1]:
            if u == "B":
                return f"{int(x)} {u}"
            return f"{x:.2f} {u}"
        x /= 1024.0
    return f"{n} B"


def make_session(settings: Settings) -> aiohttp.ClientSession:
    headers = {"User-Agent": settings.user_agent}
    timeout = aiohttp.ClientTimeout(total=settings.timeout)

    proxy = normalize_proxy(settings.tor_proxy)
    if proxy:
        if not ProxyConnector:
            raise RuntimeError("aiohttp-socks kerak: pip install aiohttp-socks")
        connector = ProxyConnector.from_url(proxy)
        return aiohttp.ClientSession(headers=headers, timeout=timeout, connector=connector)

    return aiohttp.ClientSession(headers=headers, timeout=timeout)


async def fetch_text(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url, allow_redirects=True) as resp:
        resp.raise_for_status()
        return await resp.text(errors="ignore")


async def head_content_length(session: aiohttp.ClientSession, url: str) -> Optional[int]:
    try:
        async with session.head(url, allow_redirects=True) as resp:
            if resp.status >= 400:
                return None
            cl = resp.headers.get("Content-Length")
            if cl and cl.isdigit():
                return int(cl)
            return None
    except Exception:
        return None


async def get_content_length_fallback(session: aiohttp.ClientSession, url: str) -> Optional[int]:
    try:
        async with session.get(url, headers={"Range": "bytes=0-0"}, allow_redirects=True) as resp:
            if resp.status in (200, 206):
                cr = resp.headers.get("Content-Range")
                if cr and "/" in cr:
                    total = cr.split("/")[-1].strip()
                    if total.isdigit():
                        return int(total)
                if resp.status == 200:
                    cl = resp.headers.get("Content-Length")
                    if cl and cl.isdigit():
                        return int(cl)
    except Exception:
        pass
    return None


async def estimate_size_bytes(session: aiohttp.ClientSession, url: str) -> Optional[int]:
    size = await head_content_length(session, url)
    if size is not None:
        return size
    return await get_content_length_fallback(session, url)


def parse_links(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    out: list[str] = []
    for a in soup.find_all("a"):
        href = a.get("href", "")
        if should_skip_href(href):
            continue
        out.append(safe_join(base_url, href))

    # de-dup (keep order)
    seen = set()
    dedup = []
    for u in out:
        if u not in seen:
            seen.add(u)
            dedup.append(u)
    return dedup


def is_allowed(fi: FileItem, settings: Settings) -> bool:
    if not fi.ext:
        return False
    return fi.ext.lower() in settings.allow_ext


async def crawl_directory(session: aiohttp.ClientSession, root_url: str, settings: Settings) -> list[FileItem]:
    root_url = root_url.strip()
    if not root_url.endswith("/"):
        root_url += "/"

    if not is_onion_url(root_url):
        raise ValueError("Not a valid .onion URL")

    host = urlparse(root_url).netloc.lower()

    visited_dirs: set[str] = set()
    q: list[tuple[str, int]] = [(root_url, 0)]
    files: list[FileItem] = []

    while q and len(files) < settings.max_files:
        cur, depth = q.pop(0)
        if depth > settings.max_depth:
            continue
        if not cur.endswith("/"):
            cur += "/"
        if cur in visited_dirs:
            continue
        visited_dirs.add(cur)

        try:
            html = await fetch_text(session, cur)
        except Exception:
            continue

        links = parse_links(html, cur)

        for link in links:
            if len(files) >= settings.max_files:
                break

            u = urlparse(link)
            if u.netloc.lower() != host:
                continue
            if u.path.rstrip("/").endswith(".."):
                continue

            if looks_like_directory_path(u.path):
                if depth + 1 <= settings.max_depth:
                    q.append((link, depth + 1))
                continue

            name = guess_name_from_url(link)
            ext = norm_ext_from_name(name)
            files.append(FileItem(url=link, name=name, ext=ext, path_parts=url_path_parts(link)))

    return files


async def mode_list(root_url: str, settings: Settings, limit: int = 50) -> str:
    async with make_session(settings) as session:
        files = await crawl_directory(session, root_url, settings)
        allowed = [f for f in files if is_allowed(f, settings)]

        if not allowed:
            return "Allowed files: 0/0\n(Directory listing topilmadi yoki ext filtr sabab.)"

        lines = []
        for f in allowed[:limit]:
            lines.append(f"{f.name}\n{f.url}")

        more = ""
        if len(allowed) > limit:
            more = f"\n\n… and {len(allowed) - limit} more"
        return f"Direct links ({min(limit, len(allowed))}/{len(allowed)}):\n\n" + "\n\n".join(lines) + more


async def mode_count(root_url: str, ext: Optional[str], settings: Settings) -> str:
    async with make_session(settings) as session:
        files = await crawl_directory(session, root_url, settings)
        allowed = [f for f in files if is_allowed(f, settings)]

        if ext:
            e = ext.strip().lower().lstrip(".")
            return f"Count .{e}: {sum(1 for f in allowed if f.ext == e)} (allowed set ichida)"

        # count by ext
        counts: dict[str, int] = {}
        for f in allowed:
            k = f.ext or "(noext)"
            counts[k] = counts.get(k, 0) + 1

        parts = [f"Allowed files: {len(allowed)}/{len(files)}"]
        for k in sorted(counts, key=lambda x: (-counts[x], x)):
            parts.append(f"{k}: {counts[k]}")
        return "\n".join(parts)


async def mode_size(root_url: str, settings: Settings) -> str:
    async with make_session(settings) as session:
        files = await crawl_directory(session, root_url, settings)
        allowed = [f for f in files if is_allowed(f, settings)]

        total = 0
        unknown = 0
        for f in allowed:
            sz = await estimate_size_bytes(session, f.url)
            if sz is None:
                unknown += 1
            else:
                total += sz

        return (
            f"Allowed files: {len(allowed)}/{len(files)} | "
            f"Total known size: {bytes_to_human(total)} | Unknown sizes: {unknown}"
        )


async def download_one(session: aiohttp.ClientSession, fi: FileItem, out_root: Path, settings: Settings) -> tuple[bool, str]:
    est = await estimate_size_bytes(session, fi.url)
    if est is not None and est > settings.max_mb * 1024 * 1024:
        return False, f"SKIP too large: {fi.name} ({bytes_to_human(est)})"

    rel_dir = out_root.joinpath(*fi.path_parts[:-1]) if len(fi.path_parts) > 1 else out_root
    rel_dir.mkdir(parents=True, exist_ok=True)
    out_path = rel_dir / fi.name

    max_bytes = settings.max_mb * 1024 * 1024
    downloaded = 0

    try:
        async with session.get(fi.url, allow_redirects=True) as resp:
            resp.raise_for_status()
            async with aiofiles.open(out_path, "wb") as f:
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    if not chunk:
                        continue
                    downloaded += len(chunk)
                    if downloaded > max_bytes:
                        await f.close()
                        try:
                            out_path.unlink(missing_ok=True)  # py3.8+
                        except Exception:
                            pass
                        return False, f"SKIP exceeded limit: {fi.name}"
                    await f.write(chunk)
        return True, f"OK {fi.name} ({bytes_to_human(downloaded)})"
    except Exception as e:
        try:
            out_path.unlink(missing_ok=True)
        except Exception:
            pass
        return False, f"FAIL {fi.name}: {e}"


async def mode_download(root_url: str, out_dir: str, settings: Settings) -> str:
    out_root = Path(out_dir).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    async with make_session(settings) as session:
        files = await crawl_directory(session, root_url, settings)
        allowed = [f for f in files if is_allowed(f, settings)]

        ok = skip = fail = 0
        logs: list[str] = []

        for fi in allowed:
            success, msg = await download_one(session, fi, out_root, settings)
            logs.append(msg)
            if success:
                ok += 1
            else:
                if msg.startswith("SKIP"):
                    skip += 1
                else:
                    fail += 1

        summary = (
            f"Allowed files: {len(allowed)}/{len(files)} | Downloaded: {ok} | Skipped: {skip} | Failed: {fail}\n"
            f"Saved to: {out_root}"
        )
        tail = "\n".join(logs[-50:])
        return summary + ("\n\n" + tail if tail else "")


def parse_allow_ext(csv: str) -> set[str]:
    return set(x.strip().lower().lstrip(".") for x in (csv or "").split(",") if x.strip())


def build_settings(args: argparse.Namespace) -> Settings:
    tor_proxy = (args.tor_proxy or os.getenv("TOR_PROXY", "")).strip()
    max_mb = int(args.max_mb or os.getenv("MAX_MB", "150"))
    allow_ext = parse_allow_ext(args.allow_ext or os.getenv(
        "ALLOWED_EXT",
        "pdf,txt,jpg,jpeg,png,zip,mp4,mkv,avi,webm,mov"
    ))
    max_files = int(args.max_files or os.getenv("MAX_FILES", "300"))
    max_depth = int(args.max_depth or os.getenv("MAX_DEPTH", "2"))
    timeout = int(args.timeout or os.getenv("TIMEOUT", "60"))

    return Settings(
        tor_proxy=tor_proxy,
        max_mb=max_mb,
        allow_ext=allow_ext,
        max_files=max_files,
        max_depth=max_depth,
        timeout=timeout,
    )


async def main_async() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", required=True, choices=["size", "count", "download", "list"])
    p.add_argument("--url", required=True)
    p.add_argument("--ext", default=None)
    p.add_argument("--out", default="downloads")
    p.add_argument("--max-mb", default=None)
    p.add_argument("--allow-ext", default=None)
    p.add_argument("--tor-proxy", dest="tor_proxy", default=None)
    p.add_argument("--max-files", default=None)
    p.add_argument("--max-depth", default=None)
    p.add_argument("--timeout", default=None)
    args = p.parse_args()

    if not is_onion_url(args.url):
        print("ERROR: url must be a valid .onion URL", file=sys.stderr)
        return 2

    settings = build_settings(args)

    # if proxy set, we need aiohttp-socks
    if normalize_proxy(settings.tor_proxy) and not ProxyConnector:
        print("ERROR: TOR_PROXY set but aiohttp-socks is not installed", file=sys.stderr)
        return 3

    try:
        if args.mode == "list":
            out = await mode_list(args.url, settings, limit=50)
        elif args.mode == "count":
            out = await mode_count(args.url, args.ext, settings)
        elif args.mode == "size":
            out = await mode_size(args.url, settings)
        else:
            out = await mode_download(args.url, args.out, settings)

        print(out)
        return 0
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


def main():
    raise SystemExit(asyncio.run(main_async()))


if __name__ == "__main__":
    main()
