"""
Zara "Є хоч якийсь розмір" Tracker -> Telegram (безперервна версія)

ЧОМУ ЦЯ ВЕРСІЯ ІНША:
Попередня версія запускалась ОДИН раз, перевіряла всі товари і виходила —
а розклад (кожні 10 хв) сам запускав її знову. Але кожен такий запуск
марнує ~1-2 хвилини лише на "розігрів" (встановлення Python, Playwright,
Xvfb) ще ДО самої перевірки.

Ця версія замість цього запускається РІДКО (раз на кілька годин), але
сама крутиться в циклі всередині — знову й знову перевіряє всі товари
з невеликою паузою між циклами, поки не набіжить ліміт часу одного
завдання GitHub Actions (макс. 6 годин). Це дає перевірку практично
щохвилини-півтори, замість раз на 10 хвилин.

Як і раніше: жодного кліку, жодної дії з кошиком — тільки читання
сторінки (безпечно, не виглядає як зловживання).

Перед першим запуском:
1. Встав свій BOT_TOKEN і CHAT_ID нижче (або задай їх через змінні
   середовища ZARA_BOT_TOKEN / ZARA_CHAT_ID).
2. Заповни products.json своїми товарами (лише "label" і "url").
3. `pip install playwright requests` і один раз `playwright install chromium`.
"""

import asyncio
import json
import os
import re
import time
from datetime import datetime

import requests
from playwright.async_api import async_playwright

# ---------- НАЛАШТУВАННЯ ----------
BOT_TOKEN = os.environ.get("ZARA_BOT_TOKEN", "ВСТАВ_ТОКЕН_ТУТ")
CHAT_ID = os.environ.get("ZARA_CHAT_ID", "ВСТАВ_CHAT_ID_ТУТ")

CONCURRENCY = 5  # скільки товарів перевіряти одночасно

# Скільки секунд одна сесія триватиме максимум, перш ніж сама завершиться
# (щоб встигнути закомітити результати ДО того, як GitHub примусово
# зупинить завдання через 6 годин). 5 год 40 хв лишає запас часу.
MAX_RUNTIME_SECONDS = int(os.environ.get("ZARA_MAX_RUNTIME", 5 * 3600 + 40 * 60))

# Пауза між повними циклами перевірки всіх товарів
CHECK_INTERVAL_SECONDS = int(os.environ.get("ZARA_CHECK_INTERVAL", 20))

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PRODUCTS_FILE = os.path.join(BASE_DIR, "products.json")
STATE_FILE = os.path.join(BASE_DIR, "state.json")
LOG_FILE = os.path.join(BASE_DIR, "run_log.txt")

MARKER_CAN_ADD = "Додати"
MARKER_SOLD_OUT = "немає в наявності"


def reset_log() -> None:
    """Починаємо новий файл-звіт на кожну сесію (а не дописуємо в той
    самий файл вічно — інакше він рос би без кінця з кожним комітом)."""
    try:
        open(LOG_FILE, "w", encoding="utf-8").close()
    except Exception:
        pass


def log(message: str) -> None:
    print(message)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(message + "\n")
    except Exception:
        pass


def send_telegram(text: str) -> None:
    if "ВСТАВ" in BOT_TOKEN or "ВСТАВ" in CHAT_ID:
        log("[!] Спочатку заповни BOT_TOKEN і CHAT_ID у zara_watch.py")
        return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        r = requests.post(url, data={"chat_id": CHAT_ID, "text": text}, timeout=10)
        if not r.ok:
            log(f"[!] Telegram відповів помилкою: {r.status_code} {r.text}")
    except Exception as e:
        log(f"[!] Не вдалося надіслати повідомлення: {e}")


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


async def check_one_product(context, product, semaphore, save_debug=False):
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
            log(f"[{datetime.now():%H:%M:%S}] Помилка для '{label}': {e}")
            await page.close()
            return url, label, None
        await page.close()

    html_lower = html.lower()
    can_add = MARKER_CAN_ADD in html_lower
    sold_out = MARKER_SOLD_OUT in html_lower
    available = can_add and not sold_out

    if len(html) < 10000 or save_debug:
        debug_path = os.path.join(BASE_DIR, f"debug_{safe_filename(label)}.html")
        try:
            with open(debug_path, "w", encoding="utf-8") as f:
                f.write(html)
        except Exception:
            pass

    return url, label, available


async def run_one_pass(context, products, semaphore, pass_number):
    tasks = [
        check_one_product(context, product, semaphore, save_debug=(i == 0 and pass_number == 1))
        for i, product in enumerate(products)
    ]
    return await asyncio.gather(*tasks)


async def run_loop(products, state) -> None:
    start_time = time.monotonic()
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

        pass_number = 0
        while True:
            pass_number += 1
            elapsed = time.monotonic() - start_time
            if elapsed > MAX_RUNTIME_SECONDS:
                log(f"\n===== Ліміт часу сесії досягнуто ({elapsed/60:.0f} хв) — завершую =====")
                break

            log(f"\n----- Цикл #{pass_number} ({datetime.now():%H:%M:%S}) -----")

            try:
                results = await run_one_pass(context, products, semaphore, pass_number)
            except Exception as e:
                log(f"[!] Помилка циклу #{pass_number}: {e}")
                await asyncio.sleep(CHECK_INTERVAL_SECONDS)
                continue

            available_count = 0
            for url, label, available_now in results:
                if available_now is None:
                    continue

                available_before = bool(state.get(url, False))
                if available_now:
                    available_count += 1

                if available_now and not available_before:
                    log(f"    🟢 {label}: З'ЯВИВСЯ розмір!")
                    text = (
                        f"🟢 {label}\n"
                        f"З'явився хоча б один розмір у наявності!\n"
                        f"{url}"
                    )
                    send_telegram(text)

                state[url] = available_now

            save_json(STATE_FILE, state)
            log(f"    Підсумок циклу #{pass_number}: є в наявності {available_count} з {len(products)}")

            await asyncio.sleep(CHECK_INTERVAL_SECONDS)

        await browser.close()


def main() -> None:
    reset_log()
    log(f"===== Сесію розпочато {datetime.now():%d.%m.%Y %H:%M:%S} =====")

    products = load_json(PRODUCTS_FILE, [])
    if not products:
        log("products.json порожній — додай хоча б один товар.")
        return

    state = load_json(STATE_FILE, {})

    asyncio.run(run_loop(products, state))

    log(f"===== Сесію завершено {datetime.now():%d.%m.%Y %H:%M:%S} =====")


if __name__ == "__main__":
    main()
