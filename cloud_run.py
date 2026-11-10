"""
تشغيل مرة واحدة (لـ GitHub Actions): بيفحص أمازون وجوميا، يبعت الإشعارات، ويحفظ اللي اتبعت في seen.json.
لازم يكون في نفس الفولدر مع amazon_deals_90.py
التوكن والشات ايدي بيتقروا من Secrets: TELEGRAM_BOT_TOKEN و TELEGRAM_CHAT_ID
"""
import json
import os
import random
import time

from playwright.sync_api import sync_playwright

import amazon_deals_90 as d

STATE_FILE = "seen.json"
BLOCK_ALERT_EVERY = 6 * 3600   # تنبيه الحظر مرة كل 6 ساعات بحد أقصى عشان ما يزعجكش

d.BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
d.CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
        st.setdefault("seen", [])
        st.setdefault("last_block_alert", 0)
        return st
    except Exception:
        return {"seen": [], "last_block_alert": 0}


def save_state(state):
    state["seen"] = sorted(d.seen)[-5000:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def run(page, state):
    blocked = []
    for site in d.SITES:
        for url in site["pages"]:
            try:
                result = d.scan(page, url, site)
            except Exception as e:
                d.log(f"خطأ في {url}: {e}")
                continue

            if result == "BLOCKED":
                d.log(f"{site['name']} حجب الاتصال")
                blocked.append(site["name"])
                continue

            d.log(f"[{site['name']}] فحصت {len(result)} منتج في {url}")
            for key, item in result.items():
                dsc = d.best_discount(item["text"])
                uid = f"{site['name']}:{key}"
                if dsc >= d.MIN_DISCOUNT and uid not in d.seen:
                    d.seen.add(uid)
                    snippet = " ".join(item["text"].split())[:150]
                    d.send_telegram(
                        f"🔥 [{site['name']}] خصم {dsc}%\n{snippet}\n{site['link'](key, item['href'])}"
                    )
            time.sleep(random.uniform(2, 5))

    if blocked and time.time() - state["last_block_alert"] > BLOCK_ALERT_EVERY:
        state["last_block_alert"] = time.time()
        d.send_telegram(
            f"⚠️ {', '.join(sorted(set(blocked)))} حجب الفحص من سيرفر GitHub. "
            "لو بيتكرر، يبقى الحل تشغيله من جهاز في البيت."
        )


def main():
    state = load_state()
    d.seen.update(state["seen"])
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
            context = browser.new_context(
                locale="ar-EG",
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
                ),
            )
            run(context.new_page(), state)
            browser.close()
    finally:
        save_state(state)


if __name__ == "__main__":
    main()
