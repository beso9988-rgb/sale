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
BOT_TOKEN = "ضع_التوكن_الجديد_هنا"
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
HEADLESS = False    # False = يظهر نافذة إيدج (الأضمن)، True = في الخلفية
SAVE_DEBUG = True   # يحفظ صورة ونص الصفحة في أول فحص (فولدر Documents)
RENOTIFY_AFTER_HOURS = 1   # لو ماتش اختفى ورجع متاح، ما يبلغش تاني قبل الفترة دي
# ===================================

BOOK_RE = "احجز|حجز|اشتر|شراء|book|buy|get tickets"
UNAVAILABLE_RE = "نفذ|نفد|sold|انتهت|غير متاح|غير متوفر|unavailable|مغلق|closed|قريب|soon|لا توجد"

CLOUD = "TELEGRAM_BOT_TOKEN" in os.environ
if CLOUD:
    BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
    CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
    HEADLESS = True

STATE_FILE = "seen_tazkarti.json"
STATE_VERSION = 2

JS_COLLECT = """
({book, bad}) => {
  const bookRe = new RegExp(book, 'i'), badRe = new RegExp(bad, 'i');
  const SEL = 'a, button, [role="button"], .btn';
  const isBook = (el) => {
    const l = (el.innerText || el.value || '').trim();
    return l && l.length <= 40 && bookRe.test(l);
  };
  const els = [...document.querySelectorAll(SEL)].filter(el => !el.closest('header, nav, footer') && isBook(el));
  const out = [];
  els.forEach((el, idx) => {
    el.setAttribute('data-tzk', String(idx));
    const label = (el.innerText || el.value || '').trim();
    const disabled = !!el.disabled || el.getAttribute('aria-disabled') === 'true' ||
                     /disabled/i.test(el.className || '');
    // نطلع لفوق طول ما الكارت فيه زرار حجز واحد بس والنص مش طويل
    let c = el, best = el.parentElement || el;
    for (let i = 0; i < 8 && c.parentElement; i++) {
      c = c.parentElement;
      // ما نطلعش لحد الصفحة كلها: نقف لو وصلنا لمنطقة فيها قايمة/هيدر/فلاتر
      if (/^(MAIN|BODY|HTML)$/.test(c.tagName)) break;
      if (c.querySelector('header, nav, footer, form, select, input')) break;
      const n = [...c.querySelectorAll(SEL)].filter(isBook).length;
      const t = (c.innerText || '').trim();
      if (n > 1 || t.length > 600 || t.split('\\n').length > 14) break;
      best = c;
    }
    const text = (best.innerText || '').trim();
    if (text.split('\\n').length < 2) return;

    // أسماء الفرق من alt/title بتاع الصور (الشعارات)
    const teams = [];
    best.querySelectorAll('img').forEach(im => {
      const n = (im.alt || im.title || '').trim();
      if (n && n.length < 40 && !/logo|icon|stadium|ticket|tazkarti/i.test(n) && !teams.includes(n)) teams.push(n);
    });

    // لينك الماتش
    let href = '';
    const a = el.closest('a[href]') || best.querySelector('a[href]');
    if (a) {
      const raw = a.getAttribute('href') || '';
      if (raw && raw !== '#' && !raw.startsWith('javascript')) href = a.href;
    }
    if (!href) {
      for (const node of [el, ...best.querySelectorAll('*')]) {
        for (const attr of ['routerlink', 'ng-reflect-router-link', 'data-href', 'data-url']) {
          const v = node.getAttribute && node.getAttribute(attr);
          if (v && /\\d/.test(v)) {
            href = v.startsWith('http') ? v : location.origin + '/#' + (v.startsWith('/') ? v : '/' + v);
            break;
          }
        }
        if (href) break;
      }
    }
    out.push({idx, label, disabled, text, teams, href,
              bad: badRe.test(text) || badRe.test(label),
              html: idx < 3 ? best.outerHTML.slice(0, 2500) : ''});
  });
  return out;
}
"""

NAV_NOISE = re.compile(r"\bfaq\b|about tazkarti|our stores|stadium locations|الأسئلة الشائعة", re.I)

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
        if st.get("ver") != STATE_VERSION:
            raise ValueError("old state")
        st.setdefault("avail", [])
        st.setdefault("last_notified", {})
        st.setdefault("last_block_alert", 0)
        st.setdefault("baseline_done", False)
        return st
    except Exception:
        return {"ver": STATE_VERSION, "avail": [], "last_notified": {}, "last_block_alert": 0, "baseline_done": False}


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

    if len(body) < 2500 and any(w in f"{title} {body.lower()}" for w in BLOCK_WORDS):
        return "BLOCKED", {}

    cands = page.evaluate(JS_COLLECT, {"book": BOOK_RE, "bad": UNAVAILABLE_RE})

    if first_run and SAVE_DEBUG:
        samples = "\n\n".join(f"--- كارت {c['idx']} ---\n{c['html']}" for c in cands if c.get("html"))
        if CLOUD:
            log(f"عنوان الصفحة: {title!r} - طول النص: {len(body)} - أزرار حجز: {len(cands)}")
            log("بداية النص:\n" + body[:1500])
            log("HTML الكروت:\n" + samples[:3000])
        else:
            try:
                folder = os.path.join(os.path.expanduser("~"), "Documents")
                page.screenshot(path=os.path.join(folder, "tazkarti_debug.png"), full_page=True)
                with open(os.path.join(folder, "tazkarti_debug.txt"), "w", encoding="utf-8") as f:
                    f.write(f"URL: {page.url}\nTitle: {title}\n\n{body}\n\n=== HTML الكروت ===\n{samples}")
                log(f"حفظت ملفات الفحص في: {folder}")
            except Exception as e:
                log(f"ماقدرتش أحفظ ملفات الفحص (مش مشكلة): {e}")

    found = {}
    for c in cands:
        if c["disabled"] or c["bad"]:
            continue
        lines = clean_lines(c["text"])
        if len(lines) < 2:
            continue
        if NAV_NOISE.search(" ".join(lines)):
            continue   # ده مش كارت ماتش، ده قايمة الموقع
        if TEAMS_FILTER and not any(t.lower() in " ".join(c["teams"] + lines).lower() for t in TEAMS_FILTER):
            continue
        href = c["href"]
        if not href or href.rstrip("#/") == url.rstrip("#/"):
            href = ""
        found[make_key(c["teams"] + lines, href)] = {
            "lines": lines[:8], "teams": c["teams"][:2], "href": href, "idx": c["idx"], "url": url,
        }
    return ("OK" if found else "EMPTY"), found


def describe(info):
    lines = [ln for ln in info["lines"] if not (len(ln) <= 40 and re.search(BOOK_RE, ln, re.I))]
    parts = []
    if info["teams"]:
        parts.append("⚽ " + "  🆚  ".join(info["teams"]))
    parts += lines[:5]
    return "\n".join(parts)


def resolve_link(page, info):
    """لو اللينك مش ظاهر في الصفحة: بنضغط على زرار الحجز بس عشان نقرا اللينك اللي بيفتحه (مابنحجزش حاجة)."""
    try:
        ctx = page.context
        page.goto("about:blank")
        page.goto(info["url"], wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        page.wait_for_timeout(5000)
        page.evaluate(JS_COLLECT, {"book": BOOK_RE, "bad": UNAVAILABLE_RE})   # بيعيد وسم الأزرار
        before, count = page.url, len(ctx.pages)
        page.locator(f'[data-tzk="{info["idx"]}"]').first.click(timeout=8000)
        page.wait_for_timeout(3500)
        target = page.url
        if len(ctx.pages) > count:
            extra = ctx.pages[-1]
            target = extra.url
            extra.close()
        low = target.lower()
        if target and target != before and "login" not in low and "signin" not in low:
            return target
    except Exception as e:
        log(f"ماقدرتش أجيب لينك الحجز: {e}")
    return ""


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
            listing = "\n\n".join(
                describe(i) + (f"\n🔗 {i['href']}" if i["href"] else "") for i in list(current.values())[:8]
            )
            send_telegram(f"🧪 اختبار: المتاح للحجز دلوقتي ({len(current)}):\n\n{listing}")
        else:
            send_telegram("🧪 اختبار: الصفحة اتفتحت بس مفيش ماتشات متاحة للحجز دلوقتي. هبلغك أول ما يتفتح حجز.")
        return

    for key, info in current.items():
        last = state["last_notified"].get(key, 0)
        if key not in prev and now - last > RENOTIFY_AFTER_HOURS * 3600:
            state["last_notified"][key] = now
            link = info["href"] or resolve_link(page, info)
            if not link:
                link = f"{info['url']}\n(افتح الصفحة واختار الماتش)"
            send_telegram(f"🎟️ ماتش متاح للحجز دلوقتي:\n\n{describe(info)}\n\n🔗 {link}")
            log(f"إشعار: {info['teams'] or info['lines'][:2]}")
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
