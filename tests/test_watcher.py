import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.errors import ChannelUnavailableError, SourceUnavailableError
from app.schemas.job import JobDetails
from app.services.messages import RESUME_DOWN, job_message
from app.services.watcher import excluded_terms, is_excluded
from app.state.seen_store import SeenStore
from tests.helpers import (
    DETAILS,
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


async def test_digest_skips_the_boot_then_scores_up_to_10_unseen_jobs_of_any_date() -> None:
    email = FakeNotifier("email")
    clock = Clock()
    source = FakeSource([[job(n) for n in range(12)]])
    store = SeenStore(None)
    store.mark([job(0).key], datetime(2026, 10, 25, tzinfo=UTC))
    watcher = make_watcher(source, [email], store=store, clock=clock)

    await watcher.digest_if_due()  # just deployed: nothing runs
    assert source.calls == 0

    clock.advance(minutes=360)
    await watcher.digest_if_due()

    assert source.windows == [None]
    assert [message.keys for message in email.sent] == [tuple(job(n).key for n in range(1, 11))]
    assert store.is_seen(job(10).key)
    assert not store.is_seen(job(11).key)


async def test_digest_runs_every_six_hours_and_sends_only_what_is_still_unseen() -> None:
    email = FakeNotifier("email")
    clock = Clock()
    watcher = make_watcher(FakeSource([[job(1)], [job(1), job(2)]]), [email], clock=clock)

    clock.advance(minutes=360)
    await watcher.digest_if_due()
    clock.advance(minutes=359)
    await watcher.digest_if_due()
    assert len(email.sent) == 1

    clock.advance(minutes=1)
    await watcher.digest_if_due()
    assert [message.keys for message in email.sent] == [(job(1).key,), (job(2).key,)]


async def test_digest_stays_due_when_delivery_fails() -> None:
    email = FakeNotifier("email", fail_with=ChannelUnavailableError("circuit open"))
    clock = Clock()
    watcher = make_watcher(FakeSource([[job(1)]]), [email], clock=clock)

    clock.advance(minutes=360)
    await watcher.digest_if_due()
    email.fail_with = None
    await watcher.digest_if_due()

    assert [message.keys for message in email.sent] == [(job(1).key,)]


async def test_digest_with_nothing_from_50_up_sends_no_message() -> None:
    email = FakeNotifier("email")
    clock = Clock()
    store = SeenStore(None)
    source = FakeSource([[job(1), job(2)]])
    scores = {job(1).key: assessment(49), job(2).key: assessment(30)}
    watcher = make_watcher(source, [email], store=store, clock=clock, assessor=FakeAssessor(scores))

    clock.advance(minutes=360)
    await watcher.digest_if_due()
    clock.advance(minutes=360)
    await watcher.digest_if_due()

    assert email.sent == []
    assert store.is_seen(job(1).key) and store.is_seen(job(2).key)
    assert source.detail_calls == [job(1).key, job(2).key]  # scored once: marked, never scored again


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


def test_prune_drops_keys_older_than_a_year() -> None:
    now = datetime(2026, 10, 25, tzinfo=UTC)
    store = SeenStore(None)
    store.mark([job(1).key], now - timedelta(days=366))
    store.mark([job(2).key], now - timedelta(days=364))
    assert store.prune(now)
    assert not store.is_seen(job(1).key)
    assert store.is_seen(job(2).key)


def test_message_is_stable_escaped_and_links_by_id() -> None:
    risky = job(1, title="<script>alert(1)</script>")
    first, again = job_message([risky]), job_message([risky])
    assert (first.text, first.idempotency_key) == (again.text, again.idempotency_key)
    assert "<script>" not in first.html
    assert risky.url in first.text


async def test_only_jobs_from_50_up_go_out_best_first_and_on_site_ones_never() -> None:
    email = FakeNotifier("email")
    store = SeenStore(None)
    scores = {
        job(1).key: assessment(55),
        job(2).key: assessment(90, "onsite"),
        job(3).key: assessment(82),
        job(4).key: assessment(49),
    }
    # job(5) has no score (LLM down or quota used up): it waits, neither sent nor marked.
    watcher = make_watcher(
        FakeSource([[], [job(n) for n in range(1, 6)]]), [email], store=store, assessor=FakeAssessor(scores)
    )

    await watcher.cycle()
    await watcher.cycle()

    # Up to 3 jobs go out one message each, the best score first.
    assert [message.keys for message in email.sent] == [(job(3).key,), (job(1).key,)]
    assert email.sent[0].subject.startswith("Nova vaga Easy Apply (82/100)")
    assert "Nota 82/100: Stack parecida." in email.sent[0].text
    assert "Falta: Kubernetes" in email.sent[0].text
    assert store.is_seen(job(2).key) and store.is_seen(job(4).key)
    assert not store.is_seen(job(5).key)


async def test_job_page_down_holds_the_job_until_it_can_be_scored() -> None:
    email = FakeNotifier("email")
    store = SeenStore(None)
    source = FakeSource([[], [job(1)]], details=SourceUnavailableError("status 500"))
    watcher = make_watcher(source, [email], store=store)

    await watcher.cycle()
    await watcher.cycle()
    assert email.sent == []
    assert not store.is_seen(job(1).key)

    source.details = DETAILS
    await watcher.cycle()
    assert [message.keys for message in email.sent] == [(job(1).key,)]
    assert "Nota 80/100" in email.sent[0].text


async def test_a_job_page_that_keeps_failing_is_given_up() -> None:
    store = SeenStore(None)
    source = FakeSource([[], [job(1)]], details=SourceUnavailableError("status 500"))
    watcher = make_watcher(source, [FakeNotifier("email")], store=store)

    for _ in range(5):
        await watcher.cycle()

    assert store.is_seen(job(1).key)
    assert source.detail_calls == [job(1).key] * 3


async def test_a_job_already_scored_skips_its_job_page() -> None:
    source = FakeSource([[], [job(1)]])
    watcher = make_watcher(
        source, [FakeNotifier("email")], assessor=FakeAssessor({}, cache={job(1).key: assessment(70)})
    )

    await watcher.cycle()
    await watcher.cycle()

    assert source.detail_calls == []


def test_new_score_for_the_same_jobs_is_a_new_message_key() -> None:
    first = job_message([job(1)], {job(1).key: assessment(60)})
    rescored = job_message([job(1)], {job(1).key: assessment(70)})
    assert first.idempotency_key != rescored.idempotency_key
    assert first.idempotency_key == job_message([job(1)], {job(1).key: assessment(60)}).idempotency_key


async def test_a_blocked_job_page_never_sends_the_job_without_a_score() -> None:
    # Seen on Railway: LinkedIn answers 429 on the 2nd job page and the circuit opens for the rest.
    email = FakeNotifier("email")
    store = SeenStore(None)
    blocked = SourceUnavailableError("blocked (429)", blocked=True)
    source = FakeSource([[], [job(1), job(2)]], details=blocked)
    watcher = make_watcher(source, [email], store=store, assessor=FakeAssessor({job(1).key: assessment(90)}))

    for _ in range(5):
        await watcher.cycle()

    assert email.sent == []  # a block is not a broken page: the job waits however long it takes
    assert not store.is_seen(job(1).key) and not store.is_seen(job(2).key)
    assert source.detail_calls == [job(1).key] * 4  # a block stops the job pages for the rest of the batch


async def test_every_job_sent_gets_its_tailored_resume() -> None:
    email = FakeNotifier("email")
    tailor = FakeTailor()
    scores = {job(1).key: assessment(50), job(2).key: assessment(49), job(3).key: assessment(95, "onsite")}
    watcher = make_watcher(
        FakeSource([[], [job(n) for n in range(1, 4)]]), [email], assessor=FakeAssessor(scores), tailor=tailor
    )

    await watcher.cycle()
    await watcher.cycle()

    assert tailor.calls == [job(1).key]
    (sent,) = email.sent
    assert [a.filename for a in sent.attachments] == ["Curriculo-4400000001.pdf"]
    assert "Currículo ajustado em anexo: Curriculo-4400000001.pdf" in sent.text
    assert "Resumo: Resumo ajustado." in sent.text and "Currículo ajustado" in sent.html


async def test_resume_down_says_so_and_the_job_still_goes() -> None:
    email = FakeNotifier("email")
    watcher = make_watcher(
        FakeSource([[], [job(1)]]),
        [email],
        assessor=FakeAssessor({job(1).key: assessment(70)}),
        tailor=FakeTailor(down=True),
    )

    await watcher.cycle()
    await watcher.cycle()

    (sent,) = email.sent
    assert RESUME_DOWN in sent.text and sent.attachments == ()


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
