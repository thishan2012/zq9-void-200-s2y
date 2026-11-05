"""Tiny Telegram bot logger.

Uses the Bot API sendMessage endpoint. Messages are HTML-escaped and chunked
to Telegram's 4096 character limit. Failures are logged but never crash a sync.
"""
from __future__ import annotations

import html
import logging
import time

import requests

log = logging.getLogger(__name__)

_LIMIT = 3900


class TelegramLogger:
    def __init__(
        self,
        bot_token: str = "",
        chat_id: str = "",
        enabled: bool = True,
        batch_size: int = 25,
    ):
        self.bot_token = bot_token.strip()
        self.chat_id = chat_id.strip()
        self.enabled = enabled and bool(self.bot_token and self.chat_id)
        self.session = requests.Session()
        self._last_send = 0.0
        self.batch_size = max(1, batch_size)
        self._track_buffer: list[str] = []

    def send(self, text: str, disable_preview: bool = True) -> None:
        if not self.enabled:
            return
        # Basic rate limiting: ~1 message / second to the same chat.
        wait = 1.05 - (time.time() - self._last_send)
        if wait > 0:
            time.sleep(wait)
        for chunk in self._chunks(text):
            try:
                resp = self.session.post(
                    f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                    json={
                        "chat_id": self.chat_id,
                        "text": chunk,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": disable_preview,
                    },
                    timeout=20,
                )
                self._last_send = time.time()
                if resp.status_code != 200:
                    log.warning("Telegram send failed: %s %s", resp.status_code, resp.text[:200])
            except requests.RequestException as exc:
                log.warning("Telegram send error: %s", exc)

    @staticmethod
    def _chunks(text: str) -> list[str]:
        text = text or ""
        if len(text) <= _LIMIT:
            return [text]
        chunks = []
        while text:
            if len(text) <= _LIMIT:
                chunks.append(text)
                break
            cut = text.rfind("\n", 0, _LIMIT)
            if cut < _LIMIT // 2:
                cut = _LIMIT
            chunks.append(text[:cut])
            text = text[cut:].lstrip("\n")
        return chunks

    def info(self, text: str) -> None:
        log.info(text)
        self.send(text)

    def warn(self, text: str) -> None:
        log.warning(text)
        self.send("⚠️ " + text)

    def error(self, text: str) -> None:
        log.error(text)
        self.send("🛑 " + text)

    def track_added(self, track_display: str, method: str, video_url: str) -> None:
        link = f' · <a href="{html.escape(video_url)}">YT</a>' if video_url else ""
        self._buffer_track(
            f"✅ {html.escape(track_display)} <code>[{html.escape(method or 'cached')}]</code>{link}"
        )

    def track_unmatched(self, track_display: str, tried: int) -> None:
        self._buffer_track(f"❌ {html.escape(track_display)} ({tried} methods)")

    def _buffer_track(self, line: str) -> None:
        # Batch per-track lines so a 500-song run sends ~20 messages, not 500.
        # This removes the ~1s/message rate-limit sleep from the hot path.
        self._track_buffer.append(line)
        if len(self._track_buffer) >= self.batch_size:
            self.flush_tracks()

    def flush_tracks(self) -> None:
        if not self._track_buffer:
            return
        header = f"🎧 <b>{len(self._track_buffer)} track updates</b>\n"
        self.send(header + "\n".join(self._track_buffer))
        self._track_buffer = []


def escape(text: str) -> str:
    return html.escape(text or "")
