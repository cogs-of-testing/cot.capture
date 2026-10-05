from __future__ import annotations

import dataclasses
import sys
import time


@dataclasses.dataclass(frozen=True)
class Location:
    """Where and when something happened to a proxy."""

    filename: str
    lineno: int
    monotonic: float

    @classmethod
    def here(cls, depth: int) -> Location:
        """The caller ``depth`` frames above the caller of ``here``."""
        frame = sys._getframe(depth + 1)
        return cls(frame.f_code.co_filename, frame.f_lineno, time.monotonic())

    def __str__(self) -> str:
        return f"{self.filename}:{self.lineno}"


@dataclasses.dataclass
class Annotations:
    """The history a proxy carries (design O2)."""

    slot: str
    owner: str
    installed: Location
    closed: Location | None = None

    def describe(self) -> str:
        text = f"proxy for sys.{self.slot} owned by {self.owner!r}, installed at {self.installed}"
        if self.closed is not None:
            text += f", closed at {self.closed}"
        return text
