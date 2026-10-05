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
    SlotReplacedWarning,
    StdinRefusedError,
    TerminalClosedWarning,
)
from ._proxy import StreamProxy
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
    "Level",
    "Location",
    "ProxyExpiredError",
    "ProxyExpiredWarning",
    "Scope",
    "SlotReplacedWarning",
    "StdinRefusedError",
    "StreamProxy",
    "TerminalClosedWarning",
    "TerminalStream",
    "borrow",
    "capture",
    "capture_files",
    "protect",
    "terminal",
    "unprotect",
]
