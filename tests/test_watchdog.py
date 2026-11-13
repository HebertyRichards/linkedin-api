import asyncio
import threading

import pytest

from app.main import stall_after
from app.utils.watchdog import Watchdog
from tests.helpers import FakeNotifier, FakeSource, make_watcher


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_stalled_only_after_the_limit_without_a_beat() -> None:
    clock = Clock()
    dog = Watchdog(100, clock=clock)

    clock.now = 100
    assert not dog.stalled()
    clock.now = 101
    assert dog.stalled()
    dog.beat()
    assert not dog.stalled()


def test_a_stalled_loop_exits_the_process_with_an_error(caplog: pytest.LogCaptureFixture) -> None:
    clock = Clock()
    codes: list[int] = []
    exited = threading.Event()

    def exit_process(code: int) -> None:
        codes.append(code)
        exited.set()

    dog = Watchdog(10, check_every=0.01, exit_process=exit_process, clock=clock)
    clock.now = 11
    dog.start()

    assert exited.wait(2)
    assert codes == [1]  # non-zero: Railway's On Failure policy restarts it
    assert "HEALTH_STALLED" in caplog.text


def test_a_beating_loop_is_left_alone() -> None:
    codes: list[int] = []
    dog = Watchdog(10, check_every=0.01, exit_process=codes.append, clock=Clock())
    dog.start()
    threading.Event().wait(0.1)
    dog.stop()

    assert codes == []


async def test_the_loop_beats_before_each_step_and_before_sleeping() -> None:
    stop = asyncio.Event()
    beats: list[int] = []

    def heartbeat() -> None:
        beats.append(1)
        if len(beats) == 3:
            stop.set()

    watcher = make_watcher(FakeSource([[]]), [FakeNotifier("email")])
    watcher._heartbeat = heartbeat

    await asyncio.wait_for(watcher.run(stop), timeout=5)

    assert len(beats) == 3  # digest, cycle, sleep


def test_stall_limit_covers_a_long_cycle_and_a_long_sleep() -> None:
    assert stall_after(1800) == 7200  # 30 min poll: restart after 2 h without progress
    assert stall_after(3 * 3600) == 3 * 3600 * 1.2 + 3600
