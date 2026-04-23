from __future__ import annotations

import html
import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

# -------- Config --------
STATE_FILE = Path(__file__).parent / "state.json"
TIMEOUT = 15
MAX_RETRIES = 3

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
}

TG_TOKEN  = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
RUN_INDEX  = os.environ.get("RUN_INDEX", "1")
IS_HEARTBEAT = os.environ.get("HEARTBEAT", "").lower() == "true"

SITES = [
    {"key": "zonakids",     "name": "Zonakids",          "url": "https://zonakids.com/",                         "parser": "magento"},
    {"key": "tiendapanini", "name": "Tienda Panini",      "url": "https://tiendapanini.com.ar/",                  "parser": "magento"},
    {"key": "ml_panini",    "name": "ML - Tienda Panini", "url": "https://www.mercadolibre.com.ar/tienda/panini", "parser": "meli"},
]

ML_SELLER_ID = "166666537"
ML_API_URL   = f"https://api.mercadolibre.com/sites/MLA/search?seller_id={ML_SELLER_ID}&limit=50"

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
    """GET con reintentos (backoff lineal)."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:
            log(f"[fetch] intento {attempt}/{MAX_RETRIES} falló: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(attempt * 3)
    raise RuntimeError(f"No se pudo obtener {url} tras {MAX_RETRIES} intentos")


def fetch_json(url: str):
    """GET JSON con reintentos."""
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = requests.get(url, timeout=TIMEOUT)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            log(f"[fetch_json] intento {attempt}/{MAX_RETRIES} falló: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(attempt * 3)
    raise RuntimeError(f"No se pudo obtener JSON de {url} tras {MAX_RETRIES} intentos")
def load_state() -> dict[str, Any]:
    """Carga state.json; lo resetea si está corrupto."""
    if STATE_FILE.exists():
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
            log("[state] JSON inválido (no es dict), reseteando")
        except json.JSONDecodeError as e:
            log(f"[state] JSON corrupto: {e} — reseteando")
    return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# -------- Parsers --------

def parse_magento(html_text: str, base_url: str) -> dict:
    """Parsea tiendas Magento siguiendo paginación automáticamente."""
    products: dict = {}
    page = 1

    while True:
        url = f"{base_url.rstrip('/')}/?p={page}" if page > 1 else base_url
        try:
            text = html_text if page == 1 else fetch(url)
        except RuntimeError:
            break

        soup = BeautifulSoup(text, "html.parser")
        items = soup.select("li.product-item")

        if not items:
            break

        for node in items:
            pid_el = node.find(attrs={"data-product-id": True})
            if not pid_el:
                continue

            pid      = pid_el["data-product-id"]
            name_el  = node.select_one("a.product-item-link")
            name     = name_el.get_text(strip=True) if name_el else ""
            url_prod = name_el["href"] if name_el else ""

            if url_prod.startswith("/"):
                url_prod = base_url.rstrip("/") + url_prod

            price_el = node.select_one(".price")
            price    = price_el.get_text(strip=True) if price_el else ""

            raw      = node.get_text().lower()
            in_stock = not any(x in raw for x in ["agotado", "sin stock"])

            products[pid] = {
                "id": pid, "name": name, "url": url_prod,
                "price": price, "in_stock": in_stock,
            }

        next_btn = soup.select_one("a.action.next, li.pages-item-next a")
        if not next_btn:
            break
        page += 1

    return products


def parse_meli_html(html_text: str, base_url: str) -> dict:
    """Fallback HTML para ML."""
    soup = BeautifulSoup(html_text, "html.parser")
    products: dict = {}

    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = re.search(r"(MLA\d+)", href)
        if not m:
            continue
        pid = m.group(1)
        if pid in products:
            continue

        name = a.get_text(strip=True)
        if not name:
            img = a.find("img")
            name = img["alt"] if img and img.get("alt") else a.get("title", "")
        if not name:
            continue

        name = html.unescape(name.strip())
        price = ""
        container = a.find_parent()
        if container:
            price_el = container.select_one(
                ".andes-money-amount__fraction, .price-tag-fraction, [class*='price']"
            )
            if price_el:
                price = price_el.get_text(strip=True)

        products[pid] = {
            "id": pid, "name": name, "url": href.split("#")[0],
            "price": price, "in_stock": True,
        }
    return products

def parse_meli_api(_html_text: str, _base_url: str) -> dict:
    """
    Usa la API oficial de MercadoLibre: precio y stock exactos,
    sin depender de scraping de JavaScript.
    """
    products: dict = {}
    offset = 0
    limit  = 50

    while True:
        url = f"{ML_API_URL}&offset={offset}"
        try:
            data = fetch_json(url)
        except RuntimeError as e:
            log(f"[ML API] error: {e} — usando fallback HTML")
            return {}

        results = data.get("results", [])
        if not results:
            break

        for item in results:
            pid       = item.get("id", "")
            name      = item.get("title", "")
            price_val = item.get("price")
            price     = f"${price_val:,.0f}".replace(",", ".") if price_val else ""
            permalink = item.get("permalink", "")
            avail_qty = item.get("available_quantity", 0)
            in_stock  = avail_qty > 0

            products[pid] = {
                "id": pid, "name": name, "url": permalink,
                "price": price, "in_stock": in_stock,
            }

        total  = data.get("paging", {}).get("total", 0)
        offset += limit
        if offset >= total:
            break

    log(f"[ML API] {len(products)} productos obtenidos via API oficial")
    return products


PARSERS = {"magento": parse_magento, "meli": parse_meli_api, "meli_html": parse_meli_html}


# -------- Telegram --------

def tg_send(text: str) -> bool:
    if not TG_TOKEN or not TG_CHAT_ID:
        log("Telegram no configurado")
        return False
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    try:
        resp = requests.post(
            url,
            json={
                "chat_id": TG_CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        log(f"[tg_send] error: {e}")
        return False


# -------- Formateo de alertas --------

def _precio_line(prod: dict) -> str:
    return f"\n\U0001f4b2 <b>Precio:</b> {prod['price']}" if prod.get('price') else ""


def _stock_line(prod: dict) -> str:
    return "\u2705 En stock" if prod.get('in_stock') else "\u26a0\ufe0f Sin stock confirmado"


def fmt_new(site: str, prod: dict) -> str:
    return (
        "\U0001f6a8\U0001f195 <b>\u00a1PRODUCTO NUEVO DETECTADO!</b> \U0001f6a8\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\U0001f4e6 <b>{prod['name']}</b>\n"
        f"\U0001f3ea <b>Tienda:</b> {site}\n"
        f"{_precio_line(prod)}\n"
        f"{_stock_line(prod)}\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\U0001f6d2 <a href=\"{prod['url']}\">\U0001f449 IR A COMPRAR AHORA</a>\n"
        "\u26a1 \u00a1Entr\u00e1 antes que se agote!"
    )


def fmt_restock(site: str, prod: dict) -> str:
    return (
        "\U0001f514\U0001f504 <b>\u00a1VOLVIÓ AL STOCK!</b> \U0001f514\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\U0001f4e6 <b>{prod['name']}</b>\n"
        f"\U0001f3ea <b>Tienda:</b> {site}\n"
        f"{_precio_line(prod)}\n"
        "\u2705 Disponible nuevamente\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\U0001f6d2 <a href=\"{prod['url']}\">\U0001f449 IR A COMPRAR AHORA</a>\n"
        "\u26a1 \u00a1Estaba agotado, aprovech\u00e1!"
    )


def fmt_price_change(site: str, prod: dict, old_price: str) -> str:
    return (
        "\U0001f4b0\U0001f4c9 <b>\u00a1CAMBIO DE PRECIO!</b> \U0001f4b0\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\U0001f4e6 <b>{prod['name']}</b>\n"
        f"\U0001f3ea <b>Tienda:</b> {site}\n"
        f"{_stock_line(prod)}\n"
        f"\U0001f534 <b>Antes:</b> {old_price}\n"
        f"\U0001f7e2 <b>Ahora:</b> {prod['price']}\n"
        "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n"
        f"\U0001f6d2 <a href=\"{prod['url']}\">\U0001f449 IR A COMPRAR AHORA</a>"
    )


def fmt_site_error(site_name: str) -> str:
    return (
        f"\u26a0\ufe0f <b>Error de conexi\u00f3n</b>\n"
        f"No se pudo acceder a <b>{site_name}</b> tras {MAX_RETRIES} intentos.\n"
        "El monitoreo de ese sitio se salt\u00f3 en este ciclo."
    )


def fmt_heartbeat(state: dict) -> str:
    lines = ["\U0001f49a <b>Bot activo \u2014 Monitor Panini</b>\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501"]
    total = 0
    for site in SITES:
        count = len(state.get(site['key'], {}).get('products', {}))
        total += count
        lines.append(f"\U0001f3ea {site['name']}: <b>{count}</b> productos")
    lines.append(f"\n\U0001f4e6 Total monitoreado: <b>{total}</b> productos")
    lines.append("\u23f1\ufe0f Frecuencia: cada ~90 segundos (3 checks por ciclo de 5 min)")
    return "\n".join(lines)


def fmt_first_run(state: dict) -> str:
    lines = ["\U0001f916 <b>Monitor Panini iniciado</b>\n\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501", "Estado inicial guardado:"]
    total = 0
    for site in SITES:
        count = len(state.get(site['key'], {}).get('products', {}))
        total += count
        lines.append(f"  \u2022 {site['name']}: <b>{count}</b> productos")
    lines.append(f"\n\U0001f4e6 Total: <b>{total}</b> productos en seguimiento")
    lines.append("\nA partir de ahora te avisar\u00e9 cuando haya cambios. \U0001f440")
    return "\n".join(lines)


# -------- Procesamiento paralelo --------

def process_site(site: dict, state: dict) -> tuple:
    """Fetch + parse + diff de un sitio. Corre en thread paralelo."""
    alerts: list = []
    error_msg = None

    try:
        html_text = fetch(site['url'])
    except RuntimeError as e:
        log(f"[ERROR] {site['name']}: {e}")
        return alerts, None, fmt_site_error(site['name'])

    parser_key = site['parser']
    products   = PARSERS[parser_key](html_text, site['url'])

    # Fallback HTML si la API de ML devuelve vacío
    if parser_key == 'meli' and not products:
        log('[ML] API vacía, usando fallback HTML')
        products = parse_meli_html(html_text, site['url'])

    log(f"{site['name']} \u2192 {len(products)} productos")

    prev = state.get(site['key'], {}).get('products', {})

    for pid, prod in products.items():
        if not matches_keywords(prod['name']):
            continue
        old = prev.get(pid)
        if old is None:
            alerts.append(fmt_new(site['name'], prod))
        else:
            if not old.get('in_stock') and prod.get('in_stock'):
                alerts.append(fmt_restock(site['name'], prod))
            old_price = old.get('price', '')
            new_price = prod.get('price', '')
            if old_price and new_price and old_price != new_price:
                alerts.append(fmt_price_change(site['name'], prod, old_price))

    return alerts, products, error_msg


# -------- Main --------

def run():
    state      = load_state()
    first_run  = not state

    if IS_HEARTBEAT:
        log('Modo heartbeat')
        tg_send(fmt_heartbeat(state))
        return 0

    all_alerts: list = []
    all_errors: list = []
    new_state = dict(state)

    log(f"[Run {RUN_INDEX}/3] Procesando {len(SITES)} sitios en paralelo...")

    with ThreadPoolExecutor(max_workers=len(SITES)) as executor:
        future_to_site = {executor.submit(process_site, site, state): site for site in SITES}
        for future in as_completed(future_to_site):
            site = future_to_site[future]
            try:
                alerts, products, error_msg = future.result()
                if products is not None:
                    new_state[site['key']] = {'products': products}
                if alerts:
                    all_alerts.extend(alerts)
                if error_msg:
                    all_errors.append(error_msg)
            except Exception as e:
                log(f"[ERROR inesperado] {site['name']}: {e}")
                all_errors.append(fmt_site_error(site['name']))

    if not first_run:
        if len(all_alerts) > 1:
            grouped = (
                f"\U0001f525 <b>{len(all_alerts)} alertas nuevas</b> \U0001f525\n"
                "\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\n\n"
                + "\n\n".join(all_alerts)
            )
            tg_send(grouped)
        elif len(all_alerts) == 1:
            tg_send(all_alerts[0])
        for err in all_errors:
            tg_send(err)
            time.sleep(0.5)
    elif first_run:
        tg_send(fmt_first_run(new_state))

    save_state(new_state)
    log(f"[Run {RUN_INDEX}/3] Listo. {len(all_alerts)} alertas, {len(all_errors)} errores.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
