"""WhatsApp alerts (plus a local log file that always works).

Providers
* callmebot - free, sends WhatsApp messages to your own number. One-time
  setup: save +34 644 66 32 62 in your contacts and send it
  "I allow callmebot to send me messages"; it replies with your API key.
  Free third-party service - fine for personal alerts, no delivery guarantee.
* telegram  - free and very reliable backup: create a bot with @BotFather,
  set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, and NOTIFY_PROVIDER=telegram.
* meta      - official WhatsApp Business Cloud API (needs a Meta business
  app). Free-form text only reaches you within 24 h of your last message
  to the business number; outside that window Meta requires an approved
  template. Use this if you want a fully official channel.

Every message is also appended to state/alerts.log, so nothing is lost if
WhatsApp is down.
"""
from __future__ import annotations

import logging
import os
import re
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import requests

from .config import env

log = logging.getLogger("swingbot.notify")
MAX_LEN = 1500


REQUIRED_ENV = {
    "callmebot": ("WHATSAPP_PHONE", "CALLMEBOT_API_KEY"),
    "meta": ("WHATSAPP_PHONE", "META_WA_TOKEN", "META_WA_PHONE_NUMBER_ID"),
    "telegram": ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"),
}


def callmebot_ok(body: str) -> bool:
    """CallMeBot answers HTTP 200 even when the API key is wrong - read the text."""
    b = (body or "").lower()
    return not any(bad in b for bad in ("invalid", "error", "not allowed", "not activated", "wrong"))


class Notifier:
    """Sends alerts. Provider: callmebot | meta | telegram | none.

    The NOTIFY_PROVIDER environment variable overrides the settings file, so you
    can switch channel from GitHub repository variables without editing code.
    """

    def __init__(self, provider: str, log_dir: str | Path):
        self.provider = os.environ.get("NOTIFY_PROVIDER") or provider
        self.log_path = Path(log_dir) / "alerts.log"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)

    def send(self, text: str, urgent: bool = False) -> bool:
        prefix = "🚨 " if urgent else ""
        text = f"{prefix}SwingBot: {text}"
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {text}\n")
        log.info(text)
        if self.provider == "none":
            return True
        ok = True
        for part in _split(text):
            ok &= self._send_one(part)
        return ok

    def _send_one(self, text: str) -> bool:
        if self.provider not in REQUIRED_ENV:
            log.error("unknown notification provider %s", self.provider)
            return False
        try:  # configuration problems are not worth retrying
            for name in REQUIRED_ENV[self.provider]:
                env(name)
        except RuntimeError as e:
            log.warning("%s not configured: %s", self.provider, e)
            return False
        for attempt in range(3):
            try:
                if self.provider == "callmebot":
                    url = (
                        "https://api.callmebot.com/whatsapp.php?phone="
                        + urllib.parse.quote(env("WHATSAPP_PHONE"))
                        + "&text=" + urllib.parse.quote(text)
                        + "&apikey=" + urllib.parse.quote(env("CALLMEBOT_API_KEY"))
                    )
                    r = requests.get(url, timeout=30)
                    body = re.sub(r"<[^>]+>", " ", r.text or "")
                    body = " ".join(body.split())[:300]
                    log.info("callmebot replied (%s): %s", r.status_code, body)
                    if r.status_code < 300 and callmebot_ok(body):
                        return True
                    if r.status_code < 300:
                        log.warning("callmebot rejected the message (wrong API key / not activated?): %s", body)
                        return False  # retrying won't fix a bad key
                elif self.provider == "meta":
                    r = requests.post(
                        f"https://graph.facebook.com/v21.0/{env('META_WA_PHONE_NUMBER_ID')}/messages",
                        headers={"Authorization": f"Bearer {env('META_WA_TOKEN')}"},
                        json={"messaging_product": "whatsapp", "to": env("WHATSAPP_PHONE").lstrip("+"),
                              "type": "text", "text": {"body": text}},
                        timeout=20,
                    )
                    if r.status_code < 300:
                        return True
                else:  # telegram
                    r = requests.post(
                        f"https://api.telegram.org/bot{env('TELEGRAM_BOT_TOKEN')}/sendMessage",
                        json={"chat_id": env("TELEGRAM_CHAT_ID"), "text": text},
                        timeout=20,
                    )
                    if r.status_code < 300 and r.json().get("ok"):
                        return True
                log.warning("%s send failed (%s): %s", self.provider, r.status_code, r.text[:200])
                if 400 <= r.status_code < 500 and r.status_code != 429:
                    return False  # bad token / chat id: retrying won't help
            except Exception as e:  # network
                log.warning("%s send error: %s", self.provider, e)
            time.sleep(2 ** attempt * 2)
        return False


def _split(text: str) -> list[str]:
    if len(text) <= MAX_LEN:
        return [text]
    parts, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > MAX_LEN and cur:
            parts.append(cur)
            cur = ""
        cur += line
    if cur:
        parts.append(cur)
    return parts
