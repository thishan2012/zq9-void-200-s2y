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
    """One message per run.

    Everything a run has to say is collected in ``self.lines`` and sent once,
    at the end, by :meth:`flush` — a 500-track sync must not emit 500 messages
    (Telegram rate-limits and can restrict a chat for that). ``send()`` is still
    there for a single one-off message (the ``check`` command), but the engine
    only ever calls ``info`` / ``warn`` / ``error`` / ``track_*``.
    """

    def __init__(
        self,
        bot_token: str = "",
        chat_id: str = "",
        enabled: bool = True,
        batch_size: int = 25,
        one_message: bool = True,
    ):
        self.bot_token = bot_token.strip()
        self.chat_id = chat_id.strip()
        self.enabled = enabled and bool(self.bot_token and self.chat_id)
        self.session = requests.Session()
        self._last_send = 0.0
        self.batch_size = max(1, batch_size)
        self._track_buffer: list[str] = []
        # one_message=False restores the old chatty behaviour (debugging only).
        self.one_message = one_message
        # Hard ceiling for one Telegram message even with a long Added list.
        self.max_track_lines = 60
        self.lines: list[str] = []
        self.sent_messages = 0

    def send(self, text: str, disable_preview: bool = True) -> int:
        if not self.enabled or not (text or "").strip():
            return 0
        sent = 0
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
                    log.warning(
                        "Telegram send failed: %s %s", resp.status_code, resp.text[:200]
                    )
                else:
                    sent += 1
            except requests.RequestException as exc:
                log.warning("Telegram send error: %s", exc)
        return sent

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
        if self.one_message:
            self.lines.append(text)
        else:
            self.send(text)

    def warn(self, text: str) -> None:
        log.warning(text)
        if self.one_message:
            self.lines.append("⚠️ " + text)
        else:
            self.send("⚠️ " + text)

    def error(self, text: str) -> None:
        log.error(text)
        if self.one_message:
            self.lines.append("🛑 " + text)
        else:
            self.send("🛑 " + text)

    def track_added(self, track_display: str, method: str, video_url: str) -> None:
        link = f' · <a href="{html.escape(video_url)}">YT</a>' if video_url else ""
        self._buffer_track(
            f"✅ {html.escape(track_display)} <code>[{html.escape(method or 'cached')}]</code>{link}"
        )

    def track_unmatched(self, track_display: str, tried: int) -> None:
        self._buffer_track(f"❌ {html.escape(track_display)} ({tried} methods)")

    def _buffer_track(self, line: str) -> None:
        # Per-track lines are held with everything else and sent in the single
        # end-of-run message; the cap keeps that message a sane length.
        if len(self._track_buffer) >= self.max_track_lines:
            if len(self._track_buffer) == self.max_track_lines:
                self._track_buffer.append(
                    f"… and more (only the first {self.max_track_lines} are listed)"
                )
            return
        self._track_buffer.append(line)

    def flush_tracks(self) -> None:
        """Fold the buffered per-track lines into the one-message queue."""
        if not self._track_buffer:
            return
        if not self.one_message:
            header = f"🎧 <b>{len(self._track_buffer)} track updates</b>\n"
            self.send(header + "\n".join(self._track_buffer))
            self._track_buffer = []
            return
        self.lines.extend(self._track_buffer)
        self._track_buffer = []

    def flush(self, summary: str = "") -> int:
        """Send everything collected during the run as ONE message."""
        self.flush_tracks()
        body = [line for line in self.lines if line]
        self.lines = []
        if not self.enabled:
            return 0
        # The summary always comes last so it is the visible "head" of the chat.
        parts = body + ([summary] if summary else [])
        # One compact message: the summary already lists every track, so no
        # blank line between lines (it doubles the length for no benefit).
        text = "\n".join(parts)
        if not text.strip():
            return 0
        sent = 0
        for chunk in self._chunks(text):
            self.send(chunk)
            sent += 1
        self.sent_messages += sent
        return sent


def dedupe_lines(lines: list[str], key=len) -> list[str]:
    """Drop repeated lines while keeping the original order."""
    seen: set = set()
    out: list[str] = []
    for line in lines:
        marker = key(line)
        if marker in seen:
            continue
        seen.add(marker)
        out.append(line)
    return out


def escape(text: str) -> str:
    return html.escape(text or "")
