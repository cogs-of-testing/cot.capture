"""The proxy that stands in a slot for one scope (design O1 to O3, Proxies)."""

from __future__ import annotations

import io
import sys
import warnings
from collections.abc import Iterable
from typing import TextIO

from ._annotations import Annotations, Location
from ._errors import ProxyExpiredError, ProxyExpiredWarning, StdinRefusedError


class StreamProxy(io.TextIOBase):
    """A text file object installed in ``sys.<slot>`` for one scope.

    While its scope is active it forwards to the scope's capture target. After
    the scope ends it is closed: every use warns, then either writes back
    (if requested) or raises :class:`ProxyExpiredError`.
    """

    def __init__(
        self,
        *,
        slot: str,
        annotations: Annotations,
        replaced: object,
        target: TextIO | None,
        fd: int | None = None,
        write_back: bool = False,
        stdin_text: str | None = None,
    ) -> None:
        super().__init__()
        self.annotations = annotations
        self._slot = slot
        self._replaced = replaced
        self._target = target
        self._fd = fd
        self._write_back = write_back
        self._input = None if stdin_text is None else io.StringIO(stdin_text)
        self._expired = False
        self._encoding: str = getattr(replaced, "encoding", None) or "utf-8"
        self._errors: str = getattr(replaced, "errors", None) or "strict"

    # -- lifetime -----------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._expired

    def close(self) -> None:
        """Code under test cannot close a proxy; its scope does."""

    def __del__(self) -> None:
        pass

    def _expire(self, where: Location) -> None:
        self.annotations.closed = where
        self._expired = True

    def _after_lifetime(self, op: str, stacklevel: int) -> TextIO:
        # stacklevel counts from the public method the caller used.
        warnings.warn(
            ProxyExpiredWarning(f"{op} on closed {self.annotations.describe()}"),
            stacklevel=stacklevel + 2,
        )
        if not self._write_back:
            raise ProxyExpiredError(f"{op} on closed {self.annotations.describe()}")
        return self._write_back_target()

    def _write_back_target(self) -> TextIO:
        seen: set[int] = set()
        obj: object = getattr(sys, self._slot)
        while isinstance(obj, StreamProxy) and obj.closed:
            if id(obj) in seen:
                obj = None
                break
            seen.add(id(obj))
            obj = obj._replaced
        # A dead end raises rather than reaching for sys.__stdout__ (D10).
        if obj is None:
            raise ProxyExpiredError(
                f"no write-back target for {self.annotations.describe()}"
            )
        return obj  # type: ignore[return-value]

    def _live_target(self, op: str) -> TextIO:
        if self._expired:
            return self._after_lifetime(op, 2)
        if self._target is None:
            raise io.UnsupportedOperation(f"{op} on {self.annotations.describe()}")
        return self._target

    # -- writing ------------------------------------------------------------

    def writable(self) -> bool:
        return self._target is not None

    def write(self, s: str) -> int:
        self._live_target("write").write(s)
        return len(s)

    def writelines(self, lines: Iterable[str]) -> None:  # type: ignore[override]
        target = self._live_target("writelines")
        for line in lines:
            target.write(line)

    def flush(self) -> None:
        if self._expired:
            self._after_lifetime("flush", 1).flush()
        elif self._target is not None:
            self._target.flush()

    # -- reading (stdin) ----------------------------------------------------

    def readable(self) -> bool:
        return self._slot == "stdin"

    def _source(self, op: str) -> TextIO:
        if self._expired:
            return self._after_lifetime(op, 2)
        if self._input is None:
            raise StdinRefusedError(
                f"{op} from {self.annotations.describe()}, which was given no input"
            )
        return self._input

    def read(self, size: int | None = -1) -> str:
        return self._source("read").read(-1 if size is None else size)

    def readline(self, size: int | None = -1) -> str:  # type: ignore[override]
        return self._source("readline").readline(-1 if size is None else size)

    # -- stream attributes --------------------------------------------------

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return self._encoding

    @property
    def errors(self) -> str:  # type: ignore[override]
        return self._errors

    @property
    def name(self) -> str:
        return f"<cot.capture proxy for sys.{self._slot}>"

    @property
    def mode(self) -> str:
        return "r" if self._slot == "stdin" else "w"

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        if self._fd is None:
            raise io.UnsupportedOperation(
                f"fileno() on {self.annotations.describe()}: slot-level capture has no descriptor"
            )
        return self._fd
