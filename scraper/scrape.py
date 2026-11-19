"""Revisa los precios de products.json, guarda el historial y avisa por Telegram."""
import json
import os
import random
import re
import statistics
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
PRODUCTS = json.loads((ROOT / "products.json").read_text(encoding="utf-8"))
CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
DATA_FILE = ROOT / "data" / "prices.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-CL,es;q=0.9",
}
DAY = 24 * 3600 * 1000


def now_ms():
    return int(time.time() * 1000)


def to_int(v):
    """Convierte '459.990', 459990 o '459990.00' en 459990. Devuelve None si no parece un precio."""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        n = int(round(v))
    else:
        s = re.sub(r"[^\d,.]", "", str(v))
        if not s:
            return None
        if re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
            s = s.replace(".", "")
        elif re.fullmatch(r"\d+[.,]\d{1,2}", s):
            s = re.split(r"[.,]", s)[0]
        else:
            s = s.replace(".", "").replace(",", "")
        n = int(s) if s else None
    return n if n and 1000 <= n <= 20_000_000 else None


def walk(node):
    """Recorre cualquier estructura JSON y entrega todos los diccionarios."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from walk(v)


def first(v):
    return v[0] if isinstance(v, list) and v else v


def from_price_list(data):
    """Formato tipo Falabella: prices = [{type, price:[..], crossed}, ...]."""
    for d in walk(data):
        lst = d.get("prices")
        if not isinstance(lst, list) or not lst or not all(isinstance(x, dict) for x in lst):
            continue
        now_prices, was_prices = [], []
        for p in lst:
            val = to_int(first(p.get("price")))
            if not val:
                continue
            if p.get("crossed") or p.get("type") == "normalPrice":
                was_prices.append(val)
            else:
                now_prices.append(val)
        if now_prices:
            return min(now_prices), (max(was_prices) if was_prices else None)
    return None


def from_price_info(data):
    """Formato tipo Lider (Walmart): priceInfo con currentPrice y wasPrice."""
    for d in walk(data):
        if "currentPrice" in d:
            cur, was = d.get("currentPrice"), d.get("wasPrice")
            cur = to_int(cur.get("price") if isinstance(cur, dict) else cur)
            was = to_int(was.get("price") if isinstance(was, dict) else was)
            if cur:
                return cur, was
    return None


def from_json_ld(html):
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(block)
        except Exception:
            continue
        for d in walk(data):
            offers = d.get("offers")
            for o in offers if isinstance(offers, list) else [offers]:
                if isinstance(o, dict):
                    val = to_int(o.get("price") or o.get("lowPrice"))
                    if val:
                        return val, None
    return None


def from_meta(html):
    m = re.search(r'(?:product:price:amount|og:price:amount)["\']\s+content=["\']([^"\']+)', html, re.I)
    if m:
        val = to_int(m.group(1))
        if val:
            return val, None
    return None


def extract(html):
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if m:
        try:
            data = json.loads(m.group(1))
        except Exception:
            data = None
        if data:
            for fn in (from_price_list, from_price_info):
                r = fn(data)
                if r:
                    price, was = r
                    return price, (was if was and was > price else None)
    for fn in (from_json_ld, from_meta):
        r = fn(html)
        if r:
            return r
    return None


def fetch(url):
    last = "sin respuesta"
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=25)
            if r.status_code == 200 and len(r.text) > 2000:
                return r.text
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = type(e).__name__
        time.sleep(3 * (attempt + 1))
    raise RuntimeError(last)


def telegram(text):
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("Telegram no configurado, aviso omitido:", text.splitlines()[0])
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text, "disable_web_page_preview": False},
            timeout=20,
        )
    except requests.RequestException as e:
        print("No se pudo enviar a Telegram:", e)


def fmt(n):
    return "$" + f"{n:,}".replace(",", ".")


def reasons_for(price, was, past, target):
    out = []
    if was and was > price:
        disc = (was - price) / was * 100
        if disc >= CONFIG["discount_alert_pct"]:
            out.append(f"{disc:.0f}% bajo su precio normal ({fmt(was)})")
    recent = [h[1] for h in past if h[0] >= now_ms() - 14 * DAY]
    if len(recent) >= CONFIG["min_samples_for_median"]:
        med = statistics.median(recent)
        if price <= med * (1 - CONFIG["drop_vs_median_pct"] / 100):
            out.append(f"{(1 - price / med) * 100:.0f}% bajo su precio típico de 14 días ({fmt(int(med))})")
    if len(past) >= 5 and price < min(h[1] for h in past):
        out.append("mínimo histórico registrado")
    if target and price <= target:
        out.append(f"llegó a tu meta de {fmt(target)}")
    return out


def main():
    data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
    store = data["products"]
    failed = 0
    for i, p in enumerate(PRODUCTS):
        rec = store.setdefault(p["id"], {"history": [], "alert": None})
        rec.setdefault("history", [])
        try:
            res = extract(fetch(p["url"]))
            if not res:
                raise RuntimeError("no encontré el precio en la página")
            price, was = res
        except Exception as e:  # noqa: BLE001
            failed += 1
            rec["ok"], rec["error"] = False, str(e)
            print(f"[FALLÓ] {p['name']}: {e}")
            continue

        past = list(rec["history"])
        reasons = reasons_for(price, was, past, CONFIG.get("targets", {}).get(p["id"]))
        if not past or past[-1][1] != price or now_ms() - past[-1][0] > 12 * 3600 * 1000:
            rec["history"].append([now_ms(), price, was])
        rec["history"] = [h for h in rec["history"] if h[0] >= now_ms() - 90 * DAY]
        rec.update(ok=True, price=price, was=was, last_ok=now_ms(), error=None)

        alert = rec.get("alert")
        if reasons:
            if not alert or price < alert["price"]:
                rec["alert"] = {"price": price, "reasons": reasons, "t": now_ms()}
                telegram(f"🔔 {p['name']} ({p['store']})\n{fmt(price)}\n" + "\n".join("• " + r for r in reasons) + f"\n{p['url']}")
        else:
            rec["alert"] = None
        print(f"[OK] {p['name']}: {fmt(price)}")
        if i < len(PRODUCTS) - 1:
            time.sleep(random.uniform(2, 5))

    bad = failed >= max(3, len(PRODUCTS) // 2)
    if bad and not data.get("health_alerted"):
        telegram(f"⚠️ Tantea no pudo leer {failed} de {len(PRODUCTS)} productos. Puede que una tienda esté bloqueando las consultas.")
    data["health_alerted"] = bad
    data["updated"] = now_ms()
    DATA_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
