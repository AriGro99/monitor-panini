from __future__ import annotations

import html
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

# -------- Config --------
STATE_FILE = Path(__file__).parent / "state.json"
TIMEOUT = 25

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
}

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

SITES = [
    {"key": "zonakids", "name": "Zonakids", "url": "https://zonakids.com/", "parser": "magento"},
    {"key": "tiendapanini", "name": "Tienda Panini", "url": "https://tiendapanini.com.ar/", "parser": "magento"},
    {"key": "ml_panini", "name": "ML - Tienda Panini", "url": "https://www.mercadolibre.com.ar/tienda/panini", "parser": "meli"},
]

# -------- Filtro --------
KEYWORDS_ANY_OF = [
    ["mundial", "2026"],
    ["world cup", "2026"],
    ["fifa", "2026"],
]
KEYWORDS_EXCLUDE = ["adrenalyn"]


def matches_keywords(name: str) -> bool:
    n = name.lower()
    if any(excl in n for excl in KEYWORDS_EXCLUDE):
        return False
    if not KEYWORDS_ANY_OF:
        return True
    return any(all(tok in n for tok in group) for group in KEYWORDS_ANY_OF)


# -------- Utils --------

def log(msg: str) -> None:
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}", flush=True)


def fetch(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# -------- Parsers --------

def parse_magento(html_text: str, base_url: str) -> dict:
    soup = BeautifulSoup(html_text, "html.parser")
    products = {}

    for node in soup.select("li.product-item"):
        pid_el = node.find(attrs={"data-product-id": True})
        if not pid_el:
            continue

        pid = pid_el["data-product-id"]
        name_el = node.select_one("a.product-item-link")

        name = name_el.get_text(strip=True) if name_el else ""
        url = name_el["href"] if name_el else ""

        if url.startswith("/"):
            url = base_url.rstrip("/") + url

        price_el = node.select_one(".price")
        price = price_el.get_text(strip=True) if price_el else ""

        text = node.get_text().lower()
        in_stock = not any(x in text for x in ["agotado", "sin stock"])

        products[pid] = {
            "id": pid,
            "name": name,
            "url": url,
            "price": price,
            "in_stock": in_stock,
        }

    return products


def parse_meli(html_text: str, base_url: str) -> dict:
    soup = BeautifulSoup(html_text, "html.parser")
    products = {}

    for a in soup.find_all("a", href=True):
        href = a["href"]

        m = re.search(r"/MLA-(\d+)-", href)
        if not m:
            continue

        pid = "MLA" + m.group(1)
        if pid in products:
            continue

        name = a.get_text(strip=True)
        if not name:
            continue

        products[pid] = {
            "id": pid,
            "name": name,
            "url": href,
            "price": "",
            "in_stock": True,
        }

    return products


PARSERS = {"magento": parse_magento, "meli": parse_meli}


# -------- Telegram --------

def tg_send(text: str):
    if not TG_TOKEN or not TG_CHAT_ID:
        log("Telegram no configurado")
        return

    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"

    requests.post(
        url,
        json={
            "chat_id": TG_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
        },
        timeout=TIMEOUT,
    )


def fmt_alert(site, prod):
    return f"🆕 {site}\n{prod['name']}\n{prod['url']}\n{prod['price']}"


# -------- Main --------

def run():
    state = load_state()
    first_run = not state
    alerts = []

    for site in SITES:
        log(f"Procesando {site['name']}")

        html_text = fetch(site["url"])
        products = PARSERS[site["parser"]](html_text, site["url"])

        prev = state.get(site["key"], {}).get("products", {})

        for pid, prod in products.items():
            if not matches_keywords(prod["name"]):
                continue

            old = prev.get(pid)

            if old is None:
                alerts.append(fmt_alert(site["name"], prod))

        state[site["key"]] = {"products": products}

    if not first_run:
        for a in alerts:
            tg_send(a)
            time.sleep(1)

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(run())
