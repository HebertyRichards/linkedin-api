from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.errors import ChannelUnavailableError, SourceUnavailableError
from app.services.messages import job_message
from app.state.seen_store import SeenStore
from tests.helpers import Clock, FakeNotifier, FakeSource, job, make_watcher


async def test_first_cycle_is_a_baseline_then_only_new_jobs_go_out() -> None:
    email = FakeNotifier("email")
    watcher = make_watcher(FakeSource([[job(1), job(2)], [job(1), job(2), job(3)], [job(1), job(2), job(3)]]), [email])

    await watcher.cycle()
    assert email.sent == []

    await watcher.cycle()
    await watcher.cycle()
    assert [message.keys for message in email.sent] == [(job(3).key,)]


async def test_fallback_channel_delivers_when_the_primary_fails() -> None:
    whatsapp = FakeNotifier("whatsapp", fail_with=ChannelUnavailableError("instance not connected"))
    email = FakeNotifier("email")
    watcher = make_watcher(FakeSource([[], [job(1)], [job(1)]]), [whatsapp, email])

    await watcher.cycle()
    await watcher.cycle()
    await watcher.cycle()

    assert [message.keys for message in email.sent] == [(job(1).key,)]


async def test_job_stays_pending_when_every_channel_fails(caplog: pytest.LogCaptureFixture) -> None:
    email = FakeNotifier("email", fail_with=ChannelUnavailableError("circuit open"))
    watcher = make_watcher(FakeSource([[], [job(1)]]), [email])

    await watcher.cycle()
    await watcher.cycle()
    assert "NOTIFY_FAILED" in caplog.text

    email.fail_with = None
    await watcher.cycle()
    assert [message.keys for message in email.sent] == [(job(1).key,)]


async def test_many_new_jobs_are_grouped() -> None:
    email = FakeNotifier("email")
    watcher = make_watcher(FakeSource([[], [job(n) for n in range(12)]]), [email])

    await watcher.cycle()
    await watcher.cycle()

    assert [len(message.keys) for message in email.sent] == [10, 2]


async def test_source_down_alert_goes_out_once() -> None:
    email = FakeNotifier("email")
    clock = Clock()
    watcher = make_watcher(FakeSource([SourceUnavailableError("blocked (429)", blocked=True)]), [email], clock=clock)

    for _ in range(4):
        await watcher.cycle()
        clock.advance(minutes=5)

    assert len(email.sent) == 1
    assert email.sent[0].keys == ()
    assert "12:00 UTC" in email.sent[0].text


async def test_state_file_survives_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "data" / "seen.json"
    email = FakeNotifier("email")
    first = SeenStore(path)
    watcher = make_watcher(FakeSource([[job(1)], [job(1), job(2)]]), [email], store=first)
    await watcher.cycle()
    await watcher.cycle()
    assert len(email.sent) == 1

    restarted = SeenStore(path)
    restarted.load()
    await make_watcher(FakeSource([[job(1), job(2)]]), [email], store=restarted).cycle()

    assert len(email.sent) == 1
    assert "Python Developer" not in path.read_text(encoding="utf-8")


def test_unreadable_state_file_starts_clean(tmp_path: Path) -> None:
    path = tmp_path / "seen.json"
    path.write_text("not json", encoding="utf-8")
    store = SeenStore(path)
    store.load()
    assert not store.is_seen(job(1).key)


def test_prune_drops_keys_older_than_30_days() -> None:
    now = datetime(2026, 10, 25, tzinfo=UTC)
    store = SeenStore(None)
    store.mark([job(1).key], now - timedelta(days=31))
    store.mark([job(2).key], now)
    assert store.prune(now)
    assert not store.is_seen(job(1).key)
    assert store.is_seen(job(2).key)


def test_message_is_stable_escaped_and_links_by_id() -> None:
    risky = job(1, title="<script>alert(1)</script>")
    first, again = job_message([risky]), job_message([risky])
    assert (first.text, first.idempotency_key) == (again.text, again.idempotency_key)
    assert "<script>" not in first.html
    assert risky.url in first.text
