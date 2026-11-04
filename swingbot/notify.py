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

import json
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
    "telegram": ("TELEGRAM_BOT_TOKEN",),  # chat id is auto-detected if missing or wrong
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

    def __init__(self, provider: str, log_dir: str | Path, store_path: str | Path | None = None):
        self.provider = os.environ.get("NOTIFY_PROVIDER") or provider
        self.log_path = Path(log_dir) / "alerts.log"
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        # where an auto-detected Telegram chat id is remembered (journal/ on GitHub, so it persists)
        self.store_path = Path(store_path) if store_path else Path(log_dir) / "telegram.json"

    # -- telegram chat id ----------------------------------------------------
    def _stored_chat_id(self) -> str | None:
        try:
            return json.loads(self.store_path.read_text()).get("chat_id")
        except (OSError, ValueError):
            return None

    def _save_chat_id(self, chat_id: str) -> None:
        try:
            self.store_path.parent.mkdir(parents=True, exist_ok=True)
            self.store_path.write_text(json.dumps({"chat_id": chat_id}))
        except OSError as e:
            log.warning("could not save telegram chat id: %s", e)

    @staticmethod
    def discover_chat_id(token: str) -> str | None:
        """Chat id of the most recent private message sent to the bot (Telegram keeps ~24 h)."""
        r = requests.get(f"https://api.telegram.org/bot{token}/getUpdates", timeout=20)
        for upd in reversed(r.json().get("result", []) if r.status_code < 300 else []):
            for key in ("message", "edited_message", "my_chat_member"):
                chat = (upd.get(key) or {}).get("chat") or {}
                if chat.get("type") == "private" and chat.get("id") is not None:
                    return str(chat["id"])
        return None

    def _telegram_send(self, text: str) -> bool:
        token = env("TELEGRAM_BOT_TOKEN").strip().strip('"').strip("'")
        tried: list[str] = []
        # tolerate pasted extras like quotes, spaces or 'id:' - keep digits and a leading minus
        configured = re.sub(r"[^0-9-]", "", os.environ.get("TELEGRAM_CHAT_ID", ""))
        candidates = [configured, self._stored_chat_id()]
        for attempt in range(2):
            for chat_id in [c for c in candidates if c and c not in tried]:
                tried.append(chat_id)
                r = requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                  json={"chat_id": chat_id, "text": text}, timeout=20)
                if r.status_code < 300 and r.json().get("ok"):
                    if chat_id != self._stored_chat_id():
                        self._save_chat_id(chat_id)
                    return True
                log.warning("telegram send to chat %s failed (%s): %s", chat_id, r.status_code, r.text[:200])
                if r.status_code in (401, 404):
                    log.warning("telegram: TELEGRAM_BOT_TOKEN is wrong (copy it again from @BotFather)")
                    return False
            if attempt == 0:  # nothing worked: look up who has messaged the bot
                found = self.discover_chat_id(token)
                if found and found not in tried:
                    log.warning("telegram: auto-detected your chat id %s - using it from now on", found)
                    candidates = [found]
                    continue
                log.warning("telegram: could not find your chat. Open YOUR bot in Telegram, press START, "
                            "send 'hi', then run the workflow again within 24 hours.")
                return False
        return False

    def send(self, text: str, urgent: bool = False) -> bool:
        prefix = "🚨 SwingBot ALERT: " if urgent else ("" if text.startswith("🤖") else "🤖 ")
        text = f"{prefix}{text}"
        with open(self.log_path, "a", encoding="utf-8") as fh:
            fh.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {text}\n")
        log.info(text)
        if self.provider == "none":
            return True
        ok = True
        limit = 3800 if self.provider == "telegram" else MAX_LEN  # Telegram allows 4096 chars per message
        for part in _split(text, limit):
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
                    return self._telegram_send(text)
                log.warning("%s send failed (%s): %s", self.provider, r.status_code, r.text[:200])
                if 400 <= r.status_code < 500 and r.status_code != 429:
                    return False  # bad token / chat id: retrying won't help
            except Exception as e:  # network
                log.warning("%s send error: %s", self.provider, e)
            time.sleep(2 ** attempt * 2)
        return False


def _split(text: str, limit: int = MAX_LEN) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for line in text.splitlines(keepends=True):
        if len(cur) + len(line) > limit and cur:
            parts.append(cur)
            cur = ""
        cur += line
    if cur:
        parts.append(cur)
    return parts
