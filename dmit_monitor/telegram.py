"""No automatic retry for ambiguous delivery; Telegram sendMessage has no idempotency key."""
import json
import re
import urllib.parse
from dataclasses import dataclass

from .http import SafeError, Transport


@dataclass
class Delivery:
    status: str
    receipt: int = 0
    retry_after: int = 300


class Telegram:
    def __init__(self, token, chat_id, transport=None):
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token or ""):
            raise SafeError("telegram_token_missing_or_invalid")
        if not re.fullmatch(r"-?\d+", chat_id or ""):
            raise SafeError("telegram_chat_id_missing_or_invalid")
        self.token = token
        self.chat_id = chat_id
        self.transport = transport or Transport({"api.telegram.org"})

    def send(self, text):
        try:
            response = self.transport.request("POST", f"https://api.telegram.org/bot{self.token}/sendMessage",
                urllib.parse.urlencode({"chat_id": self.chat_id, "text": text,
                                        "disable_web_page_preview": "true"}).encode(),
                {"Content-Type": "application/x-www-form-urlencoded"})
        except SafeError:
            return Delivery("uncertain")
        try:
            payload = json.loads(response.body)
        except ValueError:
            return Delivery("uncertain")
        if response.status == 200 and payload.get("ok") is True:
            receipt = payload.get("result", {}).get("message_id")
            return Delivery("sent", receipt) if type(receipt) is int else Delivery("uncertain")
        # An explicit API rejection proves the message was not accepted.
        if payload.get("ok") is False and 400 <= response.status < 500:
            retry = payload.get("parameters", {}).get("retry_after", 300)
            return Delivery("pending", retry_after=max(300, min(86400, int(retry))))
        return Delivery("uncertain")
