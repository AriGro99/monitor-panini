#!/usr/bin/env python3
"""
monitor-panini  –  Detecta productos Panini FIFA Mundial 2026
Monitorea: Zonakids, Tienda Panini (Magento), ML Tienda Panini (HTML scraping)
"""

import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup

# -------- Configuracion --------
TG_TOKEN  = os.environ["TG_TOKEN"]
TG_CHAT   = os.environ["TG_CHAT"]
STATE_FILE = "state.json"

# Tienda Panini en MercadoLibre Argentina – scraping HTML paginado
ML_STORE_URL = "https://www.mercadolibre.com.ar/tienda/panini"
ML_STORE_PAGES = 4   # cuantas paginas scrapear (48 items/pag = 192 productos)

# Headers que simulan un browser real (evita bloqueos de ML)
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


# -------- Estado persistente --------

def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, dict):
                return {}
            return data
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
    payload = {
        "chat_id": TG_CHAT,
        "text": msg,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    for attempt in range(3):
        try:
            r = requests.post(url, json=payload, timeout=10)
            r.raise_for_status()
            return
        except Exception as e:
            if attempt == 2:
                print(f"[TG] Error al enviar mensaje: {e}", flush=True)
            time.sleep(2)


# -------- HTTP helpers --------

def fetch_html(url: str, extra_headers: dict | None = None, retries: int = 3) -> str:
    """Descarga HTML con headers de browser. Retries incluidos."""
    headers = dict(BROWSER_HEADERS)
    if extra_headers:
        headers.update(extra_headers)
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=headers, timeout=20)
            r.raise_for_status()
            return r.text
        except Exception as e:
            print(f"[fetch_html] intento {attempt+1}/{retries} fallo: {e}", flush=True)
            if attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
    raise RuntimeError(f"No se pudo obtener HTML de {url} tras {retries} intentos")


def fetch_json(url: str, retries: int = 3) -> dict | list:
    """Descarga JSON sin headers especiales (para APIs que no requieren autenticacion)."""
    for attempt in range(retries):
        try:
            r = requests.get(url, timeout=15)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            print(f"[fetch_json] intento {attempt+1}/{retries} fallo: {e}", flush=True)
            if attempt < retries - 1:
                time.sleep(3)
    raise RuntimeError(f"No se pudo obtener JSON de {url} tras {retries} intentos")


# -------- Parsers de cada fuente --------

def parse_zonakids(url: str) -> dict:
    """Magento store – paginacion via ?p=N"""
    products: dict = {}
    page = 1
    while True:
        purl = f"{url}?p={page}" if page > 1 else url
        html_text = fetch_html(purl)
        soup = BeautifulSoup(html_text, "html.parser")
        items = soup.select(".product-item, .item.product")
        if not items:
            break
        found_new = False
        for item in items:
            a = item.select_one("a.product-item-link, a[class*='product']") or item.find("a", href=True)
            if not a:
                continue
            name = a.get_text(strip=True)
            href = a.get("href", "")
            if not name or not href:
                continue
            pid = re.sub(r"[^a-z0-9]", "", name.lower())[:40]
            if pid and pid not in products:
                products[pid] = {
                    "name": html.unescape(name),
                    "url": href,
                    "price": "",
                    "source": "Zonakids",
                }
                found_new = True
        if not found_new:
            break
        page += 1
        if page > 10:
            break
        time.sleep(0.5)
    return products


def parse_tienda_panini(url: str) -> dict:
    """Magento store Tienda Panini – paginacion via ?p=N"""
    products: dict = {}
    page = 1
    while True:
        purl = f"{url}?p={page}" if page > 1 else url
        html_text = fetch_html(purl)
        soup = BeautifulSoup(html_text, "html.parser")
        items = soup.select(".product-item, .item.product")
        if not items:
            break
        found_new = False
        for item in items:
            a = item.select_one("a.product-item-link, a[class*='product']") or item.find("a", href=True)
            if not a:
                continue
            name = a.get_text(strip=True)
            href = a.get("href", "")
            if not name or not href:
                continue
            pid = re.sub(r"[^a-z0-9]", "", name.lower())[:40]
            price_el = item.select_one(".price")
            price = price_el.get_text(strip=True) if price_el else ""
            if pid and pid not in products:
                products[pid] = {
                    "name": html.unescape(name),
                    "url": href,
                    "price": price,
                    "source": "Tienda Panini",
                }
                found_new = True
        if not found_new:
            break
        page += 1
        if page > 10:
            break
        time.sleep(0.5)
    return products


def _ml_page_url(page_num: int) -> str:
    """Genera la URL de paginacion de la tienda Panini en ML.
    ML usa offset 48 por pagina: _Desde_49_, _Desde_97_, etc.
    """
    if page_num == 1:
        return ML_STORE_URL
    offset = (page_num - 1) * 48 + 1
    return f"{ML_STORE_URL}/_Desde_{offset}_NoIndex_True"


def parse_ml_tienda(url: str) -> dict:
    """
    Scraping HTML paginado de la Tienda Panini en MercadoLibre.
    Ignora la API REST (requiere OAuth, siempre 403).
    Descarga ML_STORE_PAGES paginas con User-Agent de browser.
    """
    products: dict = {}
    pages_fetched = 0

    for page_num in range(1, ML_STORE_PAGES + 1):
        page_url = _ml_page_url(page_num)
        try:
            html_text = fetch_html(page_url)
        except Exception as e:
            print(f"[ML HTML] pagina {page_num} fallo: {e}", flush=True)
            break

        soup = BeautifulSoup(html_text, "html.parser")

        # Selectores de productos en ML (tienda oficial)
        items = soup.select(
            ".ui-search-result__wrapper, "
            ".andes-card--flat, "
            "li.ui-search-layout__item"
        )

        if not items:
            # Intentar fallback con selector generico de links MLA
            items = [None]  # trigger fallback below

        found_new = 0

        if items == [None]:
            # Fallback: buscar todos los links con MLA en href
            for a in soup.find_all("a", href=True):
                href = a["href"]
                m = re.search(r"(MLA-?\d+)", href)
                if not m:
                    continue
                pid = m.group(1).replace("-", "")
                if pid in products:
                    continue
                name = a.get_text(strip=True)
                if not name:
                    img = a.find("img")
                    name = img["alt"] if img and img.get("alt") else ""
                if not name:
                    continue
                # Clean URL (remove tracking params)
                clean_href = href.split("?")[0].split("#")[0]
                products[pid] = {
                    "name": html.unescape(name.strip()),
                    "url": clean_href,
                    "price": "",
                    "source": "ML Tienda Panini",
                }
                found_new += 1
        else:
            for item in items:
                # Nombre del producto
                name_el = item.select_one(
                    ".ui-search-item__title, "
                    "[class*='title'], "
                    "h2, h3"
                )
                name = name_el.get_text(strip=True) if name_el else ""

                # Link del producto
                a = item.find("a", href=True)
                if not a:
                    continue
                href = a["href"]
                m = re.search(r"(MLA-?\d+)", href)
                if not m:
                    continue
                pid = m.group(1).replace("-", "")

                if not name:
                    img = item.find("img")
                    name = img["alt"] if img and img.get("alt") else ""
                if not name or pid in products:
                    continue

                # Precio
                price_el = item.select_one(
                    ".andes-money-amount__fraction, "
                    ".price-tag-fraction, "
                    "[class*='price']"
                )
                price = price_el.get_text(strip=True) if price_el else ""

                clean_href = href.split("?")[0].split("#")[0]
                products[pid] = {
                    "name": html.unescape(name.strip()),
                    "url": clean_href,
                    "price": price,
                    "source": "ML Tienda Panini",
                }
                found_new += 1

        pages_fetched += 1
        print(f"[ML HTML] pagina {page_num}: {found_new} nuevos, total {len(products)}", flush=True)

        if found_new == 0 and pages_fetched > 1:
            # Nada nuevo en esta pagina, probablemente llegamos al final
            break

        if page_num < ML_STORE_PAGES:
            time.sleep(1)

    print(f"[ML Tienda Panini] {len(products)} productos via HTML scraping ({pages_fetched} paginas)", flush=True)
    return products


# -------- SOURCES --------

SOURCES = [
    {
        "name": "Zonakids",
        "url": "https://www.zonakids.com.ar/catalogsearch/result/?q=panini",
        "parser": parse_zonakids,
    },
    {
        "name": "Tienda Panini",
        "url": "https://tienda.panini.com.ar/catalogsearch/result/?q=album",
        "parser": parse_tienda_panini,
    },
    {
        "name": "ML Tienda Panini",
        "url": ML_STORE_URL,
        "parser": parse_ml_tienda,
    },
]


# -------- Comparacion y alertas --------

def check_source(source: dict, state: dict) -> tuple[list[str], list[str]]:
    """
    Chequea una fuente. Devuelve (alertas, errores).
    alertas = lista de mensajes Telegram.
    errores = lista de strings descriptivos.
    """
    name = source["name"]
    url  = source["url"]
    alerts: list[str] = []
    errors: list[str] = []

    try:
        current = source["parser"](url)
    except Exception as e:
        err = f"{name}: {e}"
        errors.append(err)
        print(f"[ERROR] {err}", flush=True)
        return alerts, errors

    print(f"{name} \u2192 {len(current)} productos", flush=True)

    prev = state.get(name, {})
    first_run = not prev

    matched: list[dict] = []

    for pid, prod in current.items():
        if not matches_keywords(prod["name"]):
            continue

        prev_prod = prev.get(pid)

        if prev_prod is None:
            if not first_run:
                prod["_alert_type"] = "nuevo"
            matched.append((pid, prod))
        else:
            # Chequear restock
            was_unavailable = prev_prod.get("unavailable", False)
            if was_unavailable:
                prod["_alert_type"] = "restock"
                matched.append((pid, prod))
            # Chequear cambio de precio
            elif prod.get("price") and prev_prod.get("price") and prod["price"] != prev_prod["price"]:
                prod["_alert_type"] = "precio"
                prod["_old_price"] = prev_prod["price"]
                matched.append((pid, prod))

    if matched and not first_run:
        msgs: list[str] = []
        for pid, prod in matched:
            atype = prod.get("_alert_type", "nuevo")
            pname = prod["name"]
            purl  = prod["url"]
            price = prod.get("price", "")

            if atype == "nuevo":
                header = "\U0001F6A8\u00a1NUEVO PRODUCTO DETECTADO!"
            elif atype == "restock":
                header = "\U0001F514 RESTOCK DETECTADO"
            else:
                header = "\U0001F4B0 CAMBIO DE PRECIO"

            price_line = f"\n\U0001F4B2 Precio: {price}" if price else ""
            if atype == "precio":
                price_line = f"\n\U0001F4B2 Precio: {prod.get('_old_price','')} \u2192 {price}"

            msg = (
                f"{header}\n"
                f"\U0001F3EA Tienda: {name}\n"
                f"\U0001F4E6 Producto: <b>{pname}</b>{price_line}\n"
                f"\U0001F517 <a href='{purl}'>VER AHORA \u27a1</a>\n"
                f"\u23F0 {datetime.now(timezone.utc).strftime('%H:%M:%S UTC')}"
            )
            msgs.append(msg)
        alerts.append("\n\n".join(msgs))

    if first_run and matched:
        names_list = ", ".join(p["name"] for _, p in matched[:5])
        summary = (
            f"\u2705 <b>Primera ejecucion – {name}</b>\n"
            f"Productos con keywords: {len(matched)}\n"
            f"Ejemplos: {names_list}"
        )
        alerts.append(summary)

    # Actualizar estado
    state[name] = current

    return alerts, errors


# -------- Loop principal --------

def run_check() -> tuple[int, int]:
    """Ejecuta un ciclo completo. Devuelve (n_alertas, n_errores)."""
    state = load_state()

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
        err_msg = "\u26A0\uFE0F <b>Errores en el monitor</b>\n" + "\n".join(f"\u2022 {e}" for e in all_errors)
        tg_send(err_msg)

    return len(all_alerts), len(all_errors)


def main() -> None:
    runs   = int(os.environ.get("RUNS", "3"))
    sleep_s = int(os.environ.get("SLEEP", "90"))

    for i in range(1, runs + 1):
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{ts}] [Run {i}/{runs}] Procesando {len(SOURCES)} sitios en paralelo...", flush=True)
        n_al, n_er = run_check()
        print(f"[{ts}] [Run {i}/{runs}] Listo. {n_al} alertas, {n_er} errores.", flush=True)
        if i < runs:
            time.sleep(sleep_s)


if __name__ == "__main__":
    main()
