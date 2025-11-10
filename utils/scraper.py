# utils/scraper.py
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, quote
import re
from config import BASE_URL, SEARCH_URL, GALLERY_URL, IMAGE_BASE_URL

def safe_slug(term: str) -> str:
    return quote(term.lower().replace(" ", "-"))

async def search_manga(term: str, page: int = 1) -> list:
    url = SEARCH_URL.format(slug=safe_slug(term), page=page)
    try:
        res = requests.get(url, timeout=10)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "lxml")

        items = []
        for div in soup.select("div.manga-card"):
            a_tag = div.select_one("a.title")
            if not a_tag:
                continue

            title = a_tag.get_text(strip=True) or "No Title"
            href = a_tag.get("href", "")
            if not href.startswith("/g/"):
                continue

            # href: /g/12345/ → gallery_id = 12345
            match = re.search(r"/g/(\d+)/", href)
            if not match:
                continue
            gallery_id = match.group(1)

            items.append({
                "title": title,
                "gallery_id": gallery_id,
                "url": urljoin(BASE_URL, href)
            })
        return items
    except Exception as e:
        print(f"[SCRAPER] Search error: {e}")
        return []

async def fetch_gallery_metadata(gallery_id: str) -> dict:
    url = GALLERY_URL.format(gallery_id=gallery_id)
    try:
        res = requests.get(url, timeout=10)
        res.raise_for_status()
        soup = BeautifulSoup(res.text, "lxml")

        # Rasmlar sonini aniqlash (1/15 degani 15 ta rasm bor)
        counter = soup.select_one("div.pagination > span")
        total = 1
        if counter:
            text = counter.get_text(strip=True)
            match = re.search(r"\d+/(\d+)", text)
            if match:
                total = int(match.group(1))

        # Rasmlar papkasini HTML dan olish — masalan: <img src="https://pics.hentai.name/a/b/12345/001.webp">
        # Biroq to'g'ridan-to'g'ri src dan olish yoki JS qilish kerak.
        # Alternativ: <div id="image-container"> ichida <img> lar bor deb taxmin qilamiz.

        # Ma'lumotlar olish uchun HTML dan folder/subfolder topish:
        # Masalan: <script>...gallery_folder = "a"; gallery_subfolder = "b";...</script>
        script = soup.find("script", string=re.compile(r"gallery_folder"))
        folder = "a"
        subfolder = "b"
        if script:
            text = script.string
            folder_match = re.search(r'gallery_folder\s*=\s*"([^"]+)"', text)
            subfolder_match = re.search(r'gallery_subfolder\s*=\s*"([^"]+)"', text)
            if folder_match:
                folder = folder_match.group(1)
            if subfolder_match:
                subfolder = subfolder_match.group(1)

        return {
            "gallery_id": gallery_id,
            "total_images": total,
            "folder": folder,
            "subfolder": subfolder,
        }
    except Exception as e:
        print(f"[SCRAPER] Gallery fetch error for {gallery_id}: {e}")
        return {
            "gallery_id": gallery_id,
            "total_images": 1,
            "folder": "a",
            "subfolder": "b"
        }

def build_image_url(gallery_id: str, folder: str, subfolder: str, index: int) -> str:
    # 1-indexed → 001.webp
    num = f"{index:03d}"
    return f"{IMAGE_BASE_URL}/{folder}/{subfolder}/{gallery_id}/{num}.webp"
