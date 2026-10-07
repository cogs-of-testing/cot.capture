"""File descriptor borrowing and capture files (design O5 to O7, D1, D8)."""

from __future__ import annotations

import os
import sys
import tempfile
import threading
from typing import IO

from ._errors import BorrowError

_lock = threading.Lock()
_stacks: dict[int, list[Borrow]] = {}
_protected: set[int] = set()


def protect(fd: int) -> None:
    """Declare that capture must never borrow ``fd`` (design O6)."""
    with _lock:
        _protected.add(fd)


def unprotect(fd: int) -> None:
    with _lock:
        _protected.discard(fd)


def _in_main_interpreter() -> bool:
    if sys.version_info < (3, 14):
        return True
    from concurrent import interpreters  # type: ignore[attr-defined,unused-ignore]

    return interpreters.get_current() == interpreters.get_main()


class CaptureFile:
    """A temporary file that several borrows write to in turn (design D8).

    A host keeps one per descriptor for a set of scopes, such as the phases
    of one test, so each scope does not create its own. Each borrow gets
    the bytes that arrived during it. Whoever created the file closes it.
    """

    def __init__(self, owner: str) -> None:
        self.owner = owner
        self._file: IO[bytes] | None = tempfile.TemporaryFile(buffering=0)
        #: the owner of the borrow writing to the file now, if any
        self.holder: str | None = None

    @property
    def closed(self) -> bool:
        return self._file is None

    def fileno(self) -> int:
        if self._file is None:
            raise ValueError(f"capture file of {self.owner!r} is closed")
        return self._file.fileno()

    def _take(self, holder: str) -> int:
        """Claim the file for a borrow; return the offset its bytes start at."""
        if self._file is None:
            raise BorrowError(f"capture file of {self.owner!r} is closed; {holder!r} cannot use it")
        if self.holder is not None:
            raise BorrowError(
                f"capture file of {self.owner!r} is in use by {self.holder!r}; "
                f"{holder!r} cannot use it too"
            )
        self.holder = holder
        return self._file.seek(0, os.SEEK_END)

    def _read_from(self, start: int) -> bytes:
        assert self._file is not None
        # fd 1 shares this file's offset: reading to the end leaves it where
        # the next write appends
        self._file.seek(start)
        return self._file.read()

    def _release(self, start: int) -> bytes:
        self.holder = None
        return self._read_from(start)

    def close(self) -> None:
        """Delete the file. Refused while a borrow writes to it."""
        if self.holder is not None:
            raise BorrowError(
                f"capture file of {self.owner!r} closed while {self.holder!r} still uses it"
            )
        if self._file is not None:
            self._file.close()
            self._file = None

    def __enter__(self) -> CaptureFile:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class Borrow:
    """One redirection of a descriptor to a temporary file.

    Created by :func:`borrow`; given back with :meth:`give_back`, in LIFO
    order per descriptor.
    """

    def __init__(self, fd: int, owner: str, file: CaptureFile | None) -> None:
        self.fd = fd
        self.owner = owner
        self._own_file = file is None
        self._file = file if file is not None else CaptureFile(owner)
        self._start = self._file._take(owner)
        try:
            self._saved = os.dup(fd)
            os.dup2(self._file.fileno(), fd)
        except BaseException:
            self._file._release(self._start)
            if self._own_file:
                self._file.close()
            raise
        self._returned = False
        self._cursor = self._start

    def read_new(self) -> bytes:
        """What arrived since the borrow started or since the last call.

        The borrow stays in place; :meth:`give_back` returns only what
        arrived after the last call.
        """
        data = self._file._read_from(self._cursor)
        self._cursor += len(data)
        return data

    def give_back(self) -> bytes:
        """Restore the descriptor and return what arrived on it."""
        with _lock:
            stack = _stacks[self.fd]
            if stack[-1] is not self:
                raise BorrowError(
                    f"fd {self.fd} given back by {self.owner!r} out of order: "
                    f"{stack[-1].owner!r} borrowed it later and still holds it"
                )
            stack.pop()
            os.dup2(self._saved, self.fd)
            os.close(self._saved)
            self._returned = True
        data = self._file._release(self._cursor)
        if self._own_file:
            self._file.close()
        return data


def uncaptured_dup(fd: int) -> int:
    """A new descriptor for what ``fd`` pointed at before any borrow of it."""
    with _lock:
        stack = _stacks.get(fd)
        return os.dup(stack[0]._saved if stack else fd)


def borrow(fd: int, owner: str, file: CaptureFile | None = None) -> Borrow:
    """Redirect ``fd`` to a temporary file on behalf of ``owner``.

    With ``file``, the borrow appends to that capture file and leaves it open
    when given back; otherwise it creates its own and deletes it.
    """
    if not _in_main_interpreter():
        raise BorrowError("only the main interpreter may borrow descriptors")
    with _lock:
        if fd in _protected:
            raise BorrowError(f"fd {fd} is protected and cannot be borrowed by {owner!r}")
        b = Borrow(fd, owner, file)
        _stacks.setdefault(fd, []).append(b)
        return b
