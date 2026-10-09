"""Recording warnings, unraisable and thread exceptions (design warnings.md)."""

from __future__ import annotations

import gc
import sys
import threading
import warnings
from collections.abc import Iterator

import pytest

from cot.capture import (
    GCPolicy,
    SlotReplacedWarning,
    ThreadExceptionRecord,
    UnraisableRecord,
    WarningRecord,
    record,
)


def messages(records: list[object]) -> list[str]:
    return [str(r.message.message) for r in records if isinstance(r, WarningRecord)]


@pytest.mark.filterwarnings("always")
def test_records_what_the_filters_let_through_and_restores_the_slot() -> None:
    before = warnings.showwarning
    with record(name="r") as rec:
        warnings.warn("hello", UserWarning)
        warnings.warn("old", DeprecationWarning)
    assert warnings.showwarning is before
    [first, second] = rec.records
    assert isinstance(first, WarningRecord)
    assert first.message.category is UserWarning
    assert first.message.filename == __file__
    assert first.thread == threading.current_thread().name
    assert messages(rec.records) == ["hello", "old"]


def test_filters_apply_for_the_lifetime_only() -> None:
    filters_before = list(warnings.filters)
    with record(filters=["always", "ignore::DeprecationWarning"]) as rec:
        warnings.warn("kept", UserWarning)
        warnings.warn("dropped", DeprecationWarning)
    assert messages(rec.records) == ["kept"]
    assert warnings.filters == filters_before


def test_filter_fields_are_literal_as_with_dash_w() -> None:
    with record(filters=["always", "error:a.b"]) as rec:
        warnings.warn("axb")
        with pytest.raises(UserWarning, match="a.b"):
            warnings.warn("a.b")
    assert messages(rec.records) == ["axb"]


@pytest.mark.parametrize(
    "spec", ["bogus", "always::NoSuchWarning", "always::ValueError", "always::::x", "a:b:c:d:e:f"]
)
def test_invalid_filters_are_refused(spec: str) -> None:
    with pytest.raises(ValueError, match="filter"):
        record(filters=[spec])


def test_a_recorder_never_restores_a_foreign_hook() -> None:
    seen: list[type[Warning]] = []

    def foreign(message: object, category: type[Warning], *args: object) -> None:
        seen.append(category)

    before = warnings.showwarning
    try:
        with record(filters=["always"], name="r") as rec:
            warnings.showwarning = foreign
        assert warnings.showwarning is foreign
        [diag] = rec.diagnostics
        assert diag.slot == "warnings.showwarning"
        assert "owned by 'r'" in diag.proxy
        # the report goes where warnings go now: to the foreign hook
        assert seen == [SlotReplacedWarning]
    finally:
        warnings.showwarning = before


def test_a_recorder_kept_past_its_recording_warns_and_passes_on() -> None:
    passed: list[str] = []

    def current(message: object, *args: object) -> None:
        passed.append(str(message))

    with record(warnings=True, unraisable=False, thread_exceptions=False, name="old"):
        kept = warnings.showwarning
    with warnings.catch_warnings():
        warnings.simplefilter("always")
        warnings.showwarning = current
        kept("late", UserWarning, __file__, 1)
    [expired, late] = passed
    assert "closed recorder for warnings.showwarning owned by 'old'" in expired
    assert late == "late"


@pytest.mark.filterwarnings("always")
def test_nested_recordings_stack() -> None:
    with record() as outer:
        warnings.warn("one")
        with record() as inner:
            warnings.warn("two")
        warnings.warn("three")
    assert messages(inner.records) == ["two"]
    assert messages(outer.records) == ["one", "three"]


@pytest.mark.filterwarnings("always")
def test_take_splits_a_recording() -> None:
    with record() as rec:
        warnings.warn("one")
        first = rec.take()
        warnings.warn("two")
    assert messages(first) == ["one"]
    assert messages(rec.records) == ["two"]


@pytest.mark.filterwarnings("always")
def test_warnings_from_other_threads_carry_their_thread() -> None:
    with record() as rec:
        t = threading.Thread(target=lambda: warnings.warn("there"), name="worker")
        t.start()
        t.join()
    [r] = rec.records
    assert isinstance(r, WarningRecord)
    assert r.thread == "worker"


class Noisy:
    def __init__(self) -> None:
        self.armed = True
        self.cycle: object = self

    def __repr__(self) -> str:
        return "<Noisy>"

    def __del__(self) -> None:
        if self.armed:
            raise RuntimeError("from __del__")


def test_unraisable_exceptions_are_recorded_as_their_own_kind() -> None:
    with record() as rec:
        obj = Noisy()
        obj.cycle = None
        del obj
    [r] = rec.records
    assert isinstance(r, UnraisableRecord)
    assert r.exc_type is RuntimeError
    # 3.13 names the deallocator in err_msg and passes no object
    assert "Noisy.__del__" in r.err_msg + r.object_repr
    assert "RuntimeError: from __del__" in r.traceback
    assert sys.unraisablehook is not None


def test_thread_exceptions_are_recorded() -> None:
    def fail() -> None:
        raise KeyError("boom")

    with record() as rec:
        t = threading.Thread(target=fail, name="failing")
        t.start()
        t.join()
    [r] = rec.records
    assert isinstance(r, ThreadExceptionRecord)
    assert r.thread == "failing"
    assert r.exc_type is KeyError
    assert "KeyError: 'boom'" in r.traceback


@pytest.fixture
def no_automatic_gc() -> Iterator[None]:
    gc.collect()
    gc.disable()
    try:
        yield
    finally:
        gc.enable()


@pytest.mark.usefixtures("no_automatic_gc")
def test_gc_policy_collects_before_the_recording_ends() -> None:
    with record(gc=GCPolicy(passes=1)) as rec:
        Noisy()
    assert [type(r) for r in rec.records] == [UnraisableRecord]


@pytest.mark.usefixtures("no_automatic_gc")
def test_gc_policy_collects_on_take() -> None:
    with record(gc=GCPolicy(passes=1, on_end=False)) as rec:
        Noisy()
        taken = rec.take()
    assert [type(r) for r in taken] == [UnraisableRecord]


@pytest.mark.usefixtures("no_automatic_gc")
def test_without_collection_cycles_are_not_reported_here() -> None:
    with record() as rec:
        garbage = Noisy()
        survivor = garbage
        del garbage
    assert rec.records == []
    survivor.armed = False
    survivor.cycle = None
