#!/usr/bin/env python3
"""
monitor-panini  -  Detecta productos Panini FIFA Mundial 2026
Monitorea: Zonakids, Tienda Panini (Magento), ML Tienda Panini
"""
import html
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

# -------- Configuracion --------
TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_CHAT  = os.environ["TELEGRAM_CHAT_ID"]
STATE_FILE = "state.json"

ML_STORE_URL   = "https://www.mercadolibre.com.ar/tienda/panini"
ML_STORE_PAGES = 4

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "es-AR,es;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

# -------- Filtro keywords --------
# Se aplica SOLO para decidir si alertar, nunca para descartar del estado
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


# -------- Estado persistente --------
def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_state(state: dict) -> None:
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


# -------- Telegram --------
def tg_send(msg: str) -> None:
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    payload = {"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML",
               "disable_web_page_preview": False}
    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, timeout=10)
            r.raise_for_status()
            return
        except Exception as e:
            if attempt == 2:
                print(f"[TG] Error: {e}", flush=True)
            time.sleep(2)


# -------- HTTP --------
def fetch_html(url: str, retries: int = 3) -> str:
    headers = dict(BROWSER_HEADERS)
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, timeout=20)
            r.raise_for_status()
            return r.text
        except Exception as e:
            print(f"[fetch] intento {attempt+1}/{retries} fallo: {e}", flush=True)
            if attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"No se pudo obtener HTML de {url} tras {retries} intentos")


# -------- Parser Magento generico (Zonakids + Tienda Panini) --------
def parse_magento(url: str, source_name: str) -> dict:
    """
    Scraping de tiendas Magento (Zonakids, Tienda Panini).
    Usa find_all() en lugar de select() para mayor robustez.
    Guarda TODOS los productos sin filtrar por keyword.
    """
    products: dict = {}
    page = 1
    while page <= 10:
        purl = f"{url}?p={page}" if page > 1 else url
        try:
            html_text = fetch_html(purl)
        except Exception as e:
            print(f"[{source_name}] p{page} error: {e}", flush=True)
            break

        soup = BeautifulSoup(html_text, "html.parser")

        # Intentar multiples selectores para maxima compatibilidad
        items = soup.find_all("li", class_="product-item")
        if not items:
            items = soup.find_all("li", class_=lambda c: c and "product-item" in c)
        print(f"[{source_name}] p{page}: {len(items)} items (html={len(html_text)}b)", flush=True)

        if not items:
            break

        found = 0
        for item in items:
            name_el = item.find("strong", class_="product-item-name")
            if not name_el:
                name_el = item.find(class_=lambda c: c and "product-item-name" in c)
            link_el = item.find("a", class_="product-item-link")
            if not link_el:
                link_el = item.find("a", href=True)
            if not name_el or not link_el:
                continue
            name = html.unescape(name_el.get_text(strip=True))
            href = link_el.get("href", "").strip()
            if not name or not href:
                continue
            pid = re.sub(r"[^a-z0-9]", "", name.lower())[:40]
            price_el = item.find(class_="price")
            price = price_el.get_text(strip=True) if price_el else ""
            if pid and pid not in products:
                products[pid] = {"name": name, "url": href, "price": price, "source": source_name}
                found += 1

        print(f"[{source_name}] p{page}: {found} nuevos, total {len(products)}", flush=True)
        if found == 0:
            break
        page += 1
        time.sleep(0.5)
    return products
def parse_ml_tienda(url: str) -> dict:
    """
    Scraping de la tienda oficial Panini en ML: /tienda/panini
    Selector: .poly-card (nuevo layout ML)
    Fallback: buscar MLA en hrefs.
    Guarda TODOS los productos.
    """
    products: dict = {}
    pages_fetched = 0

    for page_num in range(1, ML_STORE_PAGES + 1):
        page_url = _ml_page_url(page_num)
        try:
            html_text = fetch_html(page_url)
        except Exception as e:
            print(f"[ML] p{page_num} error: {e}", flush=True)
            break

        soup = BeautifulSoup(html_text, "html.parser")

        # Selector nuevo (poly-card) y selector viejo (ui-search-layout__item)
        items = soup.select(".poly-card, li.ui-search-layout__item, .andes-card--flat")
        found = 0

        if items:
            for item in items:
                title_el = item.select_one(
                    ".poly-component__title, .ui-search-item__title, "
                    "[class*='title'], h2, h3"
                )
                name = title_el.get_text(strip=True) if title_el else ""
                a = item.find("a", href=True)
                if not a:
                    continue
                href = a["href"]
                m = re.search(r"MLA[-]?d+", href)
                if not m:
                    continue
                pid = re.sub(r"[^a-z0-9]", "", m.group(0))
                if not name:
                    img = item.find("img")
                    name = img.get("alt", "").strip() if img else ""
                if not name or pid in products:
                    continue
                price_el = item.select_one(
                    ".poly-price__current, .andes-money-amount__fraction, "
                    ".price-tag-fraction, [class*='price']"
                )
                price = price_el.get_text(strip=True) if price_el else ""
                clean_href = href.split("?")[0]
                products[pid] = {
                    "name": html.unescape(name.strip()),
                    "url": clean_href, "price": price, "source": "ML Tienda Panini"
                }
                found += 1
        else:
            # Fallback: buscar MLA en cualquier link
            for a in soup.find_all("a", href=True):
                href = a["href"]
                m = re.search(r"MLA[-]?d+", href)
                if not m:
                    continue
                pid = re.sub(r"[^a-z0-9]", "", m.group(0))
                if pid in products:
                    continue
                name = a.get_text(strip=True)
                if not name:
                    img = a.find("img")
                    name = img.get("alt", "").strip() if img else ""
                if not name:
                    continue
                clean_href = href.split("?")[0]
                products[pid] = {
                    "name": html.unescape(name.strip()),
                    "url": clean_href, "price": "", "source": "ML Tienda Panini"
                }
                found += 1

        pages_fetched += 1
        print(f"[ML] p{page_num}: {found} nuevos, total {len(products)}", flush=True)
        if found == 0 and pages_fetched > 1:
            break
        if page_num < ML_STORE_PAGES:
            time.sleep(1)

    print(f"[ML Tienda Panini] {len(products)} productos ({pages_fetched} pags)", flush=True)
    return products


# -------- SOURCES --------
SOURCES = [
    {
        "name": "Zonakids",
        "url": "https://zonakids.com/",
        "parser": lambda url: parse_magento(url, "Zonakids"),
    },
    {
        "name": "Tienda Panini",
        "url": "https://tiendapanini.com.ar/",
        "parser": lambda url: parse_magento(url, "Tienda Panini"),
    },
    {
        "name": "ML Tienda Panini",
        "url": ML_STORE_URL,
        "parser": parse_ml_tienda,
    },
]


# -------- Logica de alertas --------
def check_source(source: dict, state: dict) -> tuple[list[str], list[str]]:
    name   = source["name"]
    url    = source["url"]
    alerts: list[str] = []
    errors: list[str] = []

    try:
        current = source["parser"](url)
    except Exception as e:
        err = f"{name}: {e}"
        errors.append(err)
        print(f"[ERROR] {err}", flush=True)
        return alerts, errors

    print(f"{name} -> {len(current)} productos totales", flush=True)

    prev      = state.get(name, {})
    first_run = not prev

    new_products   = []
    price_changes  = []
    restocks       = []

    for pid, prod in current.items():
        prev_prod = prev.get(pid)

        if prev_prod is None:
            # Producto nuevo
            if not first_run and matches_keywords(prod["name"]):
                new_products.append(prod)
        else:
            # Producto ya conocido - chequear restock
            if prev_prod.get("unavailable") and not prod.get("unavailable"):
                if matches_keywords(prod["name"]):
                    restocks.append(prod)
            # Chequear cambio de precio
            elif (prod.get("price") and prev_prod.get("price")
                    and prod["price"] != prev_prod["price"]
                    and matches_keywords(prod["name"])):
                prod["_old_price"] = prev_prod["price"]
                price_changes.append(prod)

    def fmt_product(prod: dict, atype: str) -> str:
        if atype == "nuevo":
            header = "\U0001F6A8 NUEVO PRODUCTO DETECTADO!"
        elif atype == "restock":
            header = "\U0001F514 RESTOCK DETECTADO"
        else:
            header = "\U0001F4B0 CAMBIO DE PRECIO"
        price = prod.get("price", "")
        price_line = f"\n\U0001F4B2 Precio: {price}" if price else ""
        if atype == "precio":
            price_line = f"\n\U0001F4B2 {prod.get('_old_price','')} -> {price}"
        return (
            f"{header}\n"
            f"\U0001F3EA Tienda: {name}\n"
            f"\U0001F4E6 Producto: <b>{prod['name']}</b>{price_line}\n"
            f"\U0001F517 <a href=\'{prod['url']}\'>VER AHORA</a>\n"
            f"\u23F0 {datetime.now(timezone.utc).strftime('%H:%M UTC')}"
        )

    all_msgs = (
        [fmt_product(p, "nuevo")  for p in new_products]  +
        [fmt_product(p, "restock") for p in restocks]      +
        [fmt_product(p, "precio") for p in price_changes]
    )
    if all_msgs:
        alerts.append("\n\n".join(all_msgs))

    if first_run:
        kw_count = sum(1 for p in current.values() if matches_keywords(p["name"]))
        summary = (
            f"\u2705 <b>Primera ejecucion - {name}</b>\n"
            f"Total productos: {len(current)}\n"
            f"Con keywords Mundial 2026: {kw_count}"
        )
        alerts.append(summary)

    # Guardar TODOS los productos en el estado
    state[name] = current
    return alerts, errors


# -------- Loop principal --------
def run_check() -> tuple[int, int]:
    state      = load_state()
    all_alerts: list[str] = []
    all_errors: list[str] = []

    with ThreadPoolExecutor(max_workers=len(SOURCES)) as ex:
        futures = {ex.submit(check_source, src, state): src["name"] for src in SOURCES}
        for fut in as_completed(futures):
            src_name = futures[fut]
            try:
                al, er = fut.result()
                all_alerts.extend(al)
                all_errors.extend(er)
            except Exception as e:
                all_errors.append(f"{src_name}: excepcion inesperada: {e}")

    save_state(state)

    for msg in all_alerts:
        tg_send(msg)

    if all_errors:
        err_msg = (
            "\u26A0\uFE0F <b>Errores en el monitor</b>\n"
            + "\n".join(f"\u2022 {e}" for e in all_errors)
        )
        tg_send(err_msg)

    return len(all_alerts), len(all_errors)


def main() -> None:
    runs    = int(os.environ.get("RUNS", "3"))
    sleep_s = int(os.environ.get("SLEEP", "90"))
    for i in range(1, runs + 1):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] [Run {i}/{runs}] Iniciando...", flush=True)
        n_al, n_er = run_check()
        print(f"[{ts}] [Run {i}/{runs}] {n_al} alertas, {n_er} errores.", flush=True)
        if i < runs:
            time.sleep(sleep_s)


if __name__ == "__main__":
    main()
