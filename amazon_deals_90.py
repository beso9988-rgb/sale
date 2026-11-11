import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime

from playwright.sync_api import sync_playwright
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
# ============ الإعدادات ============

CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

MIN_DISCOUNT = 70  # ابعتلي أي منتج خصمه >= الرقم ده

HEADLESS = True    # True = في الخلفية من غير نافذة، False = يظهر المتصفح (لو المواقع بدأت تحجب)

# صفحات أمازون اللي هيفحصها كل لفّة
AMAZON_PAGES = [
    "https://www.amazon.eg/gp/goldbox",
    # "https://www.amazon.eg/s?k=laptop&s=price-asc-rank",
]

# صفحات جوميا مصر اللي هيفحصها كل لفّة
JUMIA_PAGES = [
    "https://www.jumia.com.eg/ar/flash-sales/",
    "https://www.jumia.com.eg/ar/mlp-wow-deals/",
    # نصيحة: افتح قسم في جوميا (موبايلات، إلكترونيات...) واستخدم فلتر الخصم لو موجود
    # وبعدين انسخ اللينك من المتصفح وحطه هنا، مثلاً:
    # "https://www.jumia.com.eg/mobile-phones/",
]

MIN_DELAY = 60   # فاصل بين كل لفّة (ثواني)
MAX_DELAY = 120
SCROLLS = 15      # عدد مرات النزول في الصفحة عشان يحمّل منتجات أكتر
# ===================================

# كل موقع: اسمه، قايمة صفحاته، selector للينكات المنتجات، وطريقة استخراج ID المنتج
SITES = [
    {
        "name": "Amazon",
        "pages": AMAZON_PAGES,
        "selector": 'a[href*="/dp/"]',
        "key": lambda href: (re.search(r"/dp/([A-Z0-9]{10})", href) or [None, None])[1],
        "link": lambda key, href: f"https://www.amazon.eg/dp/{key}",
    },
    {
        "name": "Jumia",
        "pages": JUMIA_PAGES,
        "selector": 'article.prd a.core, a[href$=".html"]',
        "key": lambda href: href.split("?")[0].split("#")[0]
        if "jumia.com.eg" in href and href.split("?")[0].endswith(".html")
        else None,
        "link": lambda key, href: key,
    },
]

seen = set()  # المنتجات اللي اتبعت عنها إشعار

AR_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩٪", "0123456789%")

JS_COLLECT = """
(sel) => {
  const out = [];
  for (const a of document.querySelectorAll(sel)) {
    let el = a, text = '';
    for (let i = 0; i < 7 && el; i++) {
      el = el.parentElement;
      if (!el) break;
      const t = (el.innerText || '').trim();
      if (t.length > 700) break;
      if (t.includes('%') || t.includes('٪')) { text = t; break; }
    }
    if (text) out.push({href: a.href, text: text});
  }
  return out;
}
"""

DISCOUNT_PATTERNS = [
    r"(?:خصم|توفير|وفر|off|save)\s*:?\s*-?\s*(\d{1,3})\s*%",
    r"(\d{1,3})\s*%\s*(?:خصم|off|توفير)",
    r"(?<![\w.])-\s*(\d{1,3})\s*%",
]

BLOCK_WORDS = [
    "captcha", "robot check", "just a moment", "attention required", "access denied",
    "verify you are human", "not a robot", "enter the characters",
]


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def send_telegram(text):
    api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode()
    try:
        urllib.request.urlopen(api, data=data, timeout=15)
    except Exception as e:
        log(f"فشل إرسال تليجرام: {e}")


def best_discount(text):
    # بنحسب بس النسب اللي جنبها كلمة خصم/off أو علامة (-)
    # عشان نتجنب نسب زي "قطن 100%" اللي في اسم المنتج
    text = text.translate(AR_DIGITS)
    vals = []
    for pat in DISCOUNT_PATTERNS:
        vals += [int(x) for x in re.findall(pat, text, flags=re.IGNORECASE)]
    vals = [v for v in vals if v <= 100]
    return max(vals) if vals else 0


def scan(page, url, site):
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2500)
    # بنفحص النص الظاهر والعنوان بس (مش كود الصفحة)، وبس لو الصفحة صغيرة
    # عشان صفحات الحجب بتبقى قصيرة، وصفحات المنتجات العادية فيها كلمة captcha في السكريبتات
    title = (page.title() or "").lower()
    try:
        body = page.inner_text("body")
    except Exception:
        body = ""
    if len(body) < 2500 and any(w in f"{title} {body.lower()}" for w in BLOCK_WORDS):
        log(f"صفحة حجب؟ العنوان: {title[:80]!r} - طول النص: {len(body)}")
        return "BLOCKED"
    found = {}
    for _ in range(SCROLLS):
        for item in page.evaluate(JS_COLLECT, site["selector"]):
            key = site["key"](item["href"])
            if key:
                found[key] = item
        page.mouse.wheel(0, 1800)
        page.wait_for_timeout(1200)
    if not found:
        log(f"مفيش منتجات اتقرت. العنوان: {title[:80]!r} - طول النص: {len(body)}")
    return found


def main():
    with sync_playwright() as p:
        args = ["--disable-blink-features=AutomationControlled"]
        try:
            # إيدج المتثبت على ويندوز: بيتحجب أقل من Chromium بتاع playwright
            browser = p.chromium.launch(headless=HEADLESS, channel="msedge", args=args)
            log("شغّال بإيدج الحقيقي" + (" في الخلفية" if HEADLESS else ""))
        except Exception as e:
            log(f"إيدج مش متاح ({e}) هستخدم Chromium العادي")
            browser = p.chromium.launch(headless=HEADLESS, args=args)
        context = browser.new_context(
            locale="ar-EG",
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36 Edg/130.0.0.0"
            ),
        )
        page = context.new_page()
        send_telegram(f"✅ فاحص الخصومات (أمازون + جوميا) اشتغل، الحد الأدنى {MIN_DISCOUNT}%")

        while True:
            for site in SITES:
                for url in site["pages"]:
                    try:
                        result = scan(page, url, site)
                    except Exception as e:
                        log(f"خطأ في {url}: {e}")
                        continue

                    if result == "BLOCKED":
                        log(f"{site['name']} طلب تحقق/حظر! وقف 10 دقايق...")
                        send_telegram(f"⚠️ {site['name']} طلب تحقق، السكريبت واقف 10 دقايق")
                        time.sleep(600)
                        continue

                    log(f"[{site['name']}] فحصت {len(result)} منتج في {url}")
                    for key, item in result.items():
                        dsc = best_discount(item["text"])
                        uid = f"{site['name']}:{key}"
                        if dsc >= MIN_DISCOUNT and uid not in seen:
                            seen.add(uid)
                            snippet = " ".join(item["text"].split())[:150]
                            send_telegram(
                                f"🔥 [{site['name']}] خصم {dsc}%\n{snippet}\n{site['link'](key, item['href'])}"
                            )
                            log(f"إشعار: {uid} خصم {dsc}%")
                    time.sleep(random.uniform(5, 12))

            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


if __name__ == "__main__":
    main()
