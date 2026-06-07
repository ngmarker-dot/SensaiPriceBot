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
    """Парсит bigbox.ee и возвращает список товаров Sensai с ценами."""
    products = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        await page.goto(URL, wait_until="networkidle", timeout=60000)

        # Ждём появления товаров
        try:
            await page.wait_for_selector(".product-miniature, .product-list-item, [class*='product']", timeout=15000)
        except Exception:
            pass

        # Прокрутить страницу чтобы загрузить все товары
        for _ in range(5):
            await page.evaluate("window.scrollBy(0, 800)")
            await asyncio.sleep(0.5)

        # Попробуем найти товары через разные селекторы
        items = await page.query_selector_all("article, .product-miniature, [class*='product-item'], [class*='productItem']")

        for item in items:
            try:
                # Название
                name_el = await item.query_selector("h2, h3, .product-title, [class*='name'], [class*='title']")
                if not name_el:
                    continue
                name = (await name_el.inner_text()).strip()

                # Фильтруем только Sensai
                if "sensai" not in name.lower():
                    continue

                # Цена
                price_el = await item.query_selector("[class*='price'], .price, span[itemprop='price']")
                if not price_el:
                    continue
                price_text = (await price_el.inner_text()).strip()

                # Извлекаем число из цены
                price_clean = re.sub(r"[^\d,.]", "", price_text).replace(",", ".")
                if not price_clean:
                    continue
                price = float(price_clean)

                # Ссылка
                link_el = await item.query_selector("a")
                link = ""
                if link_el:
                    link = await link_el.get_attribute("href") or ""
                    if link and not link.startswith("http"):
                        link = "https://bigbox.ee" + link

                products[name] = {"price": price, "link": link}

            except Exception:
                continue

        # Если не нашли через article, попробуем через JSON в странице
        if not products:
            content = await page.content()
            # Ищем структурированные данные
            json_matches = re.findall(r'"name"\s*:\s*"([^"]*sensai[^"]*)".*?"price"\s*:\s*"?([\d.]+)"?', content, re.IGNORECASE)
            for match in json_matches:
                name, price = match
                products[name] = {"price": float(price), "link": URL}

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
    async with httpx.AsyncClient() as client:
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
    print(f"Найдено товаров: {len(current)}")

    if not current:
        print("Товары не найдены. Проверь парсер.")
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
