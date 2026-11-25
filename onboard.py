#!/usr/bin/env python3
"""Optional Lightweight Single-VM Onboarding Server.

Run once during initial VM setup:
    python onboard.py
Opens a temporary local setup page on port 8080 to configure .env,
display the VM's static public IP, and verify Kotak Neo + Telegram.
"""

from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
HTML_PATH = ROOT / "onboarding.html"
ENV_PATH = ROOT / ".env"


def _get_public_ip() -> str:
    try:
        return requests.get("https://api.ipify.org", timeout=4).text.strip()
    except Exception:
        return "Unavailable (check manually)"


def _apply_env_to_process(env_content: str) -> None:
    for line in env_content.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ[k.strip()] = v.strip()


class OnboardingHandler(BaseHTTPRequestHandler):
    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.path in ("/", "/onboarding.html"):
            body = HTML_PATH.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/info":
            self._send_json(200, {"public_ip": _get_public_ip()})
            return
        self._send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        if self.path != "/api/save":
            self._send_json(404, {"error": "Not found"})
            return
        length = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(length).decode("utf-8")
        try:
            data = json.loads(raw)
            env_content = str(data.get("env_content", ""))
            ENV_PATH.write_text(env_content, encoding="utf-8")
            _apply_env_to_process(env_content)

            from broker_kotak import kotak_login
            from telegram_notifier import is_configured, send_telegram_message

            k_res = kotak_login()
            tg_ok = False
            if is_configured():
                send_telegram_message("✅ <b>Onboarding Verified</b>: Single-Tenant Trading Engine connected.", blocking=True)
                tg_ok = True

            if k_res.get("ok"):
                self._send_json(200, {
                    "ok": True,
                    "message": f"Saved .env! Kotak Neo authenticated (UCC: {k_res.get('ucc')}). Telegram: {'OK' if tg_ok else 'Skipped'}.",
                })
            else:
                self._send_json(200, {
                    "ok": False,
                    "message": f"Saved .env, but Kotak Neo auth failed: {k_res.get('error')}",
                })
        except Exception as exc:
            self._send_json(500, {"ok": False, "message": f"Error: {exc}"})

    def log_message(self, fmt: str, *args) -> None:
        pass


if __name__ == "__main__":
    port = int(os.getenv("ONBOARD_PORT", "8080"))
    print(f"Onboarding UI running at http://0.0.0.0:{port}/  (Ctrl+C to stop)")
    HTTPServer(("0.0.0.0", port), OnboardingHandler).serve_forever()
