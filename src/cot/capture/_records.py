"""Recording warnings, unraisable and thread exceptions (design warnings.md)."""

from __future__ import annotations

import dataclasses
import gc
import re
import sys
import threading
import time
import traceback
import warnings as _warnings
from collections.abc import Callable, Sequence
from types import TracebackType
from typing import Any, Literal, Union

from ._annotations import Location
from ._errors import RecorderExpiredWarning, SlotReplacedWarning
from ._scope import ForeignReplacement

Source = Literal["warnings", "unraisable", "thread_exceptions"]

# The slot each source records through (design: What there is to capture).
_SLOTS: dict[Source, tuple[object, str]] = {
    "warnings": (_warnings, "showwarning"),
    "unraisable": (sys, "unraisablehook"),
    "thread_exceptions": (threading, "excepthook"),
}


@dataclasses.dataclass(frozen=True)
class WarningRecord:
    """A warning that the filters let through (W4)."""

    message: _warnings.WarningMessage
    thread: str
    time: float


@dataclasses.dataclass(frozen=True)
class UnraisableRecord:
    """An exception Python could not raise, from ``sys.unraisablehook`` (W4).

    ``object_repr`` and ``traceback`` are taken while the hook runs: the
    object may be finalized as soon as it returns.
    """

    exc_type: type[BaseException]
    exc_value: BaseException | None
    err_msg: str
    object_repr: str
    traceback: str
    thread: str
    time: float


@dataclasses.dataclass(frozen=True)
class ThreadExceptionRecord:
    """An exception that ended a thread, from ``threading.excepthook`` (W4)."""

    exc_type: type[BaseException]
    exc_value: BaseException | None
    thread: str
    traceback: str
    time: float


Record = Union[WarningRecord, UnraisableRecord, ThreadExceptionRecord]

_Action = Literal["default", "error", "ignore", "always", "module", "once"]

#: A parsed warning filter, as ``warnings.filterwarnings`` takes it: action,
#: message regular expression, category, module regular expression, line.
WarningFilter = tuple[_Action, str, type[Warning], str, int]


@dataclasses.dataclass(frozen=True)
class GCPolicy:
    """When a recording runs the garbage collector (WD3).

    Finalizers only run, and report unraisable exceptions, when garbage is
    collected; a recording that collects before it reads attributes them to
    itself. ``passes`` is the number of ``gc.collect()`` calls, ``0`` for
    none; ``on_take`` and ``on_end`` say where they run.
    """

    passes: int = 0
    on_take: bool = True
    on_end: bool = True

    def collect(self) -> None:
        for _ in range(self.passes):
            gc.collect()


NEVER = GCPolicy()


class _Recorder:
    """What a recording puts in one slot; annotated like a proxy (W2, W3)."""

    def __init__(self, recording: Recording, source: Source, replaced: object) -> None:
        self.recording = recording
        self.source = source
        self.replaced = replaced
        self.expired = False

    def describe(self) -> str:
        module, attr = _SLOTS[self.source]
        text = (
            f"recorder for {_name(module)}.{attr} owned by {self.recording.name!r}, "
            f"installed {self.recording._installed_at}"
        )
        if self.recording._closed_at is not None:
            text += f", closed {self.recording._closed_at}"
        return text

    def _passed_on(self) -> Callable[..., object] | None:
        """W3: warn, then find whatever the slot holds now."""
        _warnings.warn(
            RecorderExpiredWarning(f"called closed {self.describe()}"),
            stacklevel=3,
        )
        module, attr = _SLOTS[self.source]
        seen: set[int] = set()
        obj: object = getattr(module, attr)
        while isinstance(obj, _Recorder) and obj.expired:
            if id(obj) in seen:
                return None
            seen.add(id(obj))
            obj = obj.replaced
        return obj if callable(obj) else None


class _WarningRecorder(_Recorder):
    def __call__(
        self,
        message: Warning | str,
        category: type[Warning],
        filename: str,
        lineno: int,
        file: Any = None,
        line: str | None = None,
    ) -> None:
        if self.expired:
            target = self._passed_on()
            if target is not None:
                target(message, category, filename, lineno, file, line)
            return
        msg = _warnings.WarningMessage(message, category, filename, lineno, file, line)
        self.recording._add(WarningRecord(msg, _thread_name(), time.monotonic()))


class _UnraisableRecorder(_Recorder):
    def __call__(self, unraisable: Any) -> None:
        if self.expired:
            target = self._passed_on()
            if target is not None:
                target(unraisable)
            return
        self.recording._add(
            UnraisableRecord(
                exc_type=unraisable.exc_type,
                exc_value=unraisable.exc_value,
                err_msg=unraisable.err_msg or "Exception ignored in",
                object_repr=_safe_repr(unraisable.object),
                traceback=_format(
                    unraisable.exc_type, unraisable.exc_value, unraisable.exc_traceback
                ),
                thread=_thread_name(),
                time=time.monotonic(),
            )
        )


class _ThreadExceptionRecorder(_Recorder):
    def __call__(self, args: Any) -> None:
        if self.expired:
            target = self._passed_on()
            if target is not None:
                target(args)
            return
        thread = args.thread
        self.recording._add(
            ThreadExceptionRecord(
                exc_type=args.exc_type,
                exc_value=args.exc_value,
                thread=thread.name if thread is not None else "<unknown thread>",
                traceback=_format(args.exc_type, args.exc_value, args.exc_traceback),
                time=time.monotonic(),
            )
        )


_RECORDERS: dict[Source, type[_Recorder]] = {
    "warnings": _WarningRecorder,
    "unraisable": _UnraisableRecorder,
    "thread_exceptions": _ThreadExceptionRecorder,
}


class Recording:
    """Records warnings, unraisable and thread exceptions while it is active.

    Each source is a slot (``warnings.showwarning``, ``sys.unraisablehook``,
    ``threading.excepthook``) that the recording puts a recorder in and
    restores when it ends (W2). ``filters`` are warning filters applied on
    top of the current ones for the recording's lifetime, in order, so a
    later one wins (W1): strings in the ``-W`` syntax
    (``action:message:category:module:lineno``, literal text) or parsed
    :data:`WarningFilter` tuples (regular expressions). Without them the
    recording records whatever the filters in force let through. ``gc`` says
    when the garbage collector runs before records are read (WD3).

    Like :class:`Scope`, it takes ``installed`` and ``closed`` labels for a
    host that enters and leaves it from its own machinery.
    """

    def __init__(
        self,
        *,
        warnings: bool = True,
        unraisable: bool = True,
        thread_exceptions: bool = True,
        filters: Sequence[str | WarningFilter] = (),
        gc: GCPolicy = NEVER,
        name: str | None = None,
        installed: str | None = None,
        closed: str | None = None,
    ) -> None:
        self.name = name or f"recording-{id(self):x}"
        wanted: dict[Source, bool] = {
            "warnings": warnings,
            "unraisable": unraisable,
            "thread_exceptions": thread_exceptions,
        }
        self._sources = [source for source, on in wanted.items() if on]
        self._filters = [_parse_filter(f) if isinstance(f, str) else f for f in filters]
        self._gc = gc
        self._labels = {"installed": installed, "closed": closed}
        self._lock = threading.Lock()
        self._records: list[Record] = []
        self._installed: list[_Recorder] = []
        self._catch: _warnings.catch_warnings[None] | None = None
        self._installed_at: Location | None = None
        self._closed_at: Location | None = None
        self._active = False
        self.diagnostics: list[ForeignReplacement] = []

    @property
    def records(self) -> list[Record]:
        """What was recorded; complete once the recording ended.

        After :meth:`take`, only what arrived after the last call.
        """
        with self._lock:
            return list(self._records)

    def take(self) -> list[Record]:
        """Return and drop what was recorded so far; the recording stays active."""
        if self._gc.on_take:
            self._gc.collect()
        with self._lock:
            taken, self._records = self._records, []
        return taken

    def _add(self, record: Record) -> None:
        with self._lock:
            self._records.append(record)

    def _where(self, event: str) -> Location:
        label = self._labels[event]
        return Location.here(2) if label is None else Location.described(label)

    def __enter__(self) -> Recording:
        if self._active:
            raise RuntimeError(f"recording {self.name!r} is already active")
        self._active = True
        self._installed_at = self._where("installed")
        if self._filters:
            # catch_warnings keeps the filters per context where the build
            # does (WD2); recording stays in showwarning (WD1).
            self._catch = _warnings.catch_warnings()
            self._catch.__enter__()
            for action, message, category, module_re, lineno in self._filters:
                _warnings.filterwarnings(action, message, category, module_re, lineno)
        for source in self._sources:
            module, attr = _SLOTS[source]
            recorder = _RECORDERS[source](self, source, getattr(module, attr))
            setattr(module, attr, recorder)
            self._installed.append(recorder)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._closed_at = self._where("closed")
        if self._gc.on_end:
            # before the hooks go: finalizers report to this recording
            self._gc.collect()
        foreign: list[tuple[object, str, object]] = []
        while self._installed:
            recorder = self._installed.pop()
            module, attr = _SLOTS[recorder.source]
            current = getattr(module, attr)
            if current is recorder:
                setattr(module, attr, recorder.replaced)
            else:
                found = ForeignReplacement(
                    slot=f"{_name(module)}.{attr}",
                    found=repr(current),
                    proxy=recorder.describe(),
                )
                self.diagnostics.append(found)
                foreign.append((module, attr, current))
                _warnings.warn(
                    SlotReplacedWarning(
                        f"recording {self.name!r} ended with {found.slot} holding "
                        f"{found.found}, not its recorder; left in place"
                    ),
                    stacklevel=2,
                )
            recorder.expired = True
        if self._catch is not None:
            self._catch.__exit__(None, None, None)
            self._catch = None
            # catch_warnings put showwarning back; a foreign one stays (O4)
            for module, attr, current in foreign:
                setattr(module, attr, current)
        self._active = False


def record(
    *,
    warnings: bool = True,
    unraisable: bool = True,
    thread_exceptions: bool = True,
    filters: Sequence[str | WarningFilter] = (),
    gc: GCPolicy = NEVER,
    name: str | None = None,
    installed: str | None = None,
    closed: str | None = None,
) -> Recording:
    """Create a :class:`Recording`; use it as a context manager."""
    return Recording(
        warnings=warnings,
        unraisable=unraisable,
        thread_exceptions=thread_exceptions,
        filters=filters,
        gc=gc,
        name=name,
        installed=installed,
        closed=closed,
    )


_ACTIONS: tuple[_Action, ...] = ("default", "always", "ignore", "module", "once", "error")


def _parse_filter(
    spec: str,
) -> WarningFilter:
    """One ``-W`` option, as the interpreter reads it.

    The message and module fields are literal text, as with ``-W`` (not
    regular expressions, as with ``_warnings.filterwarnings``).
    """
    parts = [part.strip() for part in spec.split(":")]
    if len(parts) > 5:
        raise ValueError(f"too many fields in warning filter {spec!r}")
    parts += [""] * (5 - len(parts))
    action_text, message, category_text, module, lineno_text = parts
    action = _action(action_text, spec)
    category = _category(category_text, spec)
    if message:
        message = re.escape(message)
    if module:
        module = re.escape(module) + r"\Z"
    try:
        lineno = int(lineno_text) if lineno_text else 0
    except ValueError:
        raise ValueError(f"invalid line number in warning filter {spec!r}") from None
    if lineno < 0:
        raise ValueError(f"invalid line number in warning filter {spec!r}")
    return action, message, category, module, lineno


def _action(text: str, spec: str) -> _Action:
    if not text:
        return "default"
    for action in _ACTIONS:
        if action.startswith(text):
            return action
    raise ValueError(f"invalid action {text!r} in warning filter {spec!r}")


def _category(text: str, spec: str) -> type[Warning]:
    if not text:
        return Warning
    module_name, _, klass = text.rpartition(".")
    try:
        if module_name:
            module = __import__(module_name, None, None, [klass])
            cls = getattr(module, klass)
        else:
            import builtins

            cls = getattr(builtins, klass)
    except (ImportError, AttributeError):
        raise ValueError(f"unknown warning category {text!r} in filter {spec!r}") from None
    if not (isinstance(cls, type) and issubclass(cls, Warning)):
        raise ValueError(f"{text!r} in filter {spec!r} is not a Warning subclass")
    return cls


def _name(module: object) -> str:
    return getattr(module, "__name__", repr(module))


def _thread_name() -> str:
    return threading.current_thread().name


def _safe_repr(obj: object) -> str:
    try:
        return repr(obj)
    except Exception as e:  # noqa: BLE001 - a broken __repr__ must not lose the record
        return f"<repr of {type(obj).__name__} failed: {e!r}>"


def _format(
    exc_type: type[BaseException], exc_value: BaseException | None, tb: TracebackType | None
) -> str:
    return "".join(traceback.format_exception(exc_type, exc_value, tb))
