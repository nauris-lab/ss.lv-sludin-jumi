#!/usr/bin/env python3
"""
SS.LV -> Telegram watcher.

Reads the RSS feeds of the ss.lv search pages listed in feeds.txt, detects
new listings, and sends them to a Telegram chat. Already-seen listings are
remembered in state.json so nothing is ever sent twice.

Environment variables (set as GitHub Actions Secrets):
    TELEGRAM_BOT_TOKEN  - token from @BotFather
    TELEGRAM_CHAT_ID    - chat id of the person who should receive the messages
"""

import os
import re
import sys
import json
import time
import html
from pathlib import Path

import requests
import feedparser

# --- Configuration ----------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
FEEDS_FILE = BASE_DIR / "feeds.txt"
STATE_FILE = BASE_DIR / "state.json"

MAX_SEEN = 4000          # keep only the newest N listing ids (bounds file size)
SEND_DELAY = 0.6         # pause between Telegram messages (seconds)
REQUEST_TIMEOUT = 30     # seconds

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"
    )
}

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
# One or more chat ids, separated by commas or spaces, e.g. "12345,67890".
CHAT_IDS = [c for c in re.split(r"[\s,]+", os.environ.get("TELEGRAM_CHAT_ID", "")) if c]

TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"[ \t\r\f\v]+")
IMG_RE = re.compile(r'<img[^>]+src=["\']([^"\']+)["\']', re.IGNORECASE)


# --- Feeds & state ----------------------------------------------------------

def load_feeds():
    """Read feeds.txt and return normalised RSS URLs (each ends with /rss/)."""
    feeds = []
    for line in FEEDS_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        url = line.rstrip("/")
        if not url.endswith("/rss"):
            url += "/rss"
        feeds.append(url + "/")
    return feeds


def load_state():
    """Return (seen_list, is_bootstrap). Bootstrap = first ever run."""
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            return list(data.get("seen", [])), False
        except Exception as e:
            print(f"  ! could not read state.json ({e}); starting fresh",
                  file=sys.stderr)
    return [], True


def save_state(seen):
    seen = seen[-MAX_SEEN:]
    STATE_FILE.write_text(
        json.dumps({"seen": seen}, ensure_ascii=False),
        encoding="utf-8",
    )


# --- Parsing ----------------------------------------------------------------

def fetch_feed(url):
    r = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return feedparser.parse(r.content)


def clean_description(desc):
    """Strip HTML from the RSS description into one readable line."""
    if not desc:
        return ""
    text = html.unescape(TAG_RE.sub(" ", desc))
    text = text.replace("Apskatīt sludinājumu", "")  # drop leftover link text
    lines = [WS_RE.sub(" ", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return " | ".join(lines).strip(" |")


def extract_image(desc):
    m = IMG_RE.search(desc or "")
    return m.group(1) if m else None


def build_message(entry):
    """Return (html_text, image_url) for one listing."""
    raw_summary = entry.get("summary") or entry.get("description") or ""
    title = html.unescape(TAG_RE.sub("", entry.get("title", ""))).strip()
    link = entry.get("link", "").strip()
    details = clean_description(raw_summary)
    if len(details) > 320:
        details = details[:317] + "..."

    parts = []
    if title:
        parts.append(f"🏡 <b>{html.escape(title)}</b>")
    if details:
        parts.append(html.escape(details))
    if link:
        parts.append(f'👉 <a href="{html.escape(link)}">Atvērt sludinājumu (ss.lv)</a>')
    return "\n\n".join(parts), extract_image(raw_summary)


# --- Telegram ---------------------------------------------------------------

def _send_one(chat_id, text_html, image_url):
    """Send a single listing to one chat. Try photo first, fall back to text."""
    if image_url:
        api = f"https://api.telegram.org/bot{TOKEN}/sendPhoto"
        payload = {
            "chat_id": chat_id,
            "photo": image_url,
            "caption": text_html[:1024],
            "parse_mode": "HTML",
        }
        r = requests.post(api, data=payload, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            return True
        print(f"  ! sendPhoto {r.status_code} -> {chat_id}: {r.text[:180]}",
              file=sys.stderr)

    api = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text_html,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    r = requests.post(api, data=payload, timeout=REQUEST_TIMEOUT)
    if r.status_code != 200:
        print(f"  ! sendMessage {r.status_code} -> {chat_id}: {r.text[:180]}",
              file=sys.stderr)
    return r.status_code == 200


def send(text_html, image_url):
    """Send one listing to every configured chat id."""
    ok = False
    for chat_id in CHAT_IDS:
        if _send_one(chat_id, text_html, image_url):
            ok = True
        time.sleep(0.1)  # small gap between recipients
    return ok


# --- Main -------------------------------------------------------------------

def main():
    if not TOKEN or not CHAT_IDS:
        print("ERROR: set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID", file=sys.stderr)
        sys.exit(1)

    feeds = load_feeds()
    seen, bootstrap = load_state()
    seen_set = set(seen)

    if bootstrap:
        print("First run (bootstrap): recording current listings WITHOUT sending.")

    fresh = []  # list of (text_html, image_url), newest-first
    for url in feeds:
        try:
            parsed = fetch_feed(url)
        except Exception as e:
            print(f"  ! failed to fetch {url}: {e}", file=sys.stderr)
            continue

        new_count = 0
        for entry in parsed.entries:
            link = entry.get("link", "").strip()
            if not link or link in seen_set:
                continue
            seen_set.add(link)
            seen.append(link)
            new_count += 1
            if not bootstrap:
                fresh.append(build_message(entry))
        print(f"  {url} -> {len(parsed.entries)} items, {new_count} new")

    # Send oldest-first so notifications arrive in chronological order.
    sent = 0
    for text_html, image_url in reversed(fresh):
        if send(text_html, image_url):
            sent += 1
        time.sleep(SEND_DELAY)

    save_state(seen)
    print(f"Done. {sent} message(s) sent, {len(seen[-MAX_SEEN:])} ids tracked.")


if __name__ == "__main__":
    main()
