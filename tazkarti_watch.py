"""
مراقب تذكرتي.
- GitHub Actions: فحص واحد لكل تشغيل مجدول.
- VPS: عند ضبط TARZKARTI_CONTINUOUS=1 يعمل باستمرار، افتراضيًا كل 60 ثانية.
- يرسل Telegram عند تغيّر قائمة المباريات فقط، مع تقرير تأسيسي عند أول تشغيل.
- يقارن حقول واجهات JSON للمباريات والحجز، ويسجل التغييرات المهمة في ملف تاريخي دون حفظ أجسام الاستجابات كاملة.
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
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
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

# على VPS اضبط TARZKARTI_POLL_SECONDS=60 لمراقبة كل دقيقة.
# ابدأ بفاصل 60 ثانية لتقليل الضغط على الموقع؛ لا توجد ضمانة بتنبيه قبل نزول التذاكر.
POLL_INTERVAL_SECONDS = max(30, int(os.getenv("TARZKARTI_POLL_SECONDS", "60")))
HEADLESS = os.getenv("TARZKARTI_HEADLESS", "1").strip() == "1"
SAVE_DEBUG = True
RENOTIFY_AFTER_HOURS = 1
# ===================================

BOOK_RE = "احجز|حجز|اشتر|شراء|book|buy|get tickets"
UNAVAILABLE_RE = "نفذ|نفد|sold|انتهت|غير متاح|غير متوفر|unavailable|مغلق|closed|قريب|soon|لا توجد"

GITHUB_ACTIONS = os.getenv("GITHUB_ACTIONS", "").lower() == "true"
CONTINUOUS_MODE = os.getenv("TARZKARTI_CONTINUOUS", "0") == "1" and not GITHUB_ACTIONS
CLOUD = GITHUB_ACTIONS or CONTINUOUS_MODE
if GITHUB_ACTIONS:
    BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
    HEADLESS = True

STATE_FILE = "seen_tazkarti.json"
API_STATE_FILE = "tazkarti_api_watch.json"
STATE_VERSION = 3
API_STATE = {}

# راقب واجهات بيانات المباريات والحجز فقط، وتجاهل ملفات اللغة والموارد الثابتة.
WATCH_API_SUFFIXES = ("/data/matches-list-json.json", "/data/fanQueuesMatch-list-json.json")
API_ALERT_COOLDOWN_SECONDS = 300  # منع تكرار التنبيه لنفس الواجهة أكثر من مرة كل 5 دقائق
API_HISTORY_FILE = "tazkarti_api_history.jsonl"
API_SNAPSHOT_MAX_BYTES = 200_000
API_DIFF_MAX_FIELDS = 20

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
    """يرسل الرسالة على أجزاء عند الحاجة، مع مراعاة حد تليجرام."""
    if not BOT_TOKEN or not CHAT_ID:
        log("لم يتم الإرسال: أضف TELEGRAM_BOT_TOKEN و TELEGRAM_CHAT_ID في Secrets/Environment.")
        return

    api = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    remaining = str(text)
    chunks = []
    while remaining:
        if len(remaining) <= 3800:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, 3800)
        if cut < 1000:
            cut = 3800
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")

    for chunk in chunks:
        data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": chunk}).encode()
        try:
            with urllib.request.urlopen(api, data=data, timeout=20) as response:
                response.read()
        except Exception as e:
            log(f"فشل إرسال تليجرام: {e}")


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
        if st.get("ver") != STATE_VERSION:
            raise ValueError("old state")
        st.setdefault("avail", [])
        st.setdefault("avail_info", {})
        st.setdefault("last_notified", {})
        st.setdefault("last_block_alert", 0)
        st.setdefault("baseline_done", False)
        return st
    except Exception:
        return {"ver": STATE_VERSION, "avail": [], "avail_info": {}, "last_notified": {}, "last_block_alert": 0, "baseline_done": False}


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


def send_availability_report(current):
    """يرسل تقريرًا كاملًا بالمباريات المتاحة بعد كل فحص ناجح."""
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    if not current:
        send_telegram(
            f"🎟️ تقرير تذكرتي | {stamp}\n\n"
            "لا توجد مباريات متاحة للحجز حاليًا."
        )
        return

    entries = []
    for number, info in enumerate(current.values(), start=1):
        item = f"{number}) {describe(info)}"
        link = info.get("href") or info.get("url", "")
        if link:
            item += f"\n🔗 {link}"
        entries.append(item)

    report = (
        f"🎟️ تقرير تذكرتي | {stamp}\n"
        f"عدد المباريات المتاحة: {len(entries)}\n\n"
        + "\n\n".join(entries)
    )
    send_telegram(report)


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
        send_telegram("⚠️ موقع تذكرتي طلب تحقق أو حجب الفحص. أوقفت تغييرات الحالة لتجنب تنبيهات غير صحيحة.")

    if not healthy:
        return

    prev_keys = set(state.get("avail", []))
    prev_info = state.get("avail_info", {}) or {}

    # أول فحص يثبت خط الأساس ويرسل تقريرًا واحدًا فقط.
    if not state["baseline_done"]:
        state["baseline_done"] = True
        state["avail"] = list(current)
        state["avail_info"] = current
        send_availability_report(current)
        return

    current_keys = set(current)
    added = current_keys - prev_keys
    removed = prev_keys - current_keys
    updated = {
        key for key in (current_keys & prev_keys)
        if key in prev_info and any(
            current[key].get(field) != prev_info[key].get(field)
            for field in ("lines", "teams", "href")
        )
    }

    if added:
        entries = []
        for number, key in enumerate(sorted(added), start=1):
            info = current[key]
            link = info.get("href") or info.get("url", "")
            entries.append(f"{number}) {describe(info)}" + (f"\n🔗 {link}" if link else ""))
        send_telegram(
            "🟢 تغيير في تذكرتي: ظهرت مباراة/مباريات متاحة جديدة\n\n"
            + "\n\n".join(entries)
        )
        for key in added:
            state["last_notified"][key] = now
            log(f"إتاحة جديدة: {current[key]['teams'] or current[key]['lines'][:2]}")

    if updated:
        entries = [describe(current[key]) for key in sorted(updated)]
        send_telegram("🔄 تحديث في تفاصيل مباراة على تذكرتي:\n\n" + "\n\n".join(entries))

    if removed:
        entries = []
        for key in sorted(removed):
            info = prev_info.get(key)
            if info:
                entries.append(describe(info))
        if entries:
            send_telegram("⚪ لم تعد هذه المباراة ظاهرة كمتاحة للحجز:\n\n" + "\n\n".join(entries))
        else:
            send_telegram(f"⚪ تغيّرت قائمة المباريات المتاحة: اختفى {len(removed)} عنصر/عناصر من القائمة.")

    if not added and not removed and not updated:
        log("لا يوجد تغيير في قائمة المباريات المتاحة؛ لن يتم إرسال رسالة.")

    state["avail"] = list(current)
    state["avail_info"] = current


def launch(p):
    args = ["--no-sandbox", "--disable-blink-features=AutomationControlled"]
    if not HEADLESS and not CLOUD:
        try:
            b = p.chromium.launch(headless=False, channel="msedge", args=args)
            log("شغّال بإيدج الحقيقي")
            return b
        except Exception as e:
            log(f"إيدج مش متاح ({e}) هستخدم Chromium العادي")
    return p.chromium.launch(headless=True if CLOUD else HEADLESS, args=args)


def load_api_state():
    try:
        with open(API_STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_api_state():
    try:
        with open(API_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(API_STATE, f, ensure_ascii=False)
    except Exception as e:
        log(f"ماقدرتش أحفظ سجل مراقبة API: {e}")


def _short_value(value):
    """قيمة مختصرة للعرض في سجل التغيير، دون تخزين جسم الاستجابة كاملًا."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:120] + ("…" if len(value) > 120 else "")
    if isinstance(value, list):
        return f"[قائمة: {len(value)} عنصر]"
    if isinstance(value, dict):
        keys = list(value.keys())[:8]
        return "{حقول: " + ", ".join(str(k) for k in keys) + ("…" if len(value) > 8 else "") + "}"
    return str(type(value).__name__)


def _collect_json_changes(old_value, new_value, path="$", depth=0, output=None):
    """مقارنة حقول JSON مع تجنب التوسع داخل القوائم الكبيرة."""
    if output is None:
        output = []
    if len(output) >= API_DIFF_MAX_FIELDS:
        return output
    if isinstance(old_value, dict) and isinstance(new_value, dict) and depth < 5:
        keys = sorted(set(old_value) | set(new_value), key=str)
        for key in keys:
            if len(output) >= API_DIFF_MAX_FIELDS:
                break
            child_path = f"{path}.{key}"
            if key not in old_value:
                output.append({"field": child_path, "old": "(غير موجود)", "new": _short_value(new_value[key])})
            elif key not in new_value:
                output.append({"field": child_path, "old": _short_value(old_value[key]), "new": "(تم حذفه)"})
            else:
                _collect_json_changes(old_value[key], new_value[key], child_path, depth + 1, output)
        return output
    if old_value != new_value:
        output.append({"field": path, "old": _short_value(old_value), "new": _short_value(new_value)})
    return output


def _append_api_history(record):
    try:
        with open(API_HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        log(f"ماقدرتش أضيف لسجل تغييرات API: {e}")


def attach_api_observer(page):
    """يراقب تغيّر حقول واجهات المباريات والحجز، ويسجل ملخصًا تاريخيًا ويرسل تنبيهًا عند التغيير."""
    def on_response(response):
        try:
            parsed = urllib.parse.urlsplit(response.url)
            host = (parsed.hostname or "").lower()
            if host != "tazkarti.com" and not host.endswith(".tazkarti.com"):
                return

            endpoint = f"{host}{parsed.path}"
            if not any(parsed.path.endswith(suffix) for suffix in WATCH_API_SUFFIXES):
                return

            content_type = response.headers.get("content-type", "").lower()
            if "json" not in content_type:
                return

            body = response.body()
            if not body or len(body) > 2 * 1024 * 1024:
                return

            try:
                payload = json.loads(body.decode("utf-8-sig"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                log(f"[API_PARSE_ERROR] {endpoint} استجابة JSON غير قابلة للتحليل")
                return

            now = int(time.time())
            digest = hashlib.sha256(body).hexdigest()[:20]
            old = API_STATE.get(endpoint)
            old = old if isinstance(old, dict) else {}
            status_changed = bool(old) and old.get("status") != response.status
            fingerprint_changed = bool(old) and old.get("sha256") != digest

            # عند أول تشغيل للنسخة التي تحفظ التفاصيل، نأخذ خط أساس جديدًا دون تنبيه كاذب.
            if old and "snapshot" not in old:
                API_STATE[endpoint] = {
                    "sha256": digest,
                    "status": response.status,
                    "last_seen": now,
                    "last_alert": int(old.get("last_alert", 0)),
                    "snapshot": payload if len(body) <= API_SNAPSHOT_MAX_BYTES else None,
                }
                log(f"[API_BASELINE_REFRESH] {endpoint} status={response.status} snapshot_saved={int(len(body) <= API_SNAPSHOT_MAX_BYTES)}")
                return

            changes = []
            if old and "snapshot" in old and old.get("snapshot") is not None:
                changes = _collect_json_changes(old["snapshot"], payload)

            entry = {
                "sha256": digest,
                "status": response.status,
                "last_seen": now,
                "last_alert": int(old.get("last_alert", 0)),
                "snapshot": payload if len(body) <= API_SNAPSHOT_MAX_BYTES else None,
            }

            if not old:
                log(f"[API_ENDPOINT] {endpoint} status={response.status} baseline=1")
                _append_api_history({
                    "time": datetime.now().isoformat(timespec="seconds"),
                    "event": "baseline",
                    "endpoint": endpoint,
                    "status": response.status,
                })
            elif fingerprint_changed or status_changed:
                stamp = datetime.now().isoformat(timespec="seconds")
                log(
                    f"[API_CHANGE] {endpoint} status={response.status} "
                    f"fingerprint_changed={int(fingerprint_changed)} "
                    f"status_changed={int(status_changed)} fields_changed={len(changes)}"
                )
                record = {
                    "time": stamp,
                    "event": "change",
                    "endpoint": endpoint,
                    "status": response.status,
                    "status_changed": status_changed,
                    "fingerprint_changed": fingerprint_changed,
                    "fields": changes,
                }
                _append_api_history(record)

                if now - entry["last_alert"] >= API_ALERT_COOLDOWN_SECONDS:
                    lines = [
                        "🔎 رصد تغيير في بيانات تذكرتي",
                        f"الواجهة: {parsed.path}",
                        f"HTTP: {response.status}",
                        f"الوقت: {stamp}",
                    ]
                    if changes:
                        lines.append("\nالحقول التي تغيّرت:")
                        for item in changes[:10]:
                            lines.append(
                                f"• {item['field']}: "
                                f"{item['old']} ← {item['new']}"
                            )
                    else:
                        lines.append("\nتغيرت بصمة الاستجابة، لكن لم يتوفر فرق حقول قابل للعرض.")
                    lines.append(
                        "\nقد يكون التغيير تحديثًا عاديًا أو إشارة مرتبطة بالمباريات/الحجز؛ "
                        "ليس تأكيدًا لبدء البيع."
                    )
                    send_telegram("\n".join(lines))
                    entry["last_alert"] = now
                    log(f"[API_ALERT_SENT] {endpoint}")
                else:
                    log(f"[API_ALERT_COOLDOWN] {endpoint} alert suppressed")

            API_STATE[endpoint] = entry
        except Exception as e:
            # فشل تحليل استجابة جانبية لا يجب أن يوقف مراقبة صفحة المباريات.
            log(f"[API_OBSERVER_ERROR] {e}")

    page.on("response", on_response)


def main():
    global API_STATE
    state = load_state()
    API_STATE = load_api_state()
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
        attach_api_observer(page)

        if GITHUB_ACTIONS or os.getenv("TARZKARTI_RUN_ONCE", "0") == "1":
            try:
                process(page, state, first_run=not state["baseline_done"])
            finally:
                save_state(state)
                save_api_state()
                browser.close()
            return

        mode_name = "السيرفر" if CONTINUOUS_MODE else "التشغيل المحلي"
        send_telegram(f"✅ مراقب تذكرتي بدأ العمل على {mode_name}. الفحص الدوري كل {POLL_INTERVAL_SECONDS} ثانية.")
        log(f"بدء المراقبة المستمرة؛ الفاصل {POLL_INTERVAL_SECONDS} ثانية")
        while True:
            try:
                process(page, state, first_run=not state["baseline_done"])
                save_state(state)
                save_api_state()
            except Exception as e:
                log(f"خطأ أثناء دورة المراقبة: {e}")
                save_api_state()
            time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
