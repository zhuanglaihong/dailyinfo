"""Small flock-compatible exclusive lock for POSIX and Windows hosts."""

from __future__ import annotations

try:
    import fcntl as fcntl
except ImportError:
    import errno
    import msvcrt
    import os

    class _WindowsFcntl:
        LOCK_EX = 2
        LOCK_NB = 4
        LOCK_UN = 8

        @staticmethod
        def flock(fd: int, operation: int):
            position = os.lseek(fd, 0, os.SEEK_CUR)
            try:
                if os.fstat(fd).st_size == 0:
                    os.write(fd, b"\0")
                os.lseek(fd, 0, os.SEEK_SET)
                mode = (
                    msvcrt.LK_UNLCK
                    if operation == _WindowsFcntl.LOCK_UN
                    else msvcrt.LK_NBLCK
                )
                try:
                    msvcrt.locking(fd, mode, 1)
                except OSError as exc:
                    if operation != _WindowsFcntl.LOCK_UN and exc.errno in (
                        errno.EACCES,
                        errno.EAGAIN,
                        errno.EDEADLK,
                    ):
                        raise BlockingIOError(
                            errno.EAGAIN, "publication lock is busy"
                        ) from exc
                    raise
            finally:
                os.lseek(fd, position, os.SEEK_SET)

    fcntl = _WindowsFcntl()
