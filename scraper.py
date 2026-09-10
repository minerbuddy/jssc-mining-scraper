from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


# ---------------------------------------------------------
# Configuration
# ---------------------------------------------------------

BASE_URL = "https://jssc.jharkhand.gov.in"
START_URL = f"{BASE_URL}/whats-new"

NEWS_FILE = Path("news.json")
PDF_DIR = Path("pdf")
LOG_DIR = Path("logs")
LOG_FILE = LOG_DIR / "scraper.log"

MAX_PAGES = 20
REQUEST_DELAY_SECONDS = 1.0
DOWNLOAD_PDFS = True

KEYWORDS = [
    "mine",
    "mines",
    "mining",
    "mine inspector",
    "mining inspector",
    "mine officer",
    "mining officer",
    "assistant mining officer",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 "
        "Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
}


# ---------------------------------------------------------
# Setup
# ---------------------------------------------------------

PDF_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

session = requests.Session()

retry_strategy = Retry(
    total=5,
    connect=5,
    read=5,
    backoff_factor=1.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "HEAD"],
)

session.mount(
    "https://",
    HTTPAdapter(max_retries=retry_strategy),
)

session.headers.update(HEADERS)


# ---------------------------------------------------------
# Utility functions
# ---------------------------------------------------------

def clean_text(value: str) -> str:
    return re.sub(r"s+", " ", value or "").strip()


def normalize_url(url: str) -> str:
    parsed = urlparse(url)

    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            parsed.path,
            "",
            "",
            "",
        )
    )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha1_text(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def is_pdf_url(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".pdf")


def get_keywords(text: str) -> list[str]:
    text = clean_text(text).lower()
    return [
        keyword
        for keyword in KEYWORDS
        if keyword.lower() in text
    ]


def request_get(url: str) -> requests.Response:
    response = session.get(url, timeout=45)
    response.raise_for_status()

    time.sleep(REQUEST_DELAY_SECONDS)
    return response


def parse_date(text: str) -> str | None:
    text = clean_text(text)

    patterns = [
        r"\bd{1,2}[/-]d{1,2}[/-]d{2,4}\b",
        r"\bd{1,2}s+[A-Za-z]{3,9}s+d{4}\b",
        r"\b[A-Za-z]{3,9}s+d{1,2},s+d{4}\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, text)

        if not match:
            continue

        try:
            parsed = date_parser.parse(match.group())
            return parsed.date().isoformat()
        except (ValueError, OverflowError):
            continue

    return None


def create_pdf_filename(title: str, url: str) -> str:
    safe_title = re.sub(
        r"[^a-zA-Z0-9]+",
        "-",
        title.lower(),
    ).strip("-")

    safe_title = safe_title[:90] or "jssc-notice"
    url_hash = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]

    return f"{safe_title}-{url_hash}.pdf"


def make_record_id(title: str, url: str) -> str:
    normalized = normalize_url(url)
    return sha1_text(f"{title.strip().lower()}|{normalized}")


# ---------------------------------------------------------
# Safe file operations
# ---------------------------------------------------------

def atomic_write_json(path: Path, data) -> None:
    """
    File ko temporary file me likhta hai.
    Error hone par original file untouched rahegi.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, temporary_name = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )

    temporary_path = Path(temporary_name)

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as file:
            json.dump(
                data,
                file,
                ensure_ascii=False,
                indent=2,
            )
            file.flush()
            os.fsync(file.fileno())

        os.replace(temporary_path, path)

    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def create_file_without_overwrite(
    path: Path,
    content: bytes,
) -> bool:
    """
    File sirf tab create hogi jab pehle se exist na karti ho.
    Existing file kabhi overwrite nahi hogi.
    """
    path.parent.mkdir(parents=True, exist_ok=True)

    fd = None
    temporary_path = None

    try:
        fd, temporary_name = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".part",
        )

        temporary_path = Path(temporary_name)

        with os.fdopen(fd, "wb") as file:
            fd = None
            file.write(content)
            file.flush()
            os.fsync(file.fileno())

        try:
            # Existing target hone par error raise hoga.
            os.link(temporary_path, path)
            temporary_path.unlink(missing_ok=True)
            return True

        except FileExistsError:
            temporary_path.unlink(missing_ok=True)
            return False

    finally:
        if fd is not None:
            os.close(fd)

        if temporary_path:
            temporary_path.unlink(missing_ok=True)


# ---------------------------------------------------------
# JSON handling
# ---------------------------------------------------------

def load_existing_news() -> list[dict]:
    if not NEWS_FILE.exists():
        return []

    try:
        raw_text = NEWS_FILE.read_text(encoding="utf-8").strip()

        if not raw_text:
            logging.warning(
                "news.json empty hai, empty list se start kar rahe hain"
            )
            return []

        data = json.loads(raw_text)

        if not isinstance(data, list):
            raise ValueError("news.json list format me nahi hai")

        return [
            item
            for item in data
            if isinstance(item, dict)
        ]

    except json.JSONDecodeError as exc:
        logging.error(
            "news.json corrupt/invalid JSON hai: %s. Empty list se aage badh rahe hain.",
            exc,
        )
        return []

    except Exception as exc:
        logging.error(
            "Existing news.json read nahi hua: %s",
            exc,
        )
        raise


def merge_without_delete(
    old_records: list[dict],
    new_records: list[dict],
) -> tuple[list[dict], int]:
    """
    Existing record ko modify nahi karta.
    Sirf naye IDs add karta hai.
    """
    existing_ids = {
        item.get("id")
        for item in old_records
        if item.get("id")
    }

    final_records = list(old_records)
    added_count = 0

    for item in new_records:
        item_id = item.get("id")

        if not item_id:
            continue

        if item_id in existing_ids:
            logging.info(
                "Already exists, skipped: %s",
                item.get("title"),
            )
            continue

        final_records.append(item)
        existing_ids.add(item_id)
        added_count += 1

    # Latest notice top par.
    # Records delete nahi hote; sirf display order change hota hai.
    final_records.sort(
        key=lambda item: (
            item.get("notice_date")
            or item.get("published_date")
            or "0000-00-00"
        ),
        reverse=True,
    )

    return final_records, added_count


# ---------------------------------------------------------
# PDF handling
# ---------------------------------------------------------

def download_pdf_if_new(
    url: str,
    title: str,
) -> dict:
    filename = create_pdf_filename(title, url)
    destination = PDF_DIR / filename

    # Existing PDF pehle check.
    if destination.exists():
        content = destination.read_bytes()

        return {
            "downloaded_now": False,
            "already_exists": True,
            "local_path": str(destination),
            "size_bytes": len(content),
            "sha256": sha256_bytes(content),
        }

    try:
        response = request_get(url)
        content = response.content

        if not content.startswith(b"%PDF"):
            logging.warning(
                "PDF response nahi mila: %s",
                url,
            )

            return {
                "downloaded_now": False,
                "already_exists": False,
                "local_path": None,
                "size_bytes": len(content),
                "sha256": sha256_bytes(content),
                "error": "Response PDF nahi hai",
            }

        created = create_file_without_overwrite(
            destination,
            content,
        )

        if not created:
            existing_content = destination.read_bytes()

            return {
                "downloaded_now": False,
                "already_exists": True,
                "local_path": str(destination),
                "size_bytes": len(existing_content),
                "sha256": sha256_bytes(existing_content),
            }

        logging.info("New PDF added: %s", destination)

        return {
            "downloaded_now": True,
            "already_exists": False,
            "local_path": str(destination),
            "size_bytes": len(content),
            "sha256": sha256_bytes(content),
        }

    except requests.RequestException as exc:
        logging.error(
            "PDF download failed: %s | %s",
            url,
            exc,
        )

        return {
            "downloaded_now": False,
            "already_exists": False,
            "local_path": None,
            "size_bytes": 0,
            "sha256": None,
            "error": str(exc),
        }


# ---------------------------------------------------------
# Scraping
# ---------------------------------------------------------

def extract_links(
    page_url: str,
    html: str,
) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    results = []

    for anchor in soup.select("a[href]"):
        title = clean_text(
            anchor.get_text(" ", strip=True)
        )

        href = urljoin(
            page_url,
            anchor.get("href", ""),
        )

        if not title:
            continue

        if href.startswith(
            ("javascript:", "mailto:", "#")
        ):
            continue

        parsed = urlparse(href)

        if parsed.scheme not in ("http", "https"):
            continue

        results.append(
            {
                "title": title,
                "url": normalize_url(href),
            }
        )

    # Same links ko current page ke andar duplicate nahi karega.
    unique = {}
    for item in results:
        unique[(item["title"], item["url"])] = item

    return list(unique.values())


def get_whats_new_page(page_number: int) -> list[dict]:
    if page_number == 0:
        page_url = START_URL
    else:
        page_url = f"{START_URL}?page={page_number}"

    logging.info("Crawling: %s", page_url)

    response = request_get(page_url)

    return extract_links(
        page_url,
        response.text,
    )


def scrape_detail_page(
    title: str,
    url: str,
) -> dict:
    # Direct PDF
    if is_pdf_url(url):
        pdf_info = {}

        if DOWNLOAD_PDFS:
            pdf_info[url] = download_pdf_if_new(
                url,
                title,
            )

        return {
            "page_title": title,
            "notice_date": None,
            "snippet": "",
            "pdf_urls": [url],
            "pdf_info": pdf_info,
        }

    response = request_get(url)
    soup = BeautifulSoup(response.text, "html.parser")

    page_title = title

    if soup.title:
        page_title = clean_text(
            soup.title.get_text(" ", strip=True)
        )

    body_text = clean_text(
        soup.get_text(" ", strip=True)
    )

    notice_date = parse_date(body_text)

    pdf_urls = []

    for anchor in soup.select("a[href]"):
        href = urljoin(
            url,
            anchor.get("href", ""),
        )

        if is_pdf_url(href):
            pdf_urls.append(normalize_url(href))

    pdf_urls = list(dict.fromkeys(pdf_urls))
    pdf_info = {}

    if DOWNLOAD_PDFS:
        for pdf_url in pdf_urls:
            pdf_info[pdf_url] = download_pdf_if_new(
                pdf_url,
                page_title,
            )

    return {
        "page_title": page_title,
        "notice_date": notice_date,
        "snippet": body_text[:700],
        "pdf_urls": pdf_urls,
        "pdf_info": pdf_info,
    }


# ---------------------------------------------------------
# Main
# ---------------------------------------------------------

def main():
    logging.info("Scraper started")

    old_records = load_existing_news()
    discovered_records = []
    seen_urls = set()

    for page_number in range(MAX_PAGES):
        try:
            links = get_whats_new_page(page_number)

        except requests.RequestException as exc:
            logging.error(
                "What's New page failed: %s",
                exc,
            )
            continue

        if not links:
            logging.info(
                "No links found on page %d",
                page_number,
            )
            break

        for link in links:
            title = link["title"]
            url = link["url"]

            if url in seen_urls:
                continue

            seen_urls.add(url)

            try:
                detail = scrape_detail_page(
                    title,
                    url,
                )

                combined_text = " ".join(
                    [
                        title,
                        detail["page_title"],
                        detail["snippet"],
                    ]
                )

                matched_keywords = get_keywords(
                    combined_text
                )

                if not matched_keywords:
                    continue

                now = datetime.now(
                    timezone.utc
                ).astimezone()

                record = {
                    "id": make_record_id(
                        title,
                        url,
                    ),
                    "title": title,
                    "page_title": detail["page_title"],
                    "url": url,
                    "pdf_urls": detail["pdf_urls"],
                    "notice_date": detail["notice_date"],
                    "published_date": (
                        detail["notice_date"]
                        or now.date().isoformat()
                    ),
                    "current_date": now.date().isoformat(),
                    "current_datetime": now.isoformat(),
                    "category": "mining",
                    "source": "JSSC",
                    "source_section": "whats-new",
                    "matched_keywords": matched_keywords,
                    "snippet": detail["snippet"],
                    "pdf_info": detail["pdf_info"],
                    "first_seen": now.isoformat(),
                }

                discovered_records.append(record)

                logging.info(
                    "Mining item found: %s",
                    title,
                )

            except requests.RequestException as exc:
                logging.error(
                    "Detail page failed: %s | %s",
                    url,
                    exc,
                )

    final_records, added_count = merge_without_delete(
        old_records,
        discovered_records,
    )

    # Agar new record nahi hai to news.json rewrite nahi hogi.
    if added_count > 0 or not NEWS_FILE.exists():
        atomic_write_json(
            NEWS_FILE,
            final_records,
        )
        logging.info(
            "news.json updated; new records: %d",
            added_count,
        )
    else:
        logging.info(
            "No new record; news.json untouched"
        )

    logging.info(
        "Old records: %d | Discovered: %d | Total: %d",
        len(old_records),
        len(discovered_records),
        len(final_records),
    )

    logging.info("Scraper finished")


if __name__ == "__main__":
    main()
