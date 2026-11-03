"""WhatsApp alerts (plus a local log file that always works).

Providers
* callmebot - free, sends WhatsApp messages to your own number. One-time
  setup: save +34 644 66 32 62 in your contacts and send it
  "I allow callmebot to send me messages"; it replies with your API key.
  Free third-party service - fine for personal alerts, no delivery guarantee.
* meta      - official WhatsApp Business Cloud API (needs a Meta business
  app). Free-form text only reaches you within 24 h of your last message
  to the business number; outside that window Meta requires an approved
  template. Use this if you want a fully official channel.

Every message is also appended to state/alerts.log, so nothing is lost if
WhatsApp is down.
"""
from __future__ import annotations

import logging
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import requests

from .config import env

log = logging.getLogger("swingbot.notify")
MAX_LEN = 1500


class Notifier:
    def __init__(self, provider: str, log_dir: str | Path):
        self.provider = provider
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
        try:  # configuration problems are not worth retrying
            env("WHATSAPP_PHONE")
            env("CALLMEBOT_API_KEY" if self.provider == "callmebot" else "META_WA_TOKEN")
        except RuntimeError as e:
            log.warning("whatsapp not configured: %s", e)
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
                    r = requests.get(url, timeout=20)
                elif self.provider == "meta":
                    r = requests.post(
                        f"https://graph.facebook.com/v21.0/{env('META_WA_PHONE_NUMBER_ID')}/messages",
                        headers={"Authorization": f"Bearer {env('META_WA_TOKEN')}"},
                        json={"messaging_product": "whatsapp", "to": env("WHATSAPP_PHONE").lstrip("+"),
                              "type": "text", "text": {"body": text}},
                        timeout=20,
                    )
                else:
                    log.error("unknown whatsapp provider %s", self.provider)
                    return False
                if r.status_code < 300:
                    return True
                log.warning("whatsapp send failed (%s): %s", r.status_code, r.text[:200])
            except Exception as e:  # network
                log.warning("whatsapp send error: %s", e)
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
