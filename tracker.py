import asyncio
import json
import os
import re
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright
import httpx

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
PRICES_FILE = Path("prices.json")
URL = "https://bigbox.ee/search?text=sensai"


async def fetch_products():
    products = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"]
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await context.new_page()

        # Используем domcontentloaded вместо networkidle — быстрее и надёжнее
        try:
            await page.goto(URL, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            print(f"Ошибка загрузки страницы: {e}")
            await browser.close()
            return products

        # Даём JS время отрендерить товары
        await asyncio.sleep(5)

        # Прокрутка для lazy-load
        for _ in range(6):
            await page.evaluate("window.scrollBy(0, 600)")
            await asyncio.sleep(0.7)

        await asyncio.sleep(2)

        # Пробуем разные селекторы товаров
        selectors = [
            "article",
            ".product-miniature",
            "[class*='product-item']",
            "[class*='ProductItem']",
            "[class*='product_item']",
            ".js-product",
        ]

        items = []
        for sel in selectors:
            items = await page.query_selector_all(sel)
            if items:
                print(f"Нашёл {len(items)} элементов по селектору: {sel}")
                break

        for item in items:
            try:
                name_el = await item.query_selector(
                    "h2, h3, h4, .product-title, [class*='name'], [class*='title'], a[title]"
                )
                if not name_el:
                    continue
                name = (await name_el.inner_text()).strip()
                if not name or "sensai" not in name.lower():
                    continue

                price_el = await item.query_selector(
                    "[class*='price']:not([class*='old']):not([class*='regular']), "
                    "span[itemprop='price'], [class*='current-price'], [class*='currentPrice']"
                )
                if not price_el:
                    continue
                price_text = (await price_el.inner_text()).strip()
                price_clean = re.sub(r"[^\d,.]", "", price_text).replace(",", ".")
                # Берём первое число если их несколько
                match = re.search(r"\d+\.?\d*", price_clean)
                if not match:
                    continue
                price = float(match.group())

                link_el = await item.query_selector("a")
                link = ""
                if link_el:
                    link = await link_el.get_attribute("href") or ""
                    if link and not link.startswith("http"):
                        link = "https://bigbox.ee" + link

                products[name] = {"price": price, "link": link}
                print(f"  Товар: {name} — {price}€")

            except Exception as e:
                continue

        # Fallback: ищем данные в JSON на странице
        if not products:
            print("Пробую JSON-парсинг страницы...")
            content = await page.content()
            # Ищем JSON с товарами
            patterns = [
                r'"name"\s*:\s*"([^"]*[Ss]ensai[^"]*)"[^}]*?"price"\s*:\s*["\']?([\d.]+)',
                r'"title"\s*:\s*"([^"]*[Ss]ensai[^"]*)"[^}]*?"price"\s*:\s*["\']?([\d.]+)',
            ]
            for pattern in patterns:
                matches = re.findall(pattern, content, re.IGNORECASE | re.DOTALL)
                for name, price in matches:
                    name = name.strip()
                    if name not in products:
                        products[name] = {"price": float(price), "link": URL}
                        print(f"  JSON товар: {name} — {price}€")

        await browser.close()
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


async def send_telegram(text):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        })
        resp.raise_for_status()


async def main():
    print(f"[{datetime.now()}] Запуск трекера Sensai...")

    current = await fetch_products()
    print(f"Итого найдено товаров: {len(current)}")

    if not current:
        print("Товары не найдены.")
        await send_telegram("⚠️ <b>Sensai Tracker</b>: не удалось загрузить товары с bigbox.ee. Проверю завтра.")
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
        await send_telegram(msg)
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
        await send_telegram("\n".join(lines))
        print(f"Отправлено {len(discounts)} скидок.")
    else:
        msg = f"✅ <b>Sensai на bigbox.ee</b> ({datetime.now().strftime('%d.%m.%Y')})\nЦены не изменились. Слежу дальше 👀"
        await send_telegram(msg)
        print("Изменений цен нет.")


if __name__ == "__main__":
    asyncio.run(main())
