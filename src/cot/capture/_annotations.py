from __future__ import annotations

import dataclasses
import sys
import time

# Frames of these modules are machinery between the code that entered or left
# a scope and the scope itself: cot.capture's own, and contextlib's
# (``ExitStack``, ``@contextmanager``). A location names the first frame
# outside them.
_PASS_THROUGH = ("contextlib",)
_OWN_PREFIX = "cot.capture._"


def _passes_through(module: object) -> bool:
    return isinstance(module, str) and (
        module in _PASS_THROUGH or module.startswith(_OWN_PREFIX)
    )


@dataclasses.dataclass(frozen=True)
class Location:
    """Where and when something happened to a proxy.

    Either a code location (``filename`` and ``lineno``) or, when the code on
    the stack is a host's machinery and no file and line would say anything
    useful, a ``label`` saying where in the host's run it happened.
    """

    filename: str | None
    lineno: int | None
    monotonic: float
    label: str | None = None

    @classmethod
    def here(cls, depth: int) -> Location:
        """The caller ``depth`` frames above the caller of ``here``.

        Frames of cot.capture's internal modules and of ``contextlib`` are
        skipped, so a scope entered through an ``ExitStack`` names the code
        that entered the stack.
        """
        frame = sys._getframe(depth + 1)
        while frame.f_back is not None and _passes_through(frame.f_globals.get("__name__")):
            frame = frame.f_back
        return cls(frame.f_code.co_filename, frame.f_lineno, time.monotonic())

    @classmethod
    def described(cls, label: str) -> Location:
        """Now, at a point of a host's run that ``label`` names."""
        return cls(None, None, time.monotonic(), label)

    def __str__(self) -> str:
        """``at FILE:LINE``, or the label, to follow "installed" or "closed"."""
        if self.label is not None:
            return self.label
        return f"at {self.filename}:{self.lineno}"


@dataclasses.dataclass
class Annotations:
    """The history a proxy carries (design O2)."""

    slot: str
    owner: str
    installed: Location
    closed: Location | None = None

    def describe(self) -> str:
        text = f"proxy for sys.{self.slot} owned by {self.owner!r}, installed {self.installed}"
        if self.closed is not None:
            text += f", closed {self.closed}"
        return text
