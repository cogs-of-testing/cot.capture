"""Terminal streams: never captured, never closed (design O9, D7)."""

from __future__ import annotations

import io
import os
import sys
import threading
from typing import Literal, TextIO

from ._descriptors import uncaptured_dup

Stream = Literal["stdout", "stderr"]

_FDS: dict[str, int] = {"stdout": 1, "stderr": 2}
_lock = threading.Lock()
_streams: dict[str, TerminalStream] = {}


class TerminalStream(io.TextIOBase):
    """A text stream that always reaches the uncaptured output.

    Obtain it with :func:`terminal`. It is never closed and never captured.
    """

    def __init__(self, name: Stream) -> None:
        super().__init__()
        self._name = name
        original = getattr(sys, f"__{name}__")
        self._encoding: str = getattr(original, "encoding", None) or "utf-8"
        self._errors: str = getattr(original, "errors", None) or "strict"
        self._fd: int | None = None
        self._out: TextIO | None
        #: the error that made the stream start discarding, if any
        self.failure: BaseException | None = None
        try:
            fd = uncaptured_dup(_FDS[name])
        except OSError:
            # No usable descriptor: fall back to the original object, and
            # discard if even that fails (design: writing never fails for lack
            # of a target).
            self._out = original
        else:
            self._fd = fd
            self._out = io.TextIOWrapper(
                io.FileIO(fd, "w", closefd=False),
                encoding=self._encoding,
                errors=self._errors,
                write_through=True,
            )

    def write(self, s: str) -> int:
        if self._out is not None:
            try:
                self._out.write(s)
            except (OSError, ValueError) as exc:
                self._give_up(exc)
        return len(s)

    def flush(self) -> None:
        if self._out is not None:
            try:
                self._out.flush()
            except (OSError, ValueError) as exc:
                self._give_up(exc)

    def _give_up(self, exc: BaseException) -> None:
        """The target broke (closed, broken pipe): discard from now on."""
        self.failure = exc
        self._out = None

    def writable(self) -> bool:
        return True

    def close(self) -> None:
        """Terminal streams live as long as the interpreter."""

    def __del__(self) -> None:
        pass

    @property
    def closed(self) -> bool:
        return False

    def fileno(self) -> int:
        if self._fd is None:
            if self._out is not None:
                return self._out.fileno()
            raise io.UnsupportedOperation(f"terminal {self._name} has no descriptor")
        return self._fd

    def isatty(self) -> bool:
        if self._fd is not None:
            return os.isatty(self._fd)
        return self._out is not None and self._out.isatty()

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return self._encoding

    @property
    def errors(self) -> str:  # type: ignore[override]
        return self._errors

    @property
    def name(self) -> str:
        return f"<cot.capture terminal {self._name}>"


def terminal(name: Stream) -> TerminalStream:
    """The interpreter's terminal stream for ``"stdout"`` or ``"stderr"``."""
    with _lock:
        stream = _streams.get(name)
        if stream is None:
            stream = _streams[name] = TerminalStream(name)
        return stream
