from __future__ import annotations


class CaptureWarning(UserWarning):
    """Base class of the warnings cot.capture emits."""


class ProxyExpiredWarning(CaptureWarning):
    """A proxy was used after the scope that created it ended."""


class SlotReplacedWarning(CaptureWarning):
    """A scope ended and its slot held an object it did not put there."""


class ProxyExpiredError(ValueError):
    """A proxy without write-back was used after its scope ended.

    A ``ValueError``, like any other I/O operation on a closed file.
    """


class StdinRefusedError(OSError):
    """A scope's stdin proxy was read without input being supplied."""


class BorrowError(RuntimeError):
    """A file descriptor borrow was refused or given back out of order."""
