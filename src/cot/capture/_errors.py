from __future__ import annotations

import errno


class CaptureWarning(UserWarning):
    """Base class of the warnings cot.capture emits."""


class ProxyExpiredWarning(CaptureWarning):
    """A proxy was used after the scope that created it ended."""


class TerminalClosedWarning(CaptureWarning):
    """A terminal stream was used after its owner closed it."""


class RecorderExpiredWarning(CaptureWarning):
    """A recorder was called after the recording that installed it ended."""


class SlotReplacedWarning(CaptureWarning):
    """A scope ended and its slot held an object it did not put there."""


class ProxyExpiredError(OSError, ValueError):
    """A proxy was used after its scope ended and had nowhere to write back.

    An ``OSError`` with ``errno.EBADF``, as for a closed file descriptor, and
    a ``ValueError``, as for I/O on a closed Python file object, so code that
    handles either keeps working (like ``io.UnsupportedOperation``).
    """

    def __init__(self, message: str) -> None:
        super().__init__(errno.EBADF, message)


class StdinRefusedError(OSError):
    """A scope's stdin proxy was read without input being supplied."""


class BorrowError(RuntimeError):
    """A file descriptor borrow was refused or given back out of order."""
