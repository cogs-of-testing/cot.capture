from __future__ import annotations

import logging
import threading
from collections.abc import Iterator

import pytest

from cot.capture import (
    DiscardPolicy,
    LevelChangedWarning,
    SlotReplacedWarning,
    capture_logs,
)
from cot.capture import _logs


@pytest.fixture(autouse=True)
def no_router_left() -> Iterator[None]:
    yield
    assert _logs._router is None


def messages(records: list[logging.LogRecord]) -> list[str]:
    return [r.getMessage() for r in records]


def test_records_what_is_logged_and_removes_the_router() -> None:
    log = logging.getLogger("t.plain")
    with capture_logs() as scope:
        log.warning("one %s", 1)
        assert messages(scope.records) == ["one 1"]
    assert messages(scope.records) == ["one 1"]
    assert not any(isinstance(h, _logs._Router) for h in logging.getLogger().handlers)
    log.warning("after")
    assert messages(scope.records) == ["one 1"]


def test_levels_are_not_lowered_unless_asked() -> None:
    log = logging.getLogger("t.levels")
    with capture_logs() as scope:
        log.debug("hidden")
        log.warning("seen")
    assert messages(scope.records) == ["seen"]
    with capture_logs(levels={"t.levels": "DEBUG"}) as scope:
        assert log.level == logging.DEBUG
        log.debug("asked")
    assert messages(scope.records) == ["asked"]
    assert log.level == logging.NOTSET


def test_a_level_changed_under_the_scope_is_reported_and_left() -> None:
    log = logging.getLogger("t.changed")
    with pytest.warns(LevelChangedWarning, match="t.changed"):
        with capture_logs(levels={"t.changed": logging.DEBUG}) as scope:
            log.setLevel(logging.ERROR)
    assert log.level == logging.ERROR
    assert scope.diagnostics[0].slot == "logging.getLogger('t.changed').level"
    log.setLevel(logging.NOTSET)


def test_unknown_level_is_refused() -> None:
    with pytest.raises(ValueError, match="nope"):
        capture_logs(threshold="nope")


def test_threshold_filters_only_what_is_kept() -> None:
    log = logging.getLogger("t.threshold")
    log.setLevel(logging.DEBUG)
    try:
        with capture_logs(threshold="WARNING") as scope:
            log.info("dropped")
            log.error("kept")
        assert messages(scope.records) == ["kept"]
        assert log.level == logging.DEBUG
    finally:
        log.setLevel(logging.NOTSET)


def test_non_propagating_logger_is_captured_once() -> None:
    log = logging.getLogger("t.np")
    child = logging.getLogger("t.np.child")
    log.propagate = False
    try:
        with capture_logs() as scope:
            log.warning("parent")
            child.warning("child")
        assert messages(scope.records) == ["parent", "child"]
        assert log.handlers == []
    finally:
        log.propagate = True


def test_no_duplicate_when_propagation_flips_mid_scope() -> None:
    # pytest #15064: the stand-in must not deliver what the root now delivers
    log = logging.getLogger("t.flip")
    log.propagate = False
    try:
        with capture_logs() as scope:
            log.warning("before")
            log.propagate = True
            log.warning("after")
        assert messages(scope.records) == ["before", "after"]
    finally:
        log.propagate = True


def test_nested_non_propagating_loggers_deliver_once() -> None:
    outer = logging.getLogger("t.nest")
    inner = logging.getLogger("t.nest.inner")
    outer.propagate = inner.propagate = False
    try:
        with capture_logs() as scope:
            inner.warning("a")
            inner.propagate = True
            inner.warning("b")
            outer.propagate = True
            inner.warning("c")
        assert messages(scope.records) == ["a", "b", "c"]
    finally:
        outer.propagate = inner.propagate = True


def test_logger_turned_non_propagating_mid_scope_is_missed() -> None:
    # the documented gap shared with pytest (L2)
    log = logging.getLogger("t.late")
    try:
        with capture_logs() as scope:
            log.propagate = False
            log.warning("missed")
        assert scope.records == []
    finally:
        log.propagate = True


def test_nested_scopes_stack_and_share_one_router() -> None:
    log = logging.getLogger("t.stack")
    with capture_logs(name="outer") as outer:
        log.warning("o1")
        router = _logs._router
        with capture_logs(name="inner") as inner:
            assert _logs._router is router
            log.warning("i")
        log.warning("o2")
    assert messages(outer.records) == ["o1", "o2"]
    assert messages(inner.records) == ["i"]


def test_nested_scope_adds_and_removes_only_its_stand_ins() -> None:
    a = logging.getLogger("t.sa")
    b = logging.getLogger("t.sb")
    a.propagate = False
    try:
        with capture_logs() as outer:
            b.propagate = False
            with capture_logs() as inner:
                a.warning("a")
                b.warning("b")
            assert len(a.handlers) == 1
            assert b.handlers == []
            a.warning("a2")
        assert messages(inner.records) == ["a", "b"]
        assert messages(outer.records) == ["a2"]
        assert a.handlers == []
    finally:
        a.propagate = b.propagate = True


def test_take_splits_the_scope() -> None:
    log = logging.getLogger("t.take")
    with capture_logs() as scope:
        log.warning("setup")
        assert messages(scope.take()) == ["setup"]
        log.warning("call")
        assert messages(scope.records) == ["call"]
    assert messages(scope.records) == ["call"]


def test_discard_policy_sees_batches_and_facts() -> None:
    seen: list[tuple[list[str], dict[str, object]]] = []

    def keep(batch: object, facts: object) -> bool:
        assert isinstance(facts, dict)
        seen.append((messages(list(batch)), facts))  # type: ignore[call-overload]
        return bool(facts.get("failed"))

    log = logging.getLogger("t.discard")
    with capture_logs(discard=DiscardPolicy(keep=keep)) as scope:
        log.warning("passing")
        assert scope.take(failed=False) == []
        log.warning("failing")
        assert messages(scope.take(failed=True)) == ["failing"]
        log.warning("end")
    assert scope.records == []
    assert seen == [(["passing"], {"failed": False}), (["failing"], {"failed": True}), (["end"], {})]


def test_cap_drops_the_oldest_and_counts() -> None:
    log = logging.getLogger("t.cap")
    with capture_logs(discard=DiscardPolicy(max_records=2)) as scope:
        for i in range(5):
            log.warning("%d", i)
    assert messages(scope.records) == ["3", "4"]
    assert scope.dropped == 3


def test_records_from_other_threads_go_to_the_current_scope() -> None:
    log = logging.getLogger("t.thread")
    with capture_logs() as scope:
        t = threading.Thread(target=log.warning, args=("from thread",), name="worker")
        t.start()
        t.join()
    [record] = scope.records
    assert record.threadName == "worker"


def test_router_removed_by_someone_else_is_reported() -> None:
    log = logging.getLogger("t.reset")
    with pytest.warns(SlotReplacedWarning, match="router gone"):
        with capture_logs() as scope:
            root = logging.getLogger()
            root.removeHandler(_logs._router)  # type: ignore[arg-type]
            log.warning("lost")
    assert scope.records == []
    assert scope.diagnostics[0].slot == "logging.getLogger().handlers"


def test_router_filters_apply_once_through_a_stand_in() -> None:
    log = logging.getLogger("t.filter")
    log.propagate = False
    calls: list[str] = []

    def count(record: logging.LogRecord) -> bool:
        calls.append(record.getMessage())
        return True

    try:
        with capture_logs() as scope:
            assert _logs._router is not None
            _logs._router.addFilter(count)
            log.warning("np")
            logging.getLogger("t.other").warning("root")
        assert calls == ["np", "root"]
        assert messages(scope.records) == ["np", "root"]
    finally:
        log.propagate = True


def test_host_handlers_get_every_record_with_their_own_level() -> None:
    seen: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            seen.append(record.getMessage())

    handler = Collect(logging.INFO)
    log = logging.getLogger("t.handlers")
    log.propagate = False
    log.setLevel(logging.DEBUG)
    try:
        with capture_logs(handlers=[handler], threshold="ERROR") as scope:
            log.debug("debug")
            log.info("info")
            log.error("error")
        assert seen == ["info", "error"]
        assert messages(scope.records) == ["error"]
    finally:
        log.propagate = True
        log.setLevel(logging.NOTSET)
