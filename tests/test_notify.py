"""Alert delivery must not report success when the provider rejected the message."""
import pytest

from swingbot import notify
from swingbot.notify import Notifier, callmebot_ok


class Resp:
    def __init__(self, status, text="", payload=None):
        self.status_code, self.text, self._p = status, text, payload or {}

    def json(self):
        return self._p


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("WHATSAPP_PHONE", "+910000000000")
    monkeypatch.setenv("CALLMEBOT_API_KEY", "123")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "1")
    monkeypatch.delenv("NOTIFY_PROVIDER", raising=False)
    monkeypatch.setattr(notify.time, "sleep", lambda s: None)


def test_callmebot_text_check():
    assert callmebot_ok("Message queued. You will receive it in a few seconds.")
    assert not callmebot_ok("APIKey is invalid. Please check the APIKey")
    assert not callmebot_ok("ERROR: phone number not activated")


def test_callmebot_200_with_invalid_key_is_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(notify.requests, "get", lambda *a, **k: Resp(200, "<p>APIKey is invalid.</p>"))
    assert Notifier("callmebot", tmp_path).send("hi") is False


def test_callmebot_success(monkeypatch, tmp_path):
    monkeypatch.setattr(notify.requests, "get", lambda *a, **k: Resp(200, "Message queued."))
    assert Notifier("callmebot", tmp_path).send("hi") is True


def test_missing_config_fails_fast(monkeypatch, tmp_path):
    monkeypatch.delenv("CALLMEBOT_API_KEY")
    assert Notifier("callmebot", tmp_path).send("hi") is False


def test_telegram_and_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("NOTIFY_PROVIDER", "telegram")
    monkeypatch.setattr(notify.requests, "post", lambda *a, **k: Resp(200, payload={"ok": True}))
    n = Notifier("callmebot", tmp_path)
    assert n.provider == "telegram" and n.send("hi") is True
    monkeypatch.setattr(notify.requests, "post", lambda *a, **k: Resp(401, "Unauthorized", {"ok": False}))
    assert n.send("hi") is False


def test_telegram_autodetects_chat_id(monkeypatch, tmp_path):
    monkeypatch.setenv("NOTIFY_PROVIDER", "telegram")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", " '7123456789' ")  # wrong id (e.g. the bot's own id)
    sent = []

    def post(url, json=None, timeout=None):
        sent.append(json["chat_id"])
        if json["chat_id"] == "555":
            return Resp(200, payload={"ok": True})
        return Resp(400, '{"ok":false,"description":"Bad Request: chat not found"}', {"ok": False})

    updates = {"ok": True, "result": [{"update_id": 1, "message": {"chat": {"id": 555, "type": "private"}}}]}
    monkeypatch.setattr(notify.requests, "post", post)
    monkeypatch.setattr(notify.requests, "get", lambda *a, **k: Resp(200, payload=updates))
    store = tmp_path / "journal" / "telegram.json"
    n = Notifier("telegram", tmp_path, store_path=store)
    assert n.send("hi") is True
    assert sent == ["7123456789", "555"]
    assert '"555"' in store.read_text()
    # next run: remembered id works even with the wrong secret still set
    sent.clear()
    assert Notifier("telegram", tmp_path, store_path=store).send("again") is True
    assert sent[-1] == "555"


def test_telegram_no_chat_found(monkeypatch, tmp_path):
    monkeypatch.setenv("NOTIFY_PROVIDER", "telegram")
    monkeypatch.delenv("TELEGRAM_CHAT_ID")
    monkeypatch.setattr(notify.requests, "get", lambda *a, **k: Resp(200, payload={"ok": True, "result": []}))
    assert Notifier("telegram", tmp_path).send("hi") is False
