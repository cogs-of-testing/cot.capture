"""Capturing logging records (design logging.md)."""

from __future__ import annotations

import dataclasses
import logging
import threading
import warnings
from collections.abc import Callable, Mapping, Sequence
from types import TracebackType
from typing import Union

from ._annotations import Location
from ._errors import LevelChangedWarning, SlotReplacedWarning
from ._scope import ForeignReplacement

#: Decides, from a complete batch and what the host knows about it, whether
#: the batch is kept (LD3).
Keep = Callable[[Sequence[logging.LogRecord], Mapping[str, object]], bool]

LevelSpec = Union[int, str]


@dataclasses.dataclass(frozen=True)
class DiscardPolicy:
    """What a log scope keeps (LD3).

    ``keep`` sees each complete batch, at :meth:`LogScope.take` and at the
    end of the scope, with the facts the host passed, and returns whether
    to keep it; ``None`` keeps every batch. ``max_records`` caps a batch
    while it is collected: beyond it the oldest records are dropped and
    counted in :attr:`LogScope.dropped`.
    """

    keep: Keep | None = None
    max_records: int | None = None

    def decide(self, batch: list[logging.LogRecord], facts: Mapping[str, object]) -> bool:
        return self.keep is None or self.keep(batch, facts)


KEEP_ALL = DiscardPolicy()


class _Router(logging.Handler):
    """The one handler that captures for every log scope (L2).

    It stores the record in the current scope and writes nothing (LD2).
    """

    def __init__(self) -> None:
        super().__init__()
        self.stack: list[LogScope] = []

    def handle(self, record: logging.LogRecord) -> bool:
        rv = self.filter(record)
        if rv and self.stack:
            # 3.12+: a filter may return a replacement record
            self.stack[-1]._add(rv if isinstance(rv, logging.LogRecord) else record)
        return bool(rv)

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - handle() stores
        pass

    def createLock(self) -> None:
        self.lock = None  # stores under the scope's lock

    def __repr__(self) -> str:
        owner = self.stack[0].name if self.stack else None
        return f"<cot.capture log router owned by {owner!r}>"


class _StandIn(logging.Handler):
    """Stands in for the router on a logger that was not propagating (L2).

    As in pytest #15138: at emit time, walk up from the logger while it
    propagates; forward only when the walk ends below the root at a logger
    nothing else delivers on, so each record reaches the router once.
    """

    def __init__(self, logger: logging.Logger, router: _Router) -> None:
        super().__init__()
        self.logger = logger
        self.router = router

    def createLock(self) -> None:
        self.lock = None  # never blocks against the router

    def handle(self, record: logging.LogRecord) -> bool:
        end = self.logger
        while end.propagate and end.parent is not None:
            end = end.parent
        if end is self.logger.root or (end is not self.logger and _stand_in(end) is not None):
            return False
        # the router's own level applies here, its filters in handle()
        if record.levelno >= self.router.level:
            self.router.handle(record)
        return True

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - handle() forwards
        pass

    def __repr__(self) -> str:
        return f"<cot.capture stand-in for {self.logger.name!r}>"


def _stand_in(logger: logging.Logger) -> _StandIn | None:
    for handler in logger.handlers:
        if isinstance(handler, _StandIn):
            return handler
    return None


# One router per interpreter (L2, L6): module state, like logging's own.
_lock = threading.Lock()
_router: _Router | None = None


class LogScope:
    """Captures the ``logging`` records emitted while it is active.

    It never changes what is logged (L1): ``levels`` maps logger names
    (``""`` for the root) to levels set for the scope's lifetime and put
    back afterwards (L4); ``threshold`` only filters what the scope keeps.
    ``discard`` decides which batches of records are kept (LD3).
    ``handlers`` are the host's own outputs (L8): each record the scope
    receives is handed to them, before the threshold, with their own levels
    and filters applying; this reaches them for non-propagating loggers too.

    Like :class:`Scope`, it takes ``installed`` and ``closed`` labels for a
    host that enters and leaves it from its own machinery.
    """

    def __init__(
        self,
        *,
        levels: Mapping[str, LevelSpec] | None = None,
        threshold: LevelSpec = logging.NOTSET,
        discard: DiscardPolicy = KEEP_ALL,
        handlers: Sequence[logging.Handler] = (),
        name: str | None = None,
        installed: str | None = None,
        closed: str | None = None,
    ) -> None:
        self.name = name or f"logs-{id(self):x}"
        self._levels = {n: _level(v) for n, v in (levels or {}).items()}
        self._threshold = _level(threshold)
        self._discard = discard
        self._handlers = tuple(handlers)
        self._labels = {"installed": installed, "closed": closed}
        self._lock = threading.Lock()
        self._batch: list[logging.LogRecord] = []
        self._kept: list[logging.LogRecord] = []
        self._saved_levels: dict[str, int] = {}
        self._stand_ins: list[_StandIn] = []
        self._router: _Router | None = None
        self._installed_at: Location | None = None
        self._ended = False
        self.dropped = 0
        self.diagnostics: list[ForeignReplacement] = []

    @property
    def records(self) -> list[logging.LogRecord]:
        """The records kept; complete once the scope ended.

        While the scope is active, the batch collected since the last
        :meth:`take`. After :meth:`take`, only what arrived after it.
        """
        with self._lock:
            return list(self._kept if self._ended else self._batch)

    def take(self, **facts: object) -> list[logging.LogRecord]:
        """End the current batch and return it, or ``[]`` if it is discarded.

        ``facts`` are what the host knows about the batch (such as whether a
        test phase passed); the discard policy decides with them (LD3).
        """
        with self._lock:
            batch, self._batch = self._batch, []
        return batch if self._discard.decide(batch, facts) else []

    def _add(self, record: logging.LogRecord) -> None:
        for handler in self._handlers:
            if record.levelno >= handler.level:
                handler.handle(record)
        if record.levelno < self._threshold:
            return
        with self._lock:
            self._batch.append(record)
            cap = self._discard.max_records
            if cap is not None and len(self._batch) > cap:
                del self._batch[0]
                self.dropped += 1

    def _where(self, event: str) -> Location:
        label = self._labels[event]
        return Location.here(2) if label is None else Location.described(label)

    def describe(self) -> str:
        return f"log scope {self.name!r}, installed {self._installed_at}"

    def __enter__(self) -> LogScope:
        global _router
        self._installed_at = self._where("installed")
        root = logging.getLogger()
        with _lock:
            if _router is None:
                _router = _Router()
                root.addHandler(_router)
            self._router = _router
            _router.stack.append(self)
            # each entry scans again; only what this scope adds it removes
            for logger in list(root.manager.loggerDict.values()):
                if (
                    isinstance(logger, logging.Logger)
                    and not logger.propagate
                    and _stand_in(logger) is None
                ):
                    stand_in = _StandIn(logger, _router)
                    logger.addHandler(stand_in)
                    self._stand_ins.append(stand_in)
        for logger_name, level in self._levels.items():
            logger = logging.getLogger(logger_name or None)
            self._saved_levels[logger_name] = logger.level
            logger.setLevel(level)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        global _router
        for logger_name, saved in self._saved_levels.items():
            logger = logging.getLogger(logger_name or None)
            if logger.level == self._levels[logger_name]:
                logger.setLevel(saved)
            else:
                self._report(
                    f"logging.getLogger({logger_name!r}).level",
                    logging.getLevelName(logger.level),
                    f"left at {logging.getLevelName(logger.level)}",
                    LevelChangedWarning,
                )
        self._saved_levels.clear()
        root = logging.getLogger()
        with _lock:
            for stand_in in self._stand_ins:
                stand_in.logger.removeHandler(stand_in)
            self._stand_ins.clear()
            router = self._router
            assert router is not None
            if self in router.stack:
                router.stack.remove(self)
            if not router.stack:
                if router in root.handlers:
                    root.removeHandler(router)
                else:
                    self._report(
                        "logging.getLogger().handlers",
                        repr(root.handlers),
                        "router gone",
                        SlotReplacedWarning,
                    )
                if _router is router:
                    _router = None
        batch = self.take()
        with self._lock:
            self._kept = batch
            self._ended = True

    def _report(
        self, slot: str, found: str, what: str, category: type[Warning]
    ) -> None:
        self.diagnostics.append(ForeignReplacement(slot=slot, found=found, proxy=self.describe()))
        warnings.warn(category(f"{self.describe()} ended with {slot}: {what}"), stacklevel=3)


def capture_logs(
    *,
    levels: Mapping[str, LevelSpec] | None = None,
    threshold: LevelSpec = logging.NOTSET,
    discard: DiscardPolicy = KEEP_ALL,
    handlers: Sequence[logging.Handler] = (),
    name: str | None = None,
    installed: str | None = None,
    closed: str | None = None,
) -> LogScope:
    """Create a :class:`LogScope`; use it as a context manager."""
    return LogScope(
        levels=levels,
        threshold=threshold,
        discard=discard,
        handlers=handlers,
        name=name,
        installed=installed,
        closed=closed,
    )


def _level(level: LevelSpec) -> int:
    if isinstance(level, int):
        return level
    value = logging.getLevelName(level.upper())
    if not isinstance(value, int):
        raise ValueError(f"unknown logging level {level!r}")
    return value
