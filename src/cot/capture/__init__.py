"""Building blocks for capturing output.

See ``docs/design/streams.md`` for the ownership rules this follows.
"""

from __future__ import annotations

from ._annotations import Annotations, Location
from ._descriptors import Borrow, borrow, protect, unprotect
from ._errors import (
    BorrowError,
    CaptureWarning,
    ProxyExpiredError,
    ProxyExpiredWarning,
    SlotReplacedWarning,
    StdinRefusedError,
)
from ._proxy import StreamProxy
from ._scope import ForeignReplacement, Level, Scope, capture
from ._terminal import TerminalStream, terminal

__all__ = [
    "Annotations",
    "Borrow",
    "BorrowError",
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
    "TerminalStream",
    "borrow",
    "capture",
    "protect",
    "terminal",
    "unprotect",
]
