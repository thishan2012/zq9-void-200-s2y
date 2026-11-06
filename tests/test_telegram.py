"""Telegram output policy: one message per run, no repeats."""
from phonksync.engine import RunReport
from phonksync.telegram import TelegramLogger


def make_logger(**kw):
    tg = TelegramLogger(bot_token="t", chat_id="c", **kw)
    sent = []
    tg.send = lambda text, disable_preview=True: (sent.append(text), 1)[1]
    return tg, sent


def test_a_whole_run_becomes_exactly_one_message():
    tg, sent = make_logger()
    tg.info("🎧 started")
    tg.warn("🌐 read from the web player")
    for i in range(120):
        tg._buffer_track(f"✅ Artist {i} — SONG {i} <code>[CHANNEL]</code>")
    tg.error("something failed")
    report = RunReport(added=120, unique_tracks=120, sources_read=2)
    report.added_tracks = [f"Artist {i} — SONG {i}" for i in range(3)]
    tg.flush(summary=report.summary_text())

    assert len(sent) == 1, f"expected 1 message, got {len(sent)}"
    body = sent[0]
    assert "🎧 started" in body
    assert "Sync run finished" in body
    assert "something failed" in body


def test_long_runs_stay_one_message_by_capping_track_lines():
    tg, sent = make_logger()
    for i in range(500):
        tg._buffer_track(f"✅ track {i}")
    tg.flush(summary="summary")
    assert len(sent) == 1
    assert sent[0].count("✅ track ") == tg.max_track_lines
    assert f"only the first {tg.max_track_lines} are listed" in sent[0]


def test_chunks_only_when_telegram_would_reject_the_length():
    tg, sent = make_logger()
    one_line = "x" * 3000
    tg.info(one_line)
    tg.info(one_line)
    tg.flush(summary="summary")
    assert len(sent) == 2  # 6000 chars cannot be one Telegram message
    assert "summary" in sent[-1]


def test_disabled_logger_sends_nothing_but_keeps_lines_out_of_memory():
    tg = TelegramLogger(bot_token="", chat_id="", one_message=True)
    tg.info("secret-ish log line")
    assert tg.flush(summary="done") == 0
    assert tg.lines == []


def test_summary_lists_added_tracks_once_and_never_the_backlog():
    report = RunReport(added=2, already_synced=230, unique_tracks=232)
    report.added_tracks = [
        "Alpha — ONE <code>[CHANNEL]</code>",
        "Beta — TWO <code>[REVERSED]</code>",
    ]
    text = report.summary_text()
    # counts only for what went in — the playlist is the receipt
    assert "Added: <b>2</b>" in text
    assert "Alpha — ONE" not in text
    assert "Beta — TWO" not in text
    assert "230 were already synced" in text
    assert "Would add" not in text
