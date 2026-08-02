"""
Zara "Є хоч якийсь розмір" Tracker -> Telegram (паралельна версія)

Відмінність від попередньої версії: замість перевірки товарів ПО ЧЕРЗІ
(що при ~30 товарах займає кілька хвилин), тепер кілька товарів
перевіряються ОДНОЧАСНО — у різних вкладках одного й того самого
браузера. Сам браузер запускається лише один раз за весь запуск.

Як і раніше: жодного кліку, жодної дії з кошиком — тільки читання
сторінки (безпечно, не виглядає як зловживання).

Скільки товарів перевіряти одночасно — задається константою
CONCURRENCY нижче. 5 — розумний баланс між швидкістю і тим, щоб
не виглядати як "штурм" сайту багатьма запитами водночас.

Перед першим запуском:
1. Встав свій BOT_TOKEN і CHAT_ID нижче (або задай їх через змінні
   середовища ZARA_BOT_TOKEN / ZARA_CHAT_ID).
2. Заповни products.json своїми товарами (лише "label" і "url").
3. `pip install playwright` і один раз `playwright install chromium`.

Кожен запуск скрипту — це ОДНА перевірка всіх товарів (не нескінченний
цикл). Регулярність — через cron / Планувальник завдань (див. README.md).
"""

import asyncio
import json
import os
import re
from datetime import datetime

import requests
from playwright.async_api import async_playwright

# ---------- НАЛАШТУВАННЯ ----------
BOT_TOKEN = os.environ.get("ZARA_BOT_TOKEN", "ВСТАВ_ТОКЕН_ТУТ")
CHAT_ID = os.environ.get("ZARA_CHAT_ID", "ВСТАВ_CHAT_ID_ТУТ")

CONCURRENCY = 5  # скільки товарів перевіряти одночасно

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PRODUCTS_FILE = os.path.join(BASE_DIR, "products.json")
STATE_FILE = os.path.join(BASE_DIR, "state.json")

MARKER_CAN_ADD = "покласти в кошик"
MARKER_SOLD_OUT = "немає в наявності"


def send_telegram(text: str) -> None:
    if "ВСТАВ" in BOT_TOKEN or "ВСТАВ" in CHAT_ID:
        print("[!] Спочатку заповни BOT_TOKEN і CHAT_ID у zara_watch.py")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": CHAT_ID, "text": text}, timeout=10)
        if not r.ok:
            print(f"[!] Telegram відповів помилкою: {r.status_code} {r.text}")
    except Exception as e:
        print(f"[!] Не вдалося надіслати повідомлення: {e}")


def load_json(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path, data) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def safe_filename(text: str) -> str:
    cleaned = re.sub(r"[^\w\-]+", "_", text)
    return cleaned[:50] or "product"


async def check_one_product(context, product, semaphore):
    url = product["url"]
    label = product.get("label", url)

    async with semaphore:
        page = await context.new_page()
        try:
            await page.goto(url, wait_until="load", timeout=60000)
            try:
                await page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass
            await page.wait_for_timeout(1500)
            html = await page.content()
        except Exception as e:
            print(f"[{datetime.now():%H:%M:%S}] Помилка для '{label}': {e}")
            await page.close()
            return url, label, None
        await page.close()

    if len(html) < 10000:
        print(f"    [!] '{label}': підозріло маленька відповідь ({len(html)} символів)")
        debug_path = os.path.join(BASE_DIR, f"debug_{safe_filename(label)}.html")
        try:
            with open(debug_path, "w", encoding="utf-8") as f:
                f.write(html)
            print(f"    [!] Збережено для перевірки: {debug_path}")
        except Exception:
            pass

    html_lower = html.lower()
    can_add = MARKER_CAN_ADD in html_lower
    sold_out = MARKER_SOLD_OUT in html_lower
    available = can_add and not sold_out

    return url, label, available


async def run_all_checks(products):
    semaphore = asyncio.Semaphore(CONCURRENCY)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            ),
            locale="uk-UA",
        )

        tasks = [check_one_product(context, product, semaphore) for product in products]
        results = await asyncio.gather(*tasks)

        await browser.close()

    return results


def main() -> None:
    products = load_json(PRODUCTS_FILE, [])
    if not products:
        print("products.json порожній — додай хоча б один товар.")
        return

    state = load_json(STATE_FILE, {})

    results = asyncio.run(run_all_checks(products))

    for url, label, available_now in results:
        if available_now is None:
            continue  # була помилка при перевірці цього товару — стан не чіпаємо

        available_before = bool(state.get(url, False))
        status = "Є розмір(и) в наявності" if available_now else "немає в наявності"
        print(f"[{datetime.now():%H:%M:%S}] {label}: {status}")

        if available_now and not available_before:
            text = (
                f"🟢 {label}\n"
                f"З'явився хоча б один розмір у наявності!\n"
                f"{url}"
            )
            send_telegram(text)

        state[url] = available_now

    save_json(STATE_FILE, state)


if __name__ == "__main__":
    main()
