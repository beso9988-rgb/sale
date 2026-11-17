#!/usr/bin/env python3
"""Forward Tazkarti Plus notifications from a private ntfy topic to the existing Telegram bot."""
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime

NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
SEEN_FILE = "tazkarti_ntfy_seen.json"
MAX_SEEN = 500


def log(message):
    print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}", flush=True)


def load_seen():
    try:
        with open(SEEN_FILE, encoding="utf-8") as f:
            values = json.load(f)
        return values if isinstance(values, list) else []
    except (OSError, json.JSONDecodeError):
        return []


def save_seen(values):
    tmp = SEEN_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(values[-MAX_SEEN:], f)
    os.replace(tmp, SEEN_FILE)


def send_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": text[:3900],
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError("Telegram API rejected the message")


def main():
    if not NTFY_TOPIC or not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("Missing NTFY_TOPIC or Telegram environment variables")

    seen = load_seen()
    # A short replay window plus persisted message IDs prevents duplicates after reconnects.
    url = f"https://ntfy.sh/{NTFY_TOPIC}/json?since=1h"
    log("Notification bridge started; listening for new Tazkarti Plus alerts.")

    while True:
        try:
            request = urllib.request.Request(
                url,
                headers={"Accept": "application/x-ndjson", "User-Agent": "tazkarti-notification-bridge/1.0"},
            )
            with urllib.request.urlopen(request, timeout=90) as response:
                for raw in response:
                    if not raw.strip():
                        continue
                    try:
                        event = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue
                    if event.get("event") != "message":
                        continue
                    message_id = event.get("id")
                    if message_id and message_id in seen:
                        continue
                    message = str(event.get("message", "")).strip()
                    if not message:
                        continue
                    stamp = event.get("time")
                    received = datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M:%S") if isinstance(stamp, (int, float)) else datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                    text = "🔔 إشعار من تطبيق Tazkarti Plus\\n" + message + f"\\n\\nوقت الاستقبال: {received}"
                    for attempt in range(3):
                        try:
                            send_telegram(text)
                            log(f"Forwarded Tazkarti Plus notification (id={message_id or 'none'}).")
                            break
                        except Exception as exc:
                            log(f"Telegram forwarding attempt {attempt + 1}/3 failed: {type(exc).__name__}")
                            if attempt == 2:
                                raise
                            time.sleep(2)
                    if message_id:
                        seen.append(message_id)
                        seen = seen[-MAX_SEEN:]
                        save_seen(seen)
        except Exception as exc:
            log(f"ntfy stream interrupted: {type(exc).__name__}; reconnecting in 5 seconds.")
            time.sleep(5)


if __name__ == "__main__":
    main()
