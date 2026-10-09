"""Building blocks for capturing output.

See ``docs/design/streams.md`` for the ownership rules this follows.
"""

from __future__ import annotations

from ._annotations import Annotations, Location
from ._descriptors import Borrow, CaptureFile, borrow, protect, unprotect
from ._errors import (
    BorrowError,
    CaptureWarning,
    ProxyExpiredError,
    ProxyExpiredWarning,
    RecorderExpiredWarning,
    SlotReplacedWarning,
    StdinRefusedError,
    TerminalClosedWarning,
)
from ._proxy import StreamProxy
from ._records import (
    GCPolicy,
    Record,
    Recording,
    ThreadExceptionRecord,
    UnraisableRecord,
    WarningFilter,
    WarningRecord,
    record,
)
from ._scope import CaptureFiles, ForeignReplacement, Level, Scope, capture, capture_files
from ._terminal import TerminalStream, terminal

__all__ = [
    "Annotations",
    "Borrow",
    "BorrowError",
    "CaptureFile",
    "CaptureFiles",
    "CaptureWarning",
    "ForeignReplacement",
    "GCPolicy",
    "Level",
    "Location",
    "ProxyExpiredError",
    "ProxyExpiredWarning",
    "Record",
    "RecorderExpiredWarning",
    "Recording",
    "Scope",
    "SlotReplacedWarning",
    "StdinRefusedError",
    "StreamProxy",
    "TerminalClosedWarning",
    "TerminalStream",
    "ThreadExceptionRecord",
    "UnraisableRecord",
    "WarningFilter",
    "WarningRecord",
    "borrow",
    "capture",
    "capture_files",
    "protect",
    "record",
    "terminal",
    "unprotect",
]
