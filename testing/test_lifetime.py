"""Proxies are closed at scope end and carry annotations (O1 to O3)."""

from __future__ import annotations

import contextlib
import errno
import io
import sys

import pytest

from cot.capture import (
    ProxyExpiredError,
    ProxyExpiredWarning,
    StreamProxy,
    capture,
)


def _misplace(**kwargs: object) -> tuple[StreamProxy, int]:
    """Run a scope and keep its stdout proxy, like an early-bound handler."""
    with capture(name="kept", **kwargs) as scope:  # type: ignore[arg-type]
        kept = sys.stdout
        line = sys._getframe().f_lineno - 2
    assert isinstance(kept, StreamProxy)
    assert scope.out == ""
    return kept, line


def test_proxy_is_closed_after_its_scope() -> None:
    kept, _ = _misplace()
    assert kept.closed


def test_annotations_name_owner_slot_and_locations() -> None:
    kept, line = _misplace()
    notes = kept.annotations
    assert (notes.slot, notes.owner) == ("stdout", "kept")
    assert notes.installed.filename == __file__
    assert notes.installed.lineno == line
    assert notes.closed is not None
    assert notes.closed.monotonic >= notes.installed.monotonic


def test_locations_skip_contextlib_to_the_code_that_entered() -> None:
    """A scope entered and left through an ``ExitStack`` names the stack's user."""
    start = sys._getframe().f_lineno + 1
    with contextlib.ExitStack() as stack:
        stack.enter_context(capture(name="stacked"))
        kept = sys.stdout
    assert isinstance(kept, StreamProxy)
    notes = kept.annotations
    assert (notes.installed.filename, notes.installed.lineno) == (__file__, start + 1)
    assert notes.closed is not None
    # the line Python reports for leaving a with block differs between versions
    assert notes.closed.filename == __file__
    assert notes.closed.lineno in range(start, start + 3)


def test_describe_names_file_and_line() -> None:
    kept, line = _misplace()
    assert kept.annotations.describe() == (
        f"proxy for sys.stdout owned by 'kept', installed at {__file__}:{line}, "
        f"closed at {__file__}:{line}"
    )


def test_a_host_labels_where_its_scope_lived() -> None:
    """A host entering scopes from its own machinery says where in its run."""
    kept, _ = _misplace(installed="before setup", closed="after teardown")
    notes = kept.annotations
    assert notes.installed.filename is None
    assert notes.installed.label == "before setup"
    assert notes.closed is not None
    assert notes.closed.monotonic >= notes.installed.monotonic
    expected = (
        "write on closed proxy for sys.stdout owned by 'kept', "
        "installed before setup, closed after teardown"
    )
    with pytest.warns(ProxyExpiredWarning) as record:
        with pytest.raises(ProxyExpiredError):
            kept.write("late")
    assert str(record[0].message) == expected


def test_use_after_lifetime_warns_then_raises() -> None:
    kept, _ = _misplace()
    with pytest.warns(ProxyExpiredWarning, match="owned by 'kept'") as record:
        with pytest.raises(ProxyExpiredError, match="closed proxy for sys.stdout"):
            print("late", file=kept)
    assert record[0].filename == __file__


def test_expired_error_fits_closed_files_and_descriptors() -> None:
    """Code that handles a closed file object or a bad descriptor keeps working."""
    error = ProxyExpiredError("closed")
    assert isinstance(error, ValueError)
    assert isinstance(error, OSError)
    assert error.errno == errno.EBADF


def test_write_back_without_a_target_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """A dead end never reaches for ``sys.__stdout__`` (D10)."""
    original = io.StringIO()
    monkeypatch.setattr(sys, "__stdout__", original)
    monkeypatch.setattr(sys, "stdout", None)
    kept, _ = _misplace(write_back=True, stderr=None)
    with pytest.warns(ProxyExpiredWarning):
        with pytest.raises(ProxyExpiredError, match="no write-back target") as info:
            kept.write("late")
    assert info.value.errno == errno.EBADF
    assert original.getvalue() == ""


def test_write_back_goes_to_the_current_slot() -> None:
    kept, _ = _misplace(write_back=True)
    with capture(name="now") as now:
        with pytest.warns(ProxyExpiredWarning):
            print("late", file=kept)
    assert now.out == "late\n"


def test_write_back_follows_closed_proxies_to_what_they_replaced() -> None:
    """A closed proxy put back in the slot by foreign code is not a target."""
    before = sys.stdout
    original = io.StringIO()
    sys.stdout = original
    try:
        kept, _ = _misplace(write_back=True)
        sys.stdout = kept  # foreign code restoring a stale reference
        with pytest.warns(ProxyExpiredWarning):
            print("late", file=kept)
    finally:
        sys.stdout = before
    assert original.getvalue() == "late\n"


def test_a_proxy_is_never_reused() -> None:
    with capture():
        first = sys.stdout
    with capture():
        assert sys.stdout is not first
