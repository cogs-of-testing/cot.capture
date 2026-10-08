"""Scopes: a block of code whose standard streams are captured (design Scopes)."""

from __future__ import annotations

import codecs
import dataclasses
import io
import sys
import warnings
from types import TracebackType
from typing import Literal, TextIO

from ._annotations import Annotations, Location
from ._descriptors import Borrow, CaptureFile, borrow
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


class CaptureFiles:
    """One :class:`CaptureFile` per standard stream, for a set of scopes.

    Pass it to several scopes in turn (the phases of one test) so
    descriptor-level capture reuses two files instead of creating two per
    scope (design D8). Whoever created it closes it; it is a context manager.
    """

    def __init__(self, owner: str | None = None) -> None:
        self.owner = owner or f"files-{id(self):x}"
        self.stdout = CaptureFile(self.owner)
        self.stderr = CaptureFile(self.owner)

    def __getitem__(self, slot: str) -> CaptureFile:
        if slot == "stdout":
            return self.stdout
        if slot == "stderr":
            return self.stderr
        raise KeyError(slot)

    def close(self) -> None:
        try:
            self.stdout.close()
        finally:
            self.stderr.close()

    def __enter__(self) -> CaptureFiles:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def capture_files(owner: str | None = None) -> CaptureFiles:
    """Capture files to share between scopes; close them when done."""
    return CaptureFiles(owner)


@dataclasses.dataclass
class _Installed:
    slot: str
    proxy: StreamProxy
    replaced: object
    target: TextIO | None
    borrow: Borrow | None
    decoder: codecs.IncrementalDecoder | None = None


class Scope:
    """Captures the standard streams of the code run inside it.

    ``stdout`` and ``stderr`` take a level, ``"slot"`` or ``"fd"``, or
    ``None`` to leave the stream alone. ``stdin`` is ``False`` to leave it
    alone, ``True`` to refuse reads, or a string to serve as input.
    ``write_back`` makes the scope's proxies write back to the slot after
    the scope ended, instead of raising. ``files`` are the capture files
    descriptor-level capture appends to; without them each borrow creates
    and deletes its own.

    The proxies record where they were installed and closed: the code that
    entered and left the scope, past ``contextlib`` and cot.capture itself.
    A host that enters and leaves scopes from its own machinery, where that
    code is the host's and names nothing useful, passes ``installed`` and
    ``closed`` instead: labels saying where in its run that happens, such as
    ``"before setup"``.
    """

    def __init__(
        self,
        *,
        stdout: Level | None = "slot",
        stderr: Level | None = "slot",
        stdin: bool | str = False,
        name: str | None = None,
        write_back: bool = False,
        files: CaptureFiles | None = None,
        installed: str | None = None,
        closed: str | None = None,
    ) -> None:
        self.name = name or f"scope-{id(self):x}"
        self._levels: dict[str, Level | None] = {"stdout": stdout, "stderr": stderr}
        self._stdin = stdin
        self._write_back = write_back
        self._files = files
        self._labels = {"installed": installed, "closed": closed}
        self._installed: list[_Installed] = []
        self._captured: dict[str, str] = {}
        self.diagnostics: list[ForeignReplacement] = []

    @property
    def out(self) -> str:
        """What was captured from stdout; complete once the scope ended.

        After :meth:`take`, only what arrived after the last call.
        """
        return self._captured.get("stdout", "")

    @property
    def err(self) -> str:
        """What was captured from stderr; complete once the scope ended.

        After :meth:`take`, only what arrived after the last call.
        """
        return self._captured.get("stderr", "")

    def take(self) -> tuple[str, str]:
        """Return and drop what stdout and stderr captured so far.

        The scope stays active, so a host can split one scope into parts,
        such as the phases of a test, without replacing its proxies.
        """
        taken = {"stdout": "", "stderr": ""}
        for item in self._installed:
            if item.target is not None:
                taken[item.slot] = self._read(item, final=False)
        return taken["stdout"], taken["stderr"]

    def _read(self, item: _Installed, *, final: bool) -> str:
        target = item.target
        assert target is not None
        if item.borrow is not None:
            assert item.decoder is not None
            target.flush()
            if final:
                target.close()
                data = item.borrow.give_back()
            else:
                data = item.borrow.read_new()
            text = item.decoder.decode(data, final=final)
            # Windows line endings read as "\n", as pytest's capfd does (D11)
            return text.replace("\r\n", "\n")
        assert isinstance(target, io.StringIO)
        text = target.getvalue()
        target.seek(0)
        target.truncate()
        return text

    def proxy(self, slot: str) -> StreamProxy:
        for item in self._installed:
            if item.slot == slot:
                return item.proxy
        raise KeyError(slot)

    def _where(self, event: str) -> Location:
        label = self._labels[event]
        # depth 2: the caller of __enter__ or __exit__
        return Location.here(2) if label is None else Location.described(label)

    def __enter__(self) -> Scope:
        here = self._where("installed")
        try:
            for slot, level in self._levels.items():
                if level is not None:
                    self._install(slot, level, here)
            if self._stdin is not False:
                text = self._stdin if isinstance(self._stdin, str) else None
                self._install_proxy("stdin", None, None, None, here, stdin_text=text)
        except BaseException:
            # Undo what was installed before the refusal (a borrow of a
            # protected descriptor, a capture file already in use).
            self.__exit__(None, None, None)
            raise
        return self

    def _install(self, slot: str, level: Level, here: Location) -> None:
        replaced = getattr(sys, slot)
        encoding = getattr(replaced, "encoding", None) or "utf-8"
        errors = getattr(replaced, "errors", None) or "strict"
        target: TextIO
        if level == "fd":
            fd = _FDS[slot]
            _flush(replaced)
            b = borrow(fd, self.name, None if self._files is None else self._files[slot])
            target = io.TextIOWrapper(
                io.FileIO(fd, "w", closefd=False),
                encoding=encoding,
                errors=errors,
                write_through=True,
            )
            self._install_proxy(slot, target, b, fd, here)
            self._installed[-1].decoder = codecs.getincrementaldecoder(encoding)("replace")
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
        here = self._where("closed")
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
        if item.target is not None:
            self._captured[item.slot] = self._read(item, final=True)


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
    files: CaptureFiles | None = None,
    installed: str | None = None,
    closed: str | None = None,
) -> Scope:
    """Create a :class:`Scope`; use it as a context manager."""
    return Scope(
        stdout=stdout,
        stderr=stderr,
        stdin=stdin,
        name=name,
        write_back=write_back,
        files=files,
        installed=installed,
        closed=closed,
    )
