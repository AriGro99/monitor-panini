"""
Monitor de productos - Zonakids, Tienda Panini y Mercado Libre (tienda Panini).
Detecta productos nuevos y vuelta de stock, y avisa por Telegram.

Variables de entorno requeridas:
  TELEGRAM_BOT_TOKEN  - token del bot (de @BotFather)
  TELEGRAM_CHAT_ID    - chat_id donde enviar los avisos (de @userinfobot)

Archivo de estado:
  state.json (se crea la primera corrida; guarda los productos vistos por sitio)

Uso:
  python monitor.py
"""
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
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-AR,es;q=0.9,en;q=0.8",
}

TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TG_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

SITES = [
    {"key": "zonakids", "name": "Zonakids", "url": "https://zonakids.com/", "parser": "magento"},
    {"key": "tiendapanini", "name": "Tienda Panini", "url": "https://tiendapanini.com.ar/", "parser": "magento"},
    {"key": "ml_panini", "name": "ML - Tienda Panini", "url": "https://www.mercadolibre.com.ar/tienda/panini", "parser": "meli"},
]

# Si querés monitorear más páginas (ej: categorías de "novedades"), agregalas acá.

# -------- Filtro por palabras clave --------
# Solo envía alertas para productos cuyo NOMBRE cumpla AMBAS condiciones:
#  1) Contenga TODOS los tokens de al menos UNO de los grupos en KEYWORDS_ANY_OF.
#  2) NO contenga NINGÚN token de KEYWORDS_EXCLUDE.
#
# Por defecto está configurado para el Álbum del Mundial 2026 (figuritas/sobres
# clásicos), excluyendo "Adrenalyn" (cartas, un coleccionable distinto).
#
# Para monitorear otro álbum, reemplazá estos grupos.
# Para recibir TODOS los productos (sin filtro), dejá KEYWORDS_ANY_OF = [].
# Si también querés Adrenalyn, dejá KEYWORDS_EXCLUDE = [].
KEYWORDS_ANY_OF: list[list[str]] = [
    ["mundial", "2026"],
    ["world cup", "2026"],
    ["copa del mundo", "2026"],
    ["fifa", "2026"],
]
KEYWORDS_EXCLUDE: list[str] = ["adrenalyn"]


def matches_keywords(name: str) -> bool:
    """True si el nombre del producto cumple el filtro.
    Si KEYWORDS_ANY_OF está vacío, no aplica match positivo (pasa todo lo no excluido)."""
    n = name.lower()
    for excl in KEYWORDS_EXCLUDE:
        if excl.lower() in n:
            return False
    if not KEYWORDS_ANY_OF:
        return True
    for group in KEYWORDS_ANY_OF:
        if all(tok.lower() in n for tok in group):
            return True
    return False


# -------- Utilidades --------

def log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def fetch(url: str) -> str:
    r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text


def load_state() -> dict[str, Any]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            log("state.json corrupto, se reinicia.")
    return {}


def save_state(state: dict[str, Any]) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


# -------- Parsers --------

def parse_magento(html_text: str, base_url: str) -> dict[str, dict[str, Any]]:
    """Extrae productos de una home Magento (Zonakids / Tienda Panini).
    Clave única: product_id (Magento).
    """
    soup = BeautifulSoup(html_text, "html.parser")
    products: dict[str, dict[str, Any]] = {}

    # Buscamos contenedores típicos de product-item o product-item-info
    candidates = soup.select("li.product-item, div.product-item, div.product-item-info")

    for node in candidates:
        # product-id: lo más confiable está en el price-box
        pid_el = node.find(attrs={"data-product-id": True})
        if not pid_el:
            continue
        pid = str(pid_el.get("data-product-id")).strip()
        if not pid or not pid.isdigit():
            continue
        if pid in products:
            continue  # ya capturado

        # Nombre + URL: a.product-item-link
        name_a = node.select_one("a.product-item-link")
        name = ""
        url = ""
        if name_a:
            name = name_a.get_text(strip=True)
            url = name_a.get("href", "")
        if not name:
            # fallback: cualquier <a title="..."> dentro
            any_a = node.find("a", title=True)
            if any_a:
                name = any_a["title"]
                url = any_a.get("href", url)
        if not url:
            any_a = node.find("a", href=True)
            if any_a:
                url = any_a["href"]

        if url and url.startswith("/"):
            url = base_url.rstrip("/") + url

        # Precio
        price = ""
        price_el = node.select_one("span.price-wrapper[data-price-amount] span.price, span.price")
        if price_el:
            price = price_el.get_text(strip=True)
        price_amount = ""
        price_node = node.select_one("[data-price-amount]")
        if price_node:
            price_amount = price_node.get("data-price-amount", "")

        # Stock: si el bloque tiene un <a class="action tocart"> o botón "Añadir al Carrito" → in_stock
        # Si tiene clase/texto de "unavailable" → out_of_stock
        text = node.get_text(" ", strip=True).lower()
        classes = " ".join(node.get("class", []) + [c for sub in node.select("[class]") for c in sub.get("class", [])])
        in_stock = True
        if ("unavailable" in classes.lower()
                or "agotado" in text
                or "sin stock" in text
                or "no disponible" in text):
            in_stock = False
        # chequeo extra: si hay "tocart" explícito, lo marcamos en stock
        if node.select_one("a.action.tocart, button.action.tocart, .action.tocart.primary"):
            in_stock = True

        products[pid] = {
            "id": pid,
            "name": html.unescape(name) if name else "(sin nombre)",
            "url": url,
            "price": price,
            "price_amount": price_amount,
            "in_stock": in_stock,
        }

    return products


def parse_meli(html_text: str, base_url: str) -> dict[str, dict[str, Any]]:
    """Extrae productos de la home de una tienda oficial de Mercado Libre.
    Clave única: MLA-id del item (item_id=MLA\\d+ en la URL).
    """
    products: dict[str, dict[str, Any]] = {}

    # Los links de producto tienen forma:
    #   https://www.mercadolibre.com.ar/<slug>/p/MLA12345678?...item_id%3AMLA99999999...
    # Nos quedamos con el item_id porque cada "oferta" del shop tiene un item_id único.
    link_re = re.compile(
        r'href="(https?://www\.mercadolibre\.com\.ar/[^"]+?/p/MLA\d+[^"]*?item_id%3A(MLA\d+)[^"]*?)"',
        re.IGNORECASE,
    )
    title_re = re.compile(r'title=(?:"|&quot;)([^"&]+)', re.IGNORECASE)

    # Algunas tarjetas no tienen item_id en el href (productos destacados del shop):
    # también capturamos links /MLA-\d+-slug
    alt_re = re.compile(r'href="(https?://(?:articulo\.)?mercadolibre\.com\.ar/MLA-(\d+)-[^"]+)"', re.IGNORECASE)

    soup = BeautifulSoup(html_text, "html.parser")

    # Recorremos cada <a href> que matchee y tratamos de sacar título y precio del contexto
    for a in soup.find_all("a", href=True):
        href = a["href"]
        mla_id = None
        m1 = re.search(r"item_id%3A(MLA\d+)", href) or re.search(r"item_id=(MLA\d+)", href)
        if m1:
            mla_id = m1.group(1)
        else:
            m2 = re.search(r"/MLA-(\d+)-", href)
            if m2:
                mla_id = "MLA" + m2.group(1)
        if not mla_id or mla_id in products:
            continue

        # Nombre: en ML shops suele venir como texto del <a class="poly-component__title">
        # o en el alt de la imagen de la tarjeta.
        name = ""
        classes_a = " ".join(a.get("class") or [])
        if "poly-component__title" in classes_a:
            name = a.get_text(strip=True)
        if not name:
            name = a.get("title") or ""
        if not name:
            h = a.find(["h2", "h3"])
            if h:
                name = h.get_text(strip=True)
        if not name:
            txt = a.get_text(strip=True)
            if txt:
                name = txt
        if not name:
            img = a.find("img")
            if img and img.get("alt"):
                name = img["alt"]
        name = html.unescape(name).strip()

        # Precio: subimos al contenedor de la tarjeta (poly-card o andes-card) y buscamos la clase price
        card = a
        for _ in range(5):
            if card.parent is None:
                break
            card = card.parent
            if card.name in (None,):
                continue
            if any("poly-card" in c or "andes-card" in c for c in (card.get("class") or [])):
                break
        price = ""
        price_el = card.select_one(
            ".poly-price__current .andes-money-amount__fraction, "
            ".andes-money-amount__fraction, .price-tag-fraction"
        )
        if price_el:
            cents_el = card.select_one(".andes-money-amount__cents, .price-tag-cents")
            price = price_el.get_text(strip=True)
            if cents_el:
                price += "," + cents_el.get_text(strip=True)

        # Limpiar URL (sin los parámetros de tracking)
        clean_url = re.sub(r"#.*$", "", href)

        products[mla_id] = {
            "id": mla_id,
            "name": name or "(sin nombre)",
            "url": clean_url,
            "price": price,
            "in_stock": True,  # ML oculta out-of-stock de la home; si desaparece = probable agotado
        }

    return products


PARSERS = {"magento": parse_magento, "meli": parse_meli}


# -------- Telegram --------

def tg_send(text: str) -> None:
    if not TG_TOKEN or not TG_CHAT_ID:
        log("Falta TELEGRAM_BOT_TOKEN o TELEGRAM_CHAT_ID; imprimo aviso en consola:")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    # Telegram rate-limit: ~30 msgs/seg en total, 1/seg al mismo chat. Vamos tranquilos.
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    try:
        r = requests.post(url, json=payload, timeout=TIMEOUT)
        if r.status_code != 200:
            log(f"Telegram error {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log(f"Telegram excepción: {e}")


def fmt_alert(site_name: str, kind: str, prod: dict[str, Any]) -> str:
    emoji = {"nuevo": "🆕", "disponible": "🛒", "stock": "✅", "precio": "💲"}.get(kind, "🔔")
    title_line = {
        "nuevo": f"{emoji} <b>Nuevo producto listado en {html.escape(site_name)}</b>",
        "disponible": f"{emoji} <b>¡Disponible para comprar en {html.escape(site_name)}!</b>",
        "stock": f"{emoji} <b>Volvió el stock en {html.escape(site_name)}</b>",
        "precio": f"{emoji} <b>Cambio de precio en {html.escape(site_name)}</b>",
    }.get(kind, f"{emoji} <b>{html.escape(site_name)}</b>")
    lines = [title_line]
    name = html.escape(prod.get("name", ""))
    url = prod.get("url", "")
    if url:
        lines.append(f'<a href="{html.escape(url)}">{name}</a>')
    else:
        lines.append(name)
    if prod.get("price"):
        lines.append(f"Precio: {html.escape(prod['price'])}")
    return "\n".join(lines)


# -------- Loop principal --------

def run() -> int:
    state = load_state()
    alerts: list[str] = []
    matches_log: list[tuple[str, str]] = []  # (sitio, nombre) de cada producto que matchea el filtro

    first_run = not state  # si nunca corrió, no enviamos alertas "nuevo" de todo el catálogo

    for site in SITES:
        key = site["key"]
        name = site["name"]
        url = site["url"]
        parser_name = site["parser"]
        log(f"Procesando {name} ({url}) ...")
        try:
            html_text = fetch(url)
        except Exception as e:
            log(f"  ERROR al bajar {url}: {e}")
            continue

        try:
            products = PARSERS[parser_name](html_text, url)
        except Exception as e:
            log(f"  ERROR parseando {url}: {e}")
            continue

        log(f"  Detectados {len(products)} productos.")

        prev = state.get(key, {}).get("products", {})
        new_state = {"last_run": time.time(), "products": products}

        for pid, prod in products.items():
            # Solo nos interesan los productos que matcheen el filtro de palabras clave.
            if not matches_keywords(prod.get("name", "")):
                continue
            matches_log.append((name, prod["name"]))

            old = prev.get(pid)
            if old is None:
                # Producto nuevo (nunca visto).
                # En la primera corrida también avisamos: si ya hay algún ítem que matchea
                # el filtro, que el usuario lo sepa desde el arranque.
                kind = "nuevo"
                # Si ya aparece en stock desde el inicio, marcamos como "disponible".
                if prod.get("in_stock", True):
                    kind = "disponible"
                alerts.append(fmt_alert(name, kind, prod))
            else:
                # Stock: pasó de agotado a disponible
                if not old.get("in_stock", True) and prod.get("in_stock", True):
                    alerts.append(fmt_alert(name, "stock", prod))
                # Cambio de precio (solo si no hubo cambio de stock para no duplicar)
                elif old.get("price") and prod.get("price") and old["price"] != prod["price"]:
                    alerts.append(
                        fmt_alert(
                            name,
                            "precio",
                            {**prod, "name": f'{prod["name"]} ({old["price"]} → {prod["price"]})'},
                        )
                    )

        state[key] = new_state

    # Log: qué productos matchean el filtro actualmente (para que el usuario vea el alcance)
    if KEYWORDS_ANY_OF:
        log(f"Filtro activo KEYWORDS_ANY_OF={KEYWORDS_ANY_OF}")
    log(f"Productos que matchean el filtro en esta corrida: {len(matches_log)}")
    for site_name, pname in matches_log[:10]:
        log(f"  - [{site_name}] {pname}")

    if first_run and not alerts:
        log("Primera corrida sin matches: guardo estado inicial y espero la próxima corrida.")
        # Aviso opcional de arranque, para confirmar que el bot está funcionando.
        tg_send(
            "🤖 Bot iniciado.\n"
            f"Monitoreando {len(SITES)} sitios cada 5 minutos.\n"
            f"Filtro: {KEYWORDS_ANY_OF or 'sin filtro (todos los productos)'}.\n"
            "Por ahora no hay productos que matcheen; te aviso en cuanto aparezca alguno."
        )
    else:
        log(f"Alertas a enviar: {len(alerts)}")
        for a in alerts[:20]:
            tg_send(a)
            time.sleep(1)
        if len(alerts) > 20:
            tg_send(f"ℹ️ Hay {len(alerts) - 20} alertas más que no se enviaron para no saturar.")

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(run())
            products = PARSERS[parser_name](html_text, url)
        except Exception as e:
            log(f"  ERROR parseando {url}: {e}")
            continue

        log(f"  Detectados {len(products)} productos.")

        prev = state.get(key, {}).get("products", {})
        new_state = {"last_run": time.time(), "products": products}

        for pid, prod in products.items():
            # Solo nos interesan los productos que matcheen el filtro de palabras clave.
            if not matches_keywords(prod.get("name", "")):
                continue
            matches_log.append((name, prod["name"]))

            old = prev.get(pid)
            if old is None:
                # Producto nuevo (nunca visto).
                # En la primera corrida también avisamos: si ya hay algún ítem que matchea
                # el filtro, que el usuario lo sepa desde el arranque.
                kind = "nuevo"
                # Si ya aparece en stock desde el inicio, marcamos como "disponible".
                if prod.get("in_stock", True):
                    kind = "disponible"
                alerts.append(fmt_alert(name, kind, prod))
            else:
                # Stock: pasó de agotado a disponible
                if not old.get("in_stock", True) and prod.get("in_stock", True):
                    alerts.append(fmt_alert(name, "stock", prod))
                # Cambio de precio (solo si no hubo cambio de stock para no duplicar)
                elif old.get("price") and prod.get("price") and old["price"] != prod["price"]:
                    alerts.append(
                        fmt_alert(
                            name,
                            "precio",
                            {**prod, "name": f'{prod["name"]} ({old["price"]} → {prod["price"]})'},
                        )
                    )

        state[key] = new_state

    # Log: qué productos matchean el filtro actualmente (para que el usuario vea el alcance)
    if KEYWORDS_ANY_OF:
        log(f"Filtro activo KEYWORDS_ANY_OF={KEYWORDS_ANY_OF}")
    log(f"Productos que matchean el filtro en esta corrida: {len(matches_log)}")
    for site_name, pname in matches_log[:10]:
        log(f"  - [{site_name}] {pname}")

    if first_run and not alerts:
        log("Primera corrida sin matches: guardo estado inicial y espero la próxima corrida.")
        # Aviso opcional de arranque, para confirmar que el bot está funcionando.
        tg_send(
            "🤖 Bot iniciado.\n"
            f"Monitoreando {len(SITES)} sitios cada 5 minutos.\n"
            f"Filtro: {KEYWORDS_ANY_OF or 'sin filtro (todos los productos)'}.\n"
            "Por ahora no hay productos que matcheen; te aviso en cuanto aparezca alguno."
        )
    else:
        log(f"Alertas a enviar: {len(alerts)}")
        for a in alerts[:20]:
            tg_send(a)
            time.sleep(1)
        if len(alerts) > 20:
            tg_send(f"ℹ️ Hay {len(alerts) - 20} alertas más que no se enviaron para no saturar.")

    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(run())