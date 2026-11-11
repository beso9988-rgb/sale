"""
مراقب تذكرتي: يبلغك على تليجرام لما أي ماتش (أو حدث) يبقى متاح للحجز.
بيشتغل بطريقتين:
  1) على جهازك: python tazkarti_watch.py   (بيفضل شغال ويفحص كل شوية)
  2) على GitHub Actions: لو متغيّرات TELEGRAM_BOT_TOKEN و TELEGRAM_CHAT_ID موجودة، بيفحص مرة واحدة ويطلع
"""
import hashlib
import json
import os
import random
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime

from playwright.sync_api import sync_playwright

# ============ الإعدادات ============
BOT_TOKEN = "[REDACTED]"
CHAT_ID = "***REMOVED***"

# افتح موقع تذكرتي في متصفحك، ادخل على صفحة المباريات، وانسخ اللينك من شريط العنوان وحطه هنا.
# تقدر تحط أكتر من صفحة (مثلاً الدوري + كأس مصر).
PAGES = [
    "https://tazkarti.com/#/matches",
]

# لو عايز تتابع ماتشات فريق معين بس (بالعربي أو الإنجليزي)، اكتب الاسم. سيبها فاضية [] لكل الماتشات.
TEAMS_FILTER = [
    # "الأهلي",
    # "الزمالك",
]

MIN_DELAY = 45      # فاصل بين كل فحص (ثواني) - في التشغيل على جهازك بس
MAX_DELAY = 90
HEADLESS = True    # False = يظهر نافذة إيدج (الأضمن)، True = في الخلفية
SAVE_DEBUG = True   # يحفظ صورة ونص الصفحة في أول فحص (فولدر Documents)
RENOTIFY_AFTER_HOURS = 6   # لو ماتش اختفى ورجع متاح، ما يبلغش تاني قبل الفترة دي
# ===================================

BOOK_RE = "احجز|حجز|اشتر|شراء|book|buy|get tickets"
UNAVAILABLE_RE = "نفذ|نفد|sold|انتهت|غير متاح|غير متوفر|unavailable|مغلق|closed|قريب|soon|لا توجد"

CLOUD = "TELEGRAM_BOT_TOKEN" in os.environ
if CLOUD:
    BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
    CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
    HEADLESS = True

STATE_FILE = "seen_tazkarti.json"

JS_COLLECT = """
({book, bad}) => {
  const bookRe = new RegExp(book, 'i'), badRe = new RegExp(bad, 'i');
  const out = [];
  for (const el of document.querySelectorAll('a, button, [role="button"], .btn')) {
    if (el.closest('header, nav, footer')) continue;
    const label = (el.innerText || el.value || '').trim();
    if (!label || label.length > 40 || !bookRe.test(label)) continue;
    const disabled = !!el.disabled || el.getAttribute('aria-disabled') === 'true' ||
                     /disabled/i.test(el.className || '');
    let c = el, text = '';
    for (let i = 0; i < 6 && c.parentElement; i++) {
      c = c.parentElement;
      const t = (c.innerText || '').trim();
      if (t.length > 600) break;
      text = t;
      if (t.split('\\n').length >= 4) break;
    }
    if (text.split('\\n').length < 2) continue;
    const a = el.closest('a[href]') || c.querySelector('a[href*="#/"]');
    out.push({label, disabled, text, href: a ? a.href : '', bad: badRe.test(text) || badRe.test(label)});
  }
  return out;
}
"""

BLOCK_WORDS = ["captcha", "just a moment", "attention required", "access denied",
               "verify you are human", "not a robot", "forbidden"]


def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def send_telegram(text):
    api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text[:4000]}).encode()
    try:
        urllib.request.urlopen(api, data=data, timeout=15)
    except Exception as e:
        log(f"فشل إرسال تليجرام: {e}")


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
        st.setdefault("avail", [])
        st.setdefault("last_notified", {})
        st.setdefault("last_block_alert", 0)
        st.setdefault("baseline_done", False)
        return st
    except Exception:
        return {"avail": [], "last_notified": {}, "last_block_alert": 0, "baseline_done": False}


def save_state(st):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False)
    except Exception as e:
        log(f"ماقدرتش أحفظ الحالة: {e}")


def clean_lines(text):
    lines = []
    for ln in text.split("\n"):
        ln = ln.strip()
        if not ln:
            continue
        if re.fullmatch(r"[\d\s:٠-٩]+", ln):   # عدادات تنازلية وأرقام لوحدها بتتغير كل ثانية
            continue
        lines.append(ln)
    return lines


def make_key(lines, href):
    base = "|".join(lines).lower()
    base = re.sub(r"\s+", " ", base)
    return hashlib.md5(base.encode("utf-8")).hexdigest()[:12]


def scan(page, url, first_run):
    """بيرجّع (الحالة, {key: info}). الحالة: OK / BLOCKED / EMPTY"""
    page.goto("about:blank")                      # عشان الصفحات اللي بتتغير بالـ # تتحمل من جديد
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    page.wait_for_timeout(6000)

    title = (page.title() or "").lower()
    body = page.inner_text("body")

    if first_run and SAVE_DEBUG:
        if CLOUD:
            log(f"عنوان الصفحة: {title!r} - طول النص: {len(body)}")
            log("بداية النص:\n" + body[:1500])
        else:
            try:
                folder = os.path.join(os.path.expanduser("~"), "Documents")
                page.screenshot(path=os.path.join(folder, "tazkarti_debug.png"), full_page=True)
                with open(os.path.join(folder, "tazkarti_debug.txt"), "w", encoding="utf-8") as f:
                    f.write(f"URL: {page.url}\nTitle: {title}\n\n{body}")
                log(f"حفظت ملفات الفحص في: {folder}")
            except Exception as e:
                log(f"ماقدرتش أحفظ ملفات الفحص (مش مشكلة): {e}")

    if len(body) < 2500 and any(w in f"{title} {body.lower()}" for w in BLOCK_WORDS):
        return "BLOCKED", {}

    found = {}
    for c in page.evaluate(JS_COLLECT, {"book": BOOK_RE, "bad": UNAVAILABLE_RE}):
        if c["disabled"] or c["bad"]:
            continue
        lines = clean_lines(c["text"])
        if len(lines) < 2:
            continue
        if TEAMS_FILTER and not any(t.lower() in " ".join(lines).lower() for t in TEAMS_FILTER):
            continue
        found[make_key(lines, c["href"])] = {"lines": lines[:6], "href": c["href"] or url}
    return ("OK" if found else "EMPTY"), found


def describe(info):
    return "\n".join(info["lines"][:5])


def process(page, state, first_run):
    now = time.time()
    current, blocked, healthy = {}, False, False
    for url in PAGES:
        try:
            status, found = scan(page, url, first_run)
        except Exception as e:
            log(f"خطأ في {url}: {e}")
            continue
        log(f"{url} -> {status} ({len(found)} متاح)")
        if status == "BLOCKED":
            blocked = True
            continue
        healthy = True
        current.update(found)
        time.sleep(random.uniform(3, 6))

    if blocked and now - state["last_block_alert"] > 6 * 3600:
        state["last_block_alert"] = now
        send_telegram(
            "⚠️ موقع تذكرتي طلب تحقق أو حجب الفحص."
            + (" لو بيتكرر على GitHub، شغّله من جهازك." if CLOUD else "")
        )

    if not healthy:
        return  # ماغيّرناش الحالة عشان ما نبعتش إشعارات غلط بعد

    prev = set(state["avail"])
    if not state["baseline_done"]:
        state["baseline_done"] = True
        state["avail"] = list(current)
        if current:
            listing = "\n\n".join(describe(i) for i in list(current.values())[:8])
            send_telegram(f"🧪 اختبار: المتاح للحجز دلوقتي ({len(current)}):\n\n{listing}")
        else:
            send_telegram("🧪 اختبار: الصفحة اتفتحت بس مفيش ماتشات متاحة للحجز دلوقتي. هبلغك أول ما يتفتح حجز.")
        return

    for key, info in current.items():
        last = state["last_notified"].get(key, 0)
        if key not in prev and now - last > RENOTIFY_AFTER_HOURS * 3600:
            state["last_notified"][key] = now
            send_telegram(f"🎟️ متاح للحجز دلوقتي:\n\n{describe(info)}\n\n{info['href']}")
            log(f"إشعار: {info['lines'][:2]}")
    state["avail"] = list(current)


def launch(p):
    args = ["--disable-blink-features=AutomationControlled"]
    if not CLOUD:
        try:
            b = p.chromium.launch(headless=HEADLESS, channel="msedge", args=args)
            log("شغّال بإيدج الحقيقي")
            return b
        except Exception as e:
            log(f"إيدج مش متاح ({e}) هستخدم Chromium العادي")
    return p.chromium.launch(headless=HEADLESS, args=args)


def main():
    state = load_state()
    with sync_playwright() as p:
        browser = launch(p)
        context = browser.new_context(
            locale="ar-EG",
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        if CLOUD:
            try:
                process(page, state, first_run=not state["baseline_done"])
            finally:
                save_state(state)
            browser.close()
            return

        send_telegram("✅ مراقب تذكرتي اشتغل")
        while True:
            try:
                process(page, state, first_run=not state["baseline_done"])
                save_state(state)
            except Exception as e:
                log(f"خطأ: {e}")
            time.sleep(random.uniform(MIN_DELAY, MAX_DELAY))


if __name__ == "__main__":
    main()
