import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.errors import ChannelUnavailableError, SourceUnavailableError
from app.schemas.job import JobDetails
from app.services.messages import AI_DOWN, RESUME_DOWN, job_message
from app.services.watcher import excluded_terms, is_excluded
from app.state.seen_store import SeenStore
from tests.helpers import (
    Clock,
    FakeAssessor,
    FakeNotifier,
    FakeSource,
    FakeTailor,
    assessment,
    job,
    make_watcher,
)


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


async def test_unexpected_error_in_the_primary_still_reaches_the_fallback() -> None:
    whatsapp = FakeNotifier("whatsapp", fail_with=AttributeError("'list' object has no attribute 'get'"))
    email = FakeNotifier("email")
    watcher = make_watcher(FakeSource([[], [job(1)]]), [whatsapp, email])

    await watcher.cycle()
    await watcher.cycle()

    assert [message.keys for message in email.sent] == [(job(1).key,)]


async def test_each_cycle_logs_a_summary(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    watcher = make_watcher(FakeSource([[], [job(1)]]), [FakeNotifier("email")])

    await watcher.cycle()
    await watcher.cycle()

    assert "Cycle done: 1 of 1 search(es) answered, 1 new job(s)" in caplog.text
    assert "Cycle done: 1 of 1 search(es) answered, 0 new job(s)" not in caplog.text


async def test_a_block_stops_the_read_with_one_warning(caplog: pytest.LogCaptureFixture) -> None:
    source = FakeSource([SourceUnavailableError("blocked (429)", blocked=True)])
    watcher = make_watcher(source, [FakeNotifier("email")], searches=["python", "node", "react"])

    await watcher.cycle()

    assert source.calls == 1
    assert caplog.text.count("failed: blocked (429)") == 1


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

    for _ in range(12):
        await watcher.cycle()
        clock.advance(minutes=5)
    assert email.sent == []  # 55 minutes: a usual 429 clears before this

    for _ in range(3):
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


async def test_digest_runs_at_boot_with_up_to_10_unseen_jobs_from_the_last_3_days() -> None:
    email = FakeNotifier("email")
    source = FakeSource([[job(n) for n in range(12)]])
    store = SeenStore(None)
    store.mark([job(0).key], datetime(2026, 10, 25, tzinfo=UTC))
    watcher = make_watcher(source, [email], store=store)

    await watcher.digest_if_due()

    assert source.windows == [3 * 86400]
    assert [message.keys for message in email.sent] == [tuple(job(n).key for n in range(1, 11))]
    assert store.is_seen(job(10).key)
    assert not store.is_seen(job(11).key)


async def test_digest_waits_four_hours_and_sends_only_what_is_still_unseen() -> None:
    email = FakeNotifier("email")
    clock = Clock()
    watcher = make_watcher(FakeSource([[job(1)], [job(1), job(2)]]), [email], clock=clock)

    await watcher.digest_if_due()
    clock.advance(minutes=239)
    await watcher.digest_if_due()
    assert len(email.sent) == 1

    clock.advance(minutes=1)
    await watcher.digest_if_due()
    assert [message.keys for message in email.sent] == [(job(1).key,), (job(2).key,)]


async def test_digest_stays_due_when_delivery_fails() -> None:
    email = FakeNotifier("email", fail_with=ChannelUnavailableError("circuit open"))
    watcher = make_watcher(FakeSource([[job(1)]]), [email])

    await watcher.digest_if_due()
    email.fail_with = None
    await watcher.digest_if_due()

    assert [message.keys for message in email.sent] == [(job(1).key,)]


async def test_cycle_reads_the_last_hour() -> None:
    source = FakeSource([[]])
    await make_watcher(source, [FakeNotifier("email")]).cycle()
    assert source.windows == [3600]


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


async def test_scored_jobs_go_out_best_first_and_on_site_ones_never() -> None:
    email = FakeNotifier("email")
    store = SeenStore(None)
    scores = {job(1).key: assessment(55), job(2).key: assessment(90, "onsite"), job(3).key: assessment(82)}
    # job(4) has no score (LLM down or quota used up): it still goes out, last.
    watcher = make_watcher(
        FakeSource([[], [job(n) for n in range(1, 5)]]), [email], store=store, assessor=FakeAssessor(scores)
    )

    await watcher.cycle()
    await watcher.cycle()

    # Up to 3 jobs go out one message each, the best score first.
    assert [message.keys for message in email.sent] == [(job(3).key,), (job(1).key,), (job(4).key,)]
    assert email.sent[0].subject.startswith("Nova vaga Easy Apply (82/100)")
    assert "Nota 82/100: Stack parecida." in email.sent[0].text
    assert "Falta: Kubernetes" in email.sent[0].text
    assert store.is_seen(job(2).key)


async def test_job_page_down_still_sends_the_job_without_a_score() -> None:
    email = FakeNotifier("email")
    source = FakeSource([[], [job(1)]], details=SourceUnavailableError("status 500"))
    watcher = make_watcher(source, [email], assessor=FakeAssessor({job(1).key: assessment(80)}))

    await watcher.cycle()
    await watcher.cycle()

    assert [message.keys for message in email.sent] == [(job(1).key,)]
    assert "Nota" not in email.sent[0].text
    assert AI_DOWN in email.sent[0].text and AI_DOWN in email.sent[0].html


async def test_a_job_already_scored_skips_its_job_page() -> None:
    source = FakeSource([[], [job(1)]])
    watcher = make_watcher(
        source, [FakeNotifier("email")], assessor=FakeAssessor({}, cache={job(1).key: assessment(70)})
    )

    await watcher.cycle()
    await watcher.cycle()

    assert source.detail_calls == []


async def test_digest_scores_its_jobs_too() -> None:
    email = FakeNotifier("email")
    watcher = make_watcher(
        FakeSource([[job(1), job(2)]]),
        [email],
        assessor=FakeAssessor({job(1).key: assessment(40), job(2).key: assessment(75)}),
    )

    await watcher.digest_if_due()

    assert [message.keys for message in email.sent] == [(job(2).key, job(1).key)]


def test_new_score_for_the_same_jobs_is_a_new_message_key() -> None:
    first = job_message([job(1)], {job(1).key: assessment(60)})
    rescored = job_message([job(1)], {job(1).key: assessment(70)})
    assert first.idempotency_key != rescored.idempotency_key
    assert first.idempotency_key == job_message([job(1)], {job(1).key: assessment(60)}).idempotency_key


async def test_blocked_job_page_holds_the_jobs_once_then_sends_them_without_a_score() -> None:
    # Seen on Railway: LinkedIn answers 429 on the 2nd job page and the circuit opens for the rest.
    email = FakeNotifier("email")
    store = SeenStore(None)
    blocked = SourceUnavailableError("blocked (429)", blocked=True)
    source = FakeSource([[], [job(1), job(2)]], details=blocked)
    watcher = make_watcher(source, [email], store=store, assessor=FakeAssessor({job(1).key: assessment(90)}))

    await watcher.cycle()
    await watcher.cycle()
    assert email.sent == []  # held: the next run scores them
    assert not store.is_seen(job(1).key)
    assert source.detail_calls == [job(1).key]  # a block stops the job pages for this batch

    await watcher.cycle()
    assert [message.keys for message in email.sent] == [(job(1).key,), (job(2).key,)]
    assert "Nota" not in email.sent[0].text


async def test_digest_held_by_a_block_runs_again_on_the_next_loop() -> None:
    email = FakeNotifier("email")
    blocked = SourceUnavailableError("blocked (429)", blocked=True)
    watcher = make_watcher(FakeSource([[job(1)]], details=blocked), [email], assessor=FakeAssessor({}))

    await watcher.digest_if_due()
    assert email.sent == []
    await watcher.digest_if_due()  # same clock: still due because it held jobs
    assert [message.keys for message in email.sent] == [(job(1).key,)]


async def test_only_a_score_from_50_up_gets_the_tailored_resume_attached() -> None:
    email = FakeNotifier("email")
    tailor = FakeTailor()
    scores = {job(1).key: assessment(50), job(2).key: assessment(49), job(3).key: assessment(95, "onsite")}
    watcher = make_watcher(
        FakeSource([[], [job(n) for n in range(1, 4)]]), [email], assessor=FakeAssessor(scores), tailor=tailor
    )

    await watcher.cycle()
    await watcher.cycle()

    assert tailor.calls == [job(1).key]
    best, other = email.sent
    assert [a.filename for a in best.attachments] == ["Curriculo-4400000001.pdf"]
    assert "Currículo ajustado em anexo: Curriculo-4400000001.pdf" in best.text
    assert "Resumo: Resumo ajustado." in best.text and "Currículo ajustado" in best.html
    assert other.attachments == ()
    assert "Currículo ajustado" not in other.text and AI_DOWN not in other.text


async def test_llm_down_says_so_in_the_email_and_the_job_still_goes() -> None:
    email = FakeNotifier("email")
    # job(1) scored but its resume failed; job(2) got no score at all.
    watcher = make_watcher(
        FakeSource([[], [job(1), job(2)]]),
        [email],
        assessor=FakeAssessor({job(1).key: assessment(70)}),
        tailor=FakeTailor(down=True),
    )

    await watcher.cycle()
    await watcher.cycle()

    scored, unscored = email.sent
    assert RESUME_DOWN in scored.text and scored.attachments == ()
    assert AI_DOWN in unscored.text and RESUME_DOWN not in unscored.text


async def test_without_ai_configured_there_is_no_down_notice() -> None:
    email = FakeNotifier("email")
    watcher = make_watcher(FakeSource([[], [job(1)]]), [email])

    await watcher.cycle()
    await watcher.cycle()

    assert AI_DOWN not in email.sent[0].text


def test_not_terms_are_read_with_quotes_and_without_accents() -> None:
    search = '("desenvolvedor react" OR fullstack) NOT sênior NOT "menor aprendiz" NOT consultor'
    assert excluded_terms(search) == ["senior", "menor aprendiz", "consultor"]
    assert excluded_terms("python developer") == []


@pytest.mark.parametrize(
    ("title", "dropped"),
    [
        ("Consultores e Menores aprendizes de Plataformas Digitais", True),  # plural of "consultor"
        ("Desenvolvedor Full Stack Sênior", True),
        ("Desenvolvedor Full Stack Pleno", False),
        ("Engenheiro de Software - Seniority Flexível", True),  # a word starting with the term
        ("Desenvolvedora Python (Consultoria interna)", True),
        ("Desenvolvedor Python para time de consultas", False),
    ],
)
def test_a_title_with_a_not_term_is_dropped(title: str, dropped: bool) -> None:
    assert is_excluded(title, ["senior", "consultor"]) is dropped


async def test_jobs_with_a_not_term_in_the_title_never_go_out() -> None:
    email = FakeNotifier("email")
    noise = job(2, "Consultores e Menores aprendizes de Plataformas e Soluções Digitais")
    source = FakeSource([[job(1)], [job(1), noise, job(3)]])
    watcher = make_watcher(source, [email], searches=["fullstack NOT consultor"])

    await watcher.cycle()
    await watcher.cycle()

    assert [message.keys for message in email.sent] == [(job(3).key,)]


async def test_a_closed_job_is_dropped_before_the_ai_and_never_goes_out() -> None:
    email = FakeNotifier("email")
    closed = JobDetails(description="Vaga de Python.", closed=True)
    source = FakeSource([[], [job(1)], [job(1)]], details=closed)
    store = SeenStore(None)
    watcher = make_watcher(source, [email], store=store, assessor=FakeAssessor({job(1).key: assessment(90)}))

    await watcher.cycle()
    await watcher.cycle()
    await watcher.cycle()

    assert email.sent == []
    assert store.is_seen(job(1).key)
    assert source.detail_calls == [job(1).key]  # marked as seen: its page is not read again
