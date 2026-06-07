import json
import os
import re
from datetime import datetime
from pathlib import Path

import httpx

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
PRICES_FILE = Path("prices.json")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "et-EE,et;q=0.9,en;q=0.8,ru;q=0.7",
    "Referer": "https://bigbox.ee/",
}

# bigbox.ee использует PrestaShop — пробуем их внутренний API поиска
SEARCH_URLS = [
    "https://bigbox.ee/search?text=sensai&resultsPerPage=100",
    "https://bigbox.ee/module/ps_facetedsearch/search?q=sensai",
]


def fetch_products():
    products = {}

    with httpx.Client(headers=HEADERS, timeout=30, follow_redirects=True) as client:
        # Метод 1: Парсим HTML страницу поиска
        try:
            resp = client.get("https://bigbox.ee/search?text=sensai&resultsPerPage=100")
            resp.raise_for_status()
            html = resp.text
            print(f"Загружено {len(html)} байт HTML")

            # PrestaShop хранит данные товаров в JSON внутри HTML
            # Ищем structured data (JSON-LD)
            jsonld_matches = re.findall(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL)
            for raw in jsonld_matches:
                try:
                    data = json.loads(raw.strip())
                    items = []
                    if isinstance(data, dict):
                        if data.get("@type") == "ItemList":
                            items = data.get("itemListElement", [])
                        elif data.get("@type") == "Product":
                            items = [data]
                    elif isinstance(data, list):
                        items = data

                    for item in items:
                        if item.get("@type") == "ListItem":
                            item = item.get("item", {})
                        if item.get("@type") != "Product":
                            continue
                        name = item.get("name", "")
                        if "sensai" not in name.lower():
                            continue
                        offers = item.get("offers", {})
                        if isinstance(offers, list):
                            offers = offers[0] if offers else {}
                        price = offers.get("price")
                        url = item.get("url", "") or offers.get("url", "")
                        if price and name:
                            products[name] = {"price": float(price), "link": url}
                            print(f"  JSON-LD: {name} — {price}€")
                except Exception as e:
                    continue

            # Метод 2: PrestaShop хранит данные в window.prestashop или похожих переменных
            if not products:
                patterns = [
                    r'"product_name"\s*:\s*"([^"]*[Ss]ensai[^"]*)"[^}]{0,300}?"price"\s*:\s*"?([\d.]+)',
                    r'"name"\s*:\s*"([^"]*[Ss]ensai[^"]*)"[^}]{0,300}?"price"\s*:\s*"?([\d.]+)',
                    r'data-product-name="([^"]*[Ss]ensai[^"]*)"[^>]*data-product-price="([\d.]+)"',
                    r'data-name="([^"]*[Ss]ensai[^"]*)"[^>]*data-price="([\d.]+)"',
                ]
                for pattern in patterns:
                    matches = re.findall(pattern, html, re.IGNORECASE)
                    for name, price in matches:
                        name = name.strip()
                        if name and name not in products:
                            products[name] = {"price": float(price), "link": "https://bigbox.ee/search?text=sensai"}
                            print(f"  Regex: {name} — {price}€")

            # Метод 3: Ищем блоки с ценами рядом с Sensai
            if not products:
                # Ищем все упоминания sensai и ближайшие цены
                sensai_blocks = re.findall(
                    r'([Ss]ensai[^<]{0,200}?)([\d]+[.,][\d]{2})\s*€',
                    html
                )
                for block, price in sensai_blocks:
                    # Очищаем название
                    name = re.sub(r'[<>"\'\\/]', '', block).strip()[:80]
                    price_clean = price.replace(",", ".")
                    if name and name not in products:
                        products[name] = {"price": float(price_clean), "link": "https://bigbox.ee/search?text=sensai"}
                        print(f"  Block: {name} — {price_clean}€")

        except Exception as e:
            print(f"Ошибка HTTP запроса: {e}")

        # Метод 4: Пробуем API PrestaShop
        if not products:
            try:
                api_resp = client.get(
                    "https://bigbox.ee/search",
                    params={"text": "sensai", "resultsPerPage": "100"},
                    headers={**HEADERS, "X-Requested-With": "XMLHttpRequest"}
                )
                print(f"API статус: {api_resp.status_code}")
            except Exception as e:
                print(f"API ошибка: {e}")

    return products


def load_previous_prices():
    if PRICES_FILE.exists():
        with open(PRICES_FILE) as f:
            return json.load(f)
    return {}


def save_prices(products):
    data = {name: info["price"] for name, info in products.items()}
    with open(PRICES_FILE, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def find_discounts(current, previous):
    discounts = []
    for name, info in current.items():
        new_price = info["price"]
        old_price = previous.get(name)
        if old_price and new_price < old_price:
            pct = round((old_price - new_price) / old_price * 100, 1)
            discounts.append({
                "name": name,
                "old_price": old_price,
                "new_price": new_price,
                "pct": pct,
                "link": info.get("link", ""),
            })
    return sorted(discounts, key=lambda x: x["pct"], reverse=True)


def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    with httpx.Client(timeout=30) as client:
        resp = client.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        })
        resp.raise_for_status()


def main():
    print(f"[{datetime.now()}] Запуск трекера Sensai...")

    current = fetch_products()
    print(f"Итого найдено товаров: {len(current)}")

    if not current:
        print("Товары не найдены.")
        send_telegram("⚠️ <b>Sensai Tracker</b>: не удалось загрузить товары с bigbox.ee. Проверю завтра.")
        return

    previous = load_previous_prices()
    discounts = find_discounts(current, previous)
    save_prices(current)

    if not previous:
        msg = (
            f"📦 <b>Sensai Tracker запущен!</b>\n"
            f"Нашёл {len(current)} товаров Sensai на bigbox.ee.\n"
            f"Завтра начну отслеживать изменения цен. 🔍"
        )
        send_telegram(msg)
        print("Первый запуск — сохранил базовые цены.")
        return

    if discounts:
        lines = [f"🔥 <b>Sensai подешевел на bigbox.ee!</b> ({datetime.now().strftime('%d.%m.%Y')})\n"]
        for d in discounts:
            lines.append(
                f"📉 <b>{d['name']}</b>\n"
                f"   Было: {d['old_price']:.2f}€ → Стало: {d['new_price']:.2f}€ (-{d['pct']}%)\n"
                f"   🔗 <a href='{d['link']}'>Посмотреть</a>\n"
            )
        send_telegram("\n".join(lines))
        print(f"Отправлено {len(discounts)} скидок.")
    else:
        msg = f"✅ <b>Sensai на bigbox.ee</b> ({datetime.now().strftime('%d.%m.%Y')})\nЦены не изменились. Слежу дальше 👀"
        send_telegram(msg)
        print("Изменений цен нет.")


if __name__ == "__main__":
    main()
