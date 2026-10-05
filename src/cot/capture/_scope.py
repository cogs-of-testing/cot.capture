"""Scopes: a block of code whose standard streams are captured (design Scopes)."""

from __future__ import annotations

import dataclasses
import io
import sys
import warnings
from types import TracebackType
from typing import Literal, TextIO

from ._annotations import Annotations, Location
from ._descriptors import Borrow, borrow
from ._errors import SlotReplacedWarning
from ._proxy import StreamProxy

Level = Literal["slot", "fd"]

_FDS = {"stdout": 1, "stderr": 2}


@dataclasses.dataclass(frozen=True)
class ForeignReplacement:
    """A scope ended and its slot held an object it did not install (O4)."""

    slot: str
    found: str
    proxy: str


@dataclasses.dataclass
class _Installed:
    slot: str
    proxy: StreamProxy
    replaced: object
    target: TextIO | None
    borrow: Borrow | None


class Scope:
    """Captures the standard streams of the code run inside it.

    ``stdout`` and ``stderr`` take a level, ``"slot"`` or ``"fd"``, or
    ``None`` to leave the stream alone. ``stdin`` is ``False`` to leave it
    alone, ``True`` to refuse reads, or a string to serve as input.
    ``write_back`` makes the scope's proxies write back to the slot after
    the scope ended, instead of raising.
    """

    def __init__(
        self,
        *,
        stdout: Level | None = "slot",
        stderr: Level | None = "slot",
        stdin: bool | str = False,
        name: str | None = None,
        write_back: bool = False,
    ) -> None:
        self.name = name or f"scope-{id(self):x}"
        self._levels: dict[str, Level | None] = {"stdout": stdout, "stderr": stderr}
        self._stdin = stdin
        self._write_back = write_back
        self._installed: list[_Installed] = []
        self._captured: dict[str, str] = {}
        self.diagnostics: list[ForeignReplacement] = []

    @property
    def out(self) -> str:
        """What was captured from stdout; complete once the scope ended."""
        return self._captured.get("stdout", "")

    @property
    def err(self) -> str:
        """What was captured from stderr; complete once the scope ended."""
        return self._captured.get("stderr", "")

    def proxy(self, slot: str) -> StreamProxy:
        for item in self._installed:
            if item.slot == slot:
                return item.proxy
        raise KeyError(slot)

    def __enter__(self) -> Scope:
        here = Location.here(1)
        for slot, level in self._levels.items():
            if level is not None:
                self._install(slot, level, here)
        if self._stdin is not False:
            text = self._stdin if isinstance(self._stdin, str) else None
            self._install_proxy("stdin", None, None, None, here, stdin_text=text)
        return self

    def _install(self, slot: str, level: Level, here: Location) -> None:
        replaced = getattr(sys, slot)
        encoding = getattr(replaced, "encoding", None) or "utf-8"
        errors = getattr(replaced, "errors", None) or "strict"
        target: TextIO
        if level == "fd":
            fd = _FDS[slot]
            _flush(replaced)
            b = borrow(fd, self.name)
            target = io.TextIOWrapper(
                io.FileIO(fd, "w", closefd=False),
                encoding=encoding,
                errors=errors,
                write_through=True,
            )
            self._install_proxy(slot, target, b, fd, here)
        else:
            self._install_proxy(slot, io.StringIO(), None, None, here)

    def _install_proxy(
        self,
        slot: str,
        target: TextIO | None,
        b: Borrow | None,
        fd: int | None,
        here: Location,
        stdin_text: str | None = None,
    ) -> None:
        replaced = getattr(sys, slot)
        proxy = StreamProxy(
            slot=slot,
            annotations=Annotations(slot=slot, owner=self.name, installed=here),
            replaced=replaced,
            target=target,
            fd=fd,
            write_back=self._write_back,
            stdin_text=stdin_text,
        )
        setattr(sys, slot, proxy)
        self._installed.append(_Installed(slot, proxy, replaced, target, b))

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        here = Location.here(1)
        while self._installed:
            item = self._installed.pop()
            current = getattr(sys, item.slot)
            if current is item.proxy:
                setattr(sys, item.slot, item.replaced)
            else:
                found = ForeignReplacement(
                    slot=item.slot,
                    found=repr(current),
                    proxy=item.proxy.annotations.describe(),
                )
                self.diagnostics.append(found)
                warnings.warn(
                    SlotReplacedWarning(
                        f"scope {self.name!r} ended with sys.{item.slot} holding "
                        f"{found.found}, not its proxy; left in place"
                    ),
                    stacklevel=2,
                )
            item.proxy._expire(here)
            self._collect(item)

    def _collect(self, item: _Installed) -> None:
        target = item.target
        if target is None:
            return
        if item.borrow is not None:
            target.flush()
            target.close()
            data = item.borrow.give_back()
            self._captured[item.slot] = data.decode(item.proxy.encoding, "replace")
        else:
            assert isinstance(target, io.StringIO)
            self._captured[item.slot] = target.getvalue()


def _flush(stream: object) -> None:
    flush = getattr(stream, "flush", None)
    if flush is not None:
        try:
            flush()
        except (OSError, ValueError):
            pass


def capture(
    *,
    stdout: Level | None = "slot",
    stderr: Level | None = "slot",
    stdin: bool | str = False,
    name: str | None = None,
    write_back: bool = False,
) -> Scope:
    """Create a :class:`Scope`; use it as a context manager."""
    return Scope(
        stdout=stdout, stderr=stderr, stdin=stdin, name=name, write_back=write_back
    )
