"""Terminal streams: never captured, owned and closed by the caller (O9, D7)."""

from __future__ import annotations

import io
import os
import sys
import threading
import warnings
from typing import Literal, TextIO

from ._annotations import Location
from ._descriptors import uncaptured_dup
from ._errors import TerminalClosedWarning

Stream = Literal["stdout", "stderr", "stdin"]

_FDS: dict[str, int] = {"stdout": 1, "stderr": 2, "stdin": 0}


class TerminalStream(io.TextIOBase):
    """A text stream that reaches the uncaptured terminal while it is open.

    Obtain it with :func:`terminal`; whoever obtained it closes it, which
    releases its descriptor. The stdout and stderr streams write and never
    raise; the stdin stream reads, unbuffered, so it takes no more from the
    terminal than it returns.
    """

    def __init__(self, name: Stream, owner: str, created: Location) -> None:
        super().__init__()
        self._name = name
        self.owner = owner
        self.created = created
        self.closed_at: Location | None = None
        #: the error that made the stream start discarding, if any
        self.failure: BaseException | None = None
        self._lock = threading.Lock()
        original = getattr(sys, f"__{name}__")
        self._encoding: str = getattr(original, "encoding", None) or "utf-8"
        self._errors: str = getattr(original, "errors", None) or "strict"
        self._fd: int | None = None
        self._out: TextIO | None = None
        try:
            fd = uncaptured_dup(_FDS[name])
        except OSError:
            # No usable descriptor: fall back to the original object, and
            # discard (or read end of file) if even that fails.
            self._out = original
        else:
            self._fd = fd
            if name != "stdin":
                self._out = io.TextIOWrapper(
                    io.FileIO(fd, "w", closefd=False),
                    encoding=self._encoding,
                    errors=self._errors,
                    write_through=True,
                )

    def describe(self) -> str:
        text = f"terminal {self._name} owned by {self.owner!r}, created at {self.created}"
        if self.closed_at is not None:
            text += f", closed at {self.closed_at}"
        return text

    # -- writing ------------------------------------------------------------

    def _target(self, op: str) -> TextIO | None:
        if self.closed_at is None:
            return self._out
        warnings.warn(
            TerminalClosedWarning(
                f"{op} on closed {self.describe()}; using sys.__{self._name}__ instead"
            ),
            stacklevel=3,
        )
        return getattr(sys, f"__{self._name}__")  # type: ignore[no-any-return]

    def write(self, s: str) -> int:
        if self._name == "stdin":
            raise io.UnsupportedOperation(f"{self.describe()} is not writable")
        target = self._target("write")
        if target is not None:
            try:
                target.write(s)
            except (OSError, ValueError) as exc:
                self._give_up(exc, target)
        return len(s)

    def flush(self) -> None:
        target = self._target("flush")
        if target is not None:
            try:
                target.flush()
            except (OSError, ValueError) as exc:
                self._give_up(exc, target)

    def _give_up(self, exc: BaseException, target: TextIO) -> None:
        """The target broke (closed, broken pipe): discard from now on."""
        self.failure = exc
        if target is self._out:
            self._out = None

    def writable(self) -> bool:
        return self._name != "stdin"

    # -- reading (stdin) ----------------------------------------------------

    def readable(self) -> bool:
        return self._name == "stdin"

    def readline(self, size: int | None = -1) -> str:  # type: ignore[override]
        """One line from the terminal; ``""`` at end of file or on failure."""
        if self._name != "stdin":
            raise io.UnsupportedOperation(f"{self.describe()} is not readable")
        limit = -1 if size is None else size
        if self.closed_at is not None or self._fd is None:
            fallback = self._target("readline") if self.closed_at is not None else self._out
            if fallback is None:
                return ""
            try:
                return fallback.readline(limit)
            except (OSError, ValueError) as exc:
                self.failure = exc
                return ""
        # Byte by byte, so nothing after the line is taken from the terminal
        # and lost when the stream is closed.
        line = bytearray()
        try:
            while limit < 0 or len(line) < limit:
                byte = os.read(self._fd, 1)
                if not byte:
                    break
                line += byte
                if byte == b"\n":
                    break
        except OSError as exc:
            self.failure = exc
        return line.decode(self._encoding, self._errors)

    def read(self, size: int | None = -1) -> str:
        if size is not None and size >= 0:
            return self.readline(size)
        return "".join(iter(self.readline, ""))

    # -- lifetime -----------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self.closed_at is not None

    def close(self) -> None:
        """Release the descriptor. Later writes warn and go to ``sys.__stdout__``."""
        self._release(Location.here(1))

    def _release(self, where: Location) -> None:
        with self._lock:
            if self.closed_at is not None:
                return
            self.closed_at = where
            fd, self._fd, self._out = self._fd, None, None
        if fd is not None:
            os.close(fd)

    def __del__(self) -> None:
        if self.closed_at is None and self._fd is not None:
            try:
                warnings.warn(
                    ResourceWarning(f"unclosed {self.describe()}"),
                    source=self,
                    stacklevel=2,
                )
            finally:
                self._release(self.created)

    # -- stream attributes --------------------------------------------------

    def fileno(self) -> int:
        if self._fd is not None:
            return self._fd
        if self._out is not None:
            return self._out.fileno()
        raise io.UnsupportedOperation(f"{self.describe()} has no descriptor")

    def isatty(self) -> bool:
        if self._fd is not None:
            return os.isatty(self._fd)
        try:
            return self._out is not None and self._out.isatty()
        except (OSError, ValueError):
            return False

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return self._encoding

    @property
    def errors(self) -> str:  # type: ignore[override]
        return self._errors

    @property
    def name(self) -> str:
        return f"<cot.capture terminal {self._name}>"


def terminal(name: Stream, *, owner: str | None = None) -> TerminalStream:
    """A new terminal stream for ``"stdout"``, ``"stderr"`` or ``"stdin"``.

    Each call makes a private ``dup`` of the uncaptured descriptor; close the
    stream (or use it as a context manager) to release it.
    """
    created = Location.here(1)
    return TerminalStream(name, owner or f"{created}", created)
