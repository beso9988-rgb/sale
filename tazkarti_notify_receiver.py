#!/usr/bin/env python3
"""Authenticated HTTPS ingress for forwarding Tazkarti Plus Android notifications to Telegram."""
import hmac
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOST = "127.0.0.1"
PORT = 8765
EXPECTED_TOKEN = os.environ.get("INGEST_TOKEN", "")
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
EXPECTED_PACKAGE = "com.tzk.notifier"
MAX_BODY = 12_000


def send_telegram(title: str, body: str) -> None:
    message = "🔔 إشعار من Tazkarti Plus\n"
    if title:
        message += title[:300] + "\n"
    if body:
        message += body[:3000]
    message += "\n\nوقت الاستقبال: " + datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    payload = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": message[:3900],
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError("Telegram API did not accept message")


class Handler(BaseHTTPRequestHandler):
    server_version = "TazkartiNotify/1.0"

    def reply(self, status: int, text: str) -> None:
        data = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            self.reply(200, "ok")
        else:
            self.reply(404, "not found")

    def do_POST(self):
        if self.path != "/notify":
            self.reply(404, "not found")
            return
        supplied = self.headers.get("Authorization", "")
        if not EXPECTED_TOKEN or not hmac.compare_digest(supplied, "Bearer " + EXPECTED_TOKEN):
            self.reply(401, "unauthorized")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.reply(400, "bad content length")
            return
        if length < 1 or length > MAX_BODY:
            self.reply(413, "invalid body size")
            return
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        lines = raw.splitlines()
        package = lines[0].strip() if lines else ""
        title = lines[1].strip() if len(lines) > 1 else ""
        body = "\n".join(lines[2:]).strip() if len(lines) > 2 else ""
        if package != EXPECTED_PACKAGE:
            self.reply(403, "unexpected app package")
            return
        if not title and not body:
            self.reply(400, "empty notification")
            return
        try:
            send_telegram(title, body)
        except Exception as exc:
            print(f"[FORWARD_ERROR] {type(exc).__name__}", flush=True)
            self.reply(502, "telegram forwarding failed")
            return
        print(f"[FORWARDED] package={package} title_chars={len(title)} body_chars={len(body)}", flush=True)
        self.reply(200, "forwarded")

    def log_message(self, fmt, *args):
        # Avoid logging request paths, headers, tokens, or notification contents.
        print("[HTTP] notification request handled", flush=True)


def main():
    if not EXPECTED_TOKEN or not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("Missing INGEST_TOKEN / TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID")
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[STARTED] listening on {HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
