#!/usr/bin/env python3
"""
darkweb-file-downloader.py

Modes:
- size:    estimate total bytes of files in an onion directory
- count:   count files (optionally filtered by extension)
- download: download files with limits

Networking:
- Uses TOR_PROXY (socks5h://...) if provided.
- If TOR_PROXY is empty, uses direct internet (won't work for .onion unless torsocks wraps the whole process).

Directory listing:
- Works best with standard "Index of /" (Apache/nginx) HTML listings.
- For arbitrary pages, it will still collect <a href> links and filter them.

Safety:
- MAX_MB, ALLOW_EXT list, max files, recursion depth.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional
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
    max_files: int = 200
    max_depth: int = 2
    user_agent: str = "Mozilla/5.0 (compatible; DWBot/1.0)"

    def __post_init__(self):
        if self.allow_ext is None:
            self.allow_ext = {"mp4", "mkv", "jpg", "jpeg", "png", "zip"}


@dataclass
class FileItem:
    url: str
    name: str
    ext: str
    path_parts: tuple[str, ...]


def is_onion_url(url: str) -> bool:
    return bool(ONION_RE.match(url.strip()))


def norm_ext_from_name(name: str) -> str:
    name = name.strip().lower()
    if "." not in name:
        return ""
    return name.rsplit(".", 1)[-1].lstrip(".")


def safe_join(base_url: str, href: str) -> str:
    # remove fragment (#...)
    href, _ = urldefrag(href)
    return urljoin(base_url, href)


def is_same_host(a: str, b: str) -> bool:
    try:
        return urlparse(a).netloc.lower() == urlparse(b).netloc.lower()
    except Exception:
        return False


def looks_like_directory_link(href: str) -> bool:
    # Apache listing directories usually end with /
    return href.endswith("/")


def should_skip_href(href: str) -> bool:
    if not href:
        return True
    href = href.strip()
    if href.startswith("#"):
        return True
    if href.lower().startswith("javascript:"):
        return True
    if href.lower().startswith("mailto:"):
        return True
    return False


def make_session(settings: Settings) -> aiohttp.ClientSession:
    headers = {"User-Agent": settings.user_agent}
    timeout = aiohttp.ClientTimeout(total=settings.timeout)

    # Use Tor proxy if available
    if settings.tor_proxy:
        if not ProxyConnector:
            raise RuntimeError("aiohttp-socks is required for TOR_PROXY (pip install aiohttp-socks)")
        connector = ProxyConnector.from_url(settings.tor_proxy)
        return aiohttp.ClientSession(headers=headers, timeout=timeout, connector=connector)

    # Direct session
    return aiohttp.ClientSession(headers=headers, timeout=timeout)


async def fetch_text(session: aiohttp.ClientSession, url: str) -> str:
    async with session.get(url, allow_redirects=True) as resp:
        resp.raise_for_status()
        return await resp.text(errors="ignore")


async def head_content_length(session: aiohttp.ClientSession, url: str) -> Optional[int]:
    # Some onion servers don't support HEAD; we try HEAD then fallback GET range
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
    # Try GET with Range: bytes=0-0 to get Content-Range or Content-Length
    try:
        async with session.get(url, headers={"Range": "bytes=0-0"}, allow_redirects=True) as resp:
            if resp.status in (200, 206):
                cr = resp.headers.get("Content-Range")
                if cr and "/" in cr:
                    total = cr.split("/")[-1].strip()
                    if total.isdigit():
                        return int(total)
                cl = resp.headers.get("Content-Length")
                if cl and cl.isdigit():
                    # If 206, CL is 1; not total. Prefer Content-Range.
                    return int(cl) if resp.status == 200 else None
    except Exception:
        pass
    return None


async def estimate_size_bytes(session: aiohttp.ClientSession, url: str) -> Optional[int]:
    size = await head_content_length(session, url)
    if size is not None:
        return size
    return await get_content_length_fallback(session, url)


def parse_links_from_listing(html: str, base_url: str) -> list[str]:
    soup = BeautifulSoup(html, "lxml")
    links: list[str] = []
    for a in soup.find_all("a"):
        href = a.get("href", "")
        if should_skip_href(href):
            continue
        full = safe_join(base_url, href)
        links.append(full)
    # de-dup, keep order
    seen = set()
    out = []
    for x in links:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def guess_name_from_url(url: str) -> str:
    p = urlparse(url).path
    if not p or p.endswith("/"):
        return "index"
    return p.rstrip("/").split("/")[-1] or "file"


def url_path_parts(url: str) -> tuple[str, ...]:
    p = urlparse(url).path.strip("/")
    if not p:
        return tuple()
    return tuple([x for x in p.split("/") if x])


async def crawl_directory(
    session: aiohttp.ClientSession,
    root_url: str,
    settings: Settings,
) -> list[FileItem]:
    """
    Crawl links starting from root_url, staying on the same onion host.
    Collect files up to settings.max_files, depth-limited.
    """
    root_url = root_url.strip()
    if not is_onion_url(root_url):
        raise ValueError("Not a valid .onion url")

    host = urlparse(root_url).netloc.lower()

    files: list[FileItem] = []
    visited_dirs: set[str] = set()
    q: list[tuple[str, int]] = [(root_url, 0)]

    while q and len(files) < settings.max_files:
        cur, depth = q.pop(0)
        if depth > settings.max_depth:
            continue

        # normalize directory url to end with /
        if not cur.endswith("/"):
            cur_dir = cur + "/"
        else:
            cur_dir = cur

        if cur_dir in visited_dirs:
            continue
        visited_dirs.add(cur_dir)

        try:
            html = await fetch_text(session, cur_dir)
        except Exception:
            # If it's not a directory listing but maybe a file, skip
            continue

        links = parse_links_from_listing(html, cur_dir)

        for link in links:
            if len(files) >= settings.max_files:
                break

            if urlparse(link).netloc.lower() != host:
                continue  # stay in same onion service

            # skip parent dir links
            if link.rstrip("/").endswith(".."):
                continue

            # decide file vs dir
            path = urlparse(link).path
            if not path:
                continue

            if looks_like_directory_link(path):
                # enqueue next dir
                if depth + 1 <= settings.max_depth:
                    q.append((link, depth + 1))
                continue

            name = guess_name_from_url(link)
            ext = norm_ext_from_name(name)
            fi = FileItem(url=link, name=name, ext=ext, path_parts=url_path_parts(link))
            files.append(fi)

    return files


def bytes_to_human(n: int) -> str:
    # simple human readable
    units = ["B", "KB", "MB", "GB", "TB"]
    x = float(n)
    for u in units:
        if x < 1024.0 or u == units[-1]:
            if u == "B":
                return f"{int(x)} {u}"
            return f"{x:.2f} {u}"
        x /= 1024.0
    return f"{n} B"


async def mode_count(root_url: str, ext: Optional[str], settings: Settings) -> str:
    async with make_session(settings) as session:
        files = await crawl_directory(session, root_url, settings)
        if ext:
            ext = ext.lower().lstrip(".")
            files = [f for f in files if f.ext == ext]
        return f"Found {len(files)} file(s)" + (f" with .{ext}" if ext else "")


async def mode_size(root_url: str, settings: Settings) -> str:
    async with make_session(settings) as session:
        files = await crawl_directory(session, root_url, settings)
        total = 0
        unknown = 0
        # sequential to avoid hammering onion
        for f in files:
            sz = await estimate_size_bytes(session, f.url)
            if sz is None:
                unknown += 1
                continue
            total += sz
        return f"Files: {len(files)} | Total known size: {bytes_to_human(total)} | Unknown sizes: {unknown}"


def is_allowed(fi: FileItem, settings: Settings) -> bool:
    if not fi.ext:
        return False
    return fi.ext.lower() in settings.allow_ext


async def download_one(
    session: aiohttp.ClientSession,
    fi: FileItem,
    out_root: Path,
    settings: Settings,
) -> tuple[bool, str]:
    # size gate (best-effort)
    est = await estimate_size_bytes(session, fi.url)
    if est is not None and est > settings.max_mb * 1024 * 1024:
        return False, f"SKIP too large: {fi.name} ({bytes_to_human(est)})"

    # create path
    rel_dir = out_root.joinpath(*fi.path_parts[:-1]) if len(fi.path_parts) > 1 else out_root
    rel_dir.mkdir(parents=True, exist_ok=True)
    out_path = rel_dir / fi.name

    # stream download with hard limit
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
                        # stop and delete partial
                        await f.close()
                        try:
                            out_path.unlink(missing_ok=True)  # type: ignore[attr-defined]
                        except Exception:
                            pass
                        return False, f"SKIP exceeded limit while downloading: {fi.name}"
                    await f.write(chunk)
        return True, f"OK {fi.name} ({bytes_to_human(downloaded)})"
    except Exception as e:
        try:
            out_path.unlink(missing_ok=True)  # type: ignore[attr-defined]
        except Exception:
            pass
        return False, f"FAIL {fi.name}: {e}"


async def mode_download(root_url: str, out_dir: str, settings: Settings) -> str:
    out_root = Path(out_dir).resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    async with make_session(settings) as session:
        files = await crawl_directory(session, root_url, settings)
        files_allowed = [f for f in files if is_allowed(f, settings)]

        ok_count = 0
        skip_count = 0
        fail_count = 0
        logs: list[str] = []

        for fi in files_allowed:
            ok, msg = await download_one(session, fi, out_root, settings)
            logs.append(msg)
            if ok:
                ok_count += 1
            else:
                if msg.startswith("SKIP"):
                    skip_count += 1
                else:
                    fail_count += 1

        summary = (
            f"Allowed files: {len(files_allowed)}/{len(files)} | "
            f"Downloaded: {ok_count} | Skipped: {skip_count} | Failed: {fail_count}\n"
            f"Saved to: {out_root}"
        )
        tail = "\n".join(logs[-50:])  # last 50 lines
        return summary + ("\n" + tail if tail else "")


def parse_allow_ext(s: str) -> set[str]:
    return set(x.strip().lower().lstrip(".") for x in s.split(",") if x.strip())


def build_settings_from_env_and_args(args: argparse.Namespace) -> Settings:
    tor_proxy = (args.tor_proxy or os.getenv("TOR_PROXY", "")).strip()
    max_mb = int(args.max_mb or os.getenv("MAX_MB", "150"))
    allow_ext = parse_allow_ext(args.allow_ext or os.getenv("ALLOWED_EXT", "pdf,txt,jpg,jpeg,png,zip"))
    max_files = int(args.max_files or os.getenv("MAX_FILES", "200"))
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
    p.add_argument("--mode", required=True, choices=["size", "count", "download"])
    p.add_argument("--url", required=True, help="onion directory URL")
    p.add_argument("--ext", default=None, help="extension filter for count")
    p.add_argument("--out", default="downloads", help="output directory for download")
    p.add_argument("--max-mb", default=None)
    p.add_argument("--allow-ext", default=None, help="csv list, e.g. pdf,zip,png")
    p.add_argument("--tor-proxy", default=None, help="socks5h://host:port")
    p.add_argument("--max-files", default=None)
    p.add_argument("--max-depth", default=None)
    p.add_argument("--timeout", default=None)
    args = p.parse_args()

    if not is_onion_url(args.url):
        print("ERROR: url must be a valid .onion URL", file=sys.stderr)
        return 2

    settings = build_settings_from_env_and_args(args)

    # Validate socks dependency if proxy is set
    if settings.tor_proxy and not ProxyConnector:
        print("ERROR: TOR_PROXY set but aiohttp-socks is not installed", file=sys.stderr)
        return 3

    try:
        if args.mode == "count":
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
