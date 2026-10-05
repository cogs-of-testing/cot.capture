"""File descriptor borrowing (design O5 to O7, D1)."""

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


class Borrow:
    """One redirection of a descriptor to a temporary file.

    Created by :func:`borrow`; given back with :meth:`give_back`, in LIFO
    order per descriptor.
    """

    def __init__(self, fd: int, owner: str) -> None:
        self.fd = fd
        self.owner = owner
        self._target: IO[bytes] = tempfile.TemporaryFile(buffering=0)
        self._saved = os.dup(fd)
        os.dup2(self._target.fileno(), fd)
        self._returned = False

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
        self._target.seek(0)
        data = self._target.read()
        self._target.close()
        return data


def uncaptured_dup(fd: int) -> int:
    """A new descriptor for what ``fd`` pointed at before any borrow of it."""
    with _lock:
        stack = _stacks.get(fd)
        return os.dup(stack[0]._saved if stack else fd)


def borrow(fd: int, owner: str) -> Borrow:
    """Redirect ``fd`` to a fresh temporary file on behalf of ``owner``."""
    if not _in_main_interpreter():
        raise BorrowError("only the main interpreter may borrow descriptors")
    with _lock:
        if fd in _protected:
            raise BorrowError(f"fd {fd} is protected and cannot be borrowed by {owner!r}")
        b = Borrow(fd, owner)
        _stacks.setdefault(fd, []).append(b)
        return b
