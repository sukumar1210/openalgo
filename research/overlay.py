"""
Copy-on-write for the files a replayed Python strategy writes outside its folder.

A script written for the strategy host writes where it was told to: an audit
log under a data directory named in ``.env``, a state file beside itself, a
SQLite journal. Run unchanged in a backtest, those writes must work, or the
script fails every day, and they must not land in the live installation, or
the backtest rewrites the live strategy's records.

So in a replay every write through Python's file functions to a path outside
the day's folder goes to a copy of that path under ``<folder>/outside/``: an
existing file is copied there first, so appending keeps what it had. Every
later read of that path, and a listing of its directory, sees the copy. A
deletion of a file that exists only outside is remembered, not performed. The
live file is never opened for writing. The copies and the deletions carry from
day to day with the rest of the replay's state.

What is covered: ``open``/``io.open``, ``os.open``, ``os.stat``,
``os.listdir``, ``os.scandir``, ``os.mkdir`` (and with it ``os.makedirs``),
``os.remove``/``os.unlink``, ``os.rmdir``, ``os.rename``/``os.replace``,
``os.utime``, ``os.chmod`` and ``sqlite3.connect``, which between them carry
``pathlib``, ``shutil``, ``json``, ``csv``, ``logging`` and ``pandas``' text
writers. What is not: compiled code that opens files itself (pyarrow, DuckDB)
and calls given a directory descriptor. Those reach the real path, where the
kernel's wall (``research.landlock``) refuses the write.
"""

from __future__ import annotations

import builtins
import errno
import io
import json
import os
import sqlite3
import stat as stat_module

#: Kernel and device paths a script reads and writes as itself, never copied.
PASS_THROUGH = ("/dev/", "/proc/", "/sys/")
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC


class _Listing:
    """``os.scandir``'s iterator, over the copy and the live directory together."""

    def __init__(self, entries) -> None:
        self._entries = iter(entries)

    def __iter__(self):
        return self._entries

    def __next__(self):
        return next(self._entries)

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self._entries = iter(())


class Overlay:
    """Copies of the outside paths a replay writes, kept under ``<folder>/outside``."""

    def __init__(self, folder: str) -> None:
        self.folder = os.path.realpath(folder)
        self.root = os.path.join(self.folder, "outside")
        self._deleted_file = os.path.join(self.root, ".deleted.json")
        self.real = {
            name: getattr(os, name)
            for name in (
                "open",
                "stat",
                "listdir",
                "scandir",
                "mkdir",
                "remove",
                "unlink",
                "rmdir",
                "rename",
                "replace",
                "utime",
                "chmod",
            )
        }
        self.real_open = builtins.open
        self.real_connect = sqlite3.connect
        try:
            with self.real_open(self._deleted_file, encoding="utf-8") as fh:
                self.deleted = set(json.load(fh))
        except (OSError, ValueError):
            self.deleted: set[str] = set()

    # ------------------------------------------------------------- paths

    def outside(self, path) -> bool:
        if isinstance(path, int):
            return False
        full = os.path.realpath(os.fspath(path))
        if full == self.folder or full.startswith(self.folder + os.sep):
            return False
        return not full.startswith(PASS_THROUGH)

    def _key(self, path) -> str:
        return os.path.realpath(os.fspath(path))

    def copy_of(self, path) -> str:
        return os.path.join(self.root, self._key(path).lstrip(os.sep))

    def _exists(self, path: str) -> bool:
        try:
            self.real["stat"](path)
        except OSError:
            return False
        return True

    def _isdir(self, path: str) -> bool:
        """Whether ``path`` itself is a directory, never looking through a copy."""
        try:
            return stat_module.S_ISDIR(self.real["stat"](path).st_mode)
        except OSError:
            return False

    def _live(self, path) -> bool:
        """Whether the live path is still visible: it exists and was not deleted here."""
        return self._key(path) not in self.deleted and self._exists(self._key(path))

    def _missing(self, path):
        return FileNotFoundError(errno.ENOENT, os.strerror(errno.ENOENT), os.fspath(path))

    def for_reading(self, path) -> str:
        copy = self.copy_of(path)
        if self._exists(copy):
            return copy
        if self._key(path) in self.deleted:
            raise self._missing(path)
        return os.fspath(path)

    def for_writing(self, path, keep: bool) -> str:
        """The copy to write, made from the live file first when its contents are kept."""
        copy = self.copy_of(path)
        os.makedirs(os.path.dirname(copy), exist_ok=True)
        if keep and not self._exists(copy) and self._live(path):
            live = self._key(path)
            if not self._isdir(live):
                with self.real_open(live, "rb") as src, self.real_open(copy, "wb") as dst:
                    while chunk := src.read(1 << 20):
                        dst.write(chunk)
        self.deleted.discard(self._key(path))
        return copy

    def save(self) -> None:
        if self.deleted or self._exists(self.root):
            os.makedirs(self.root, exist_ok=True)
            with self.real_open(self._deleted_file, "w", encoding="utf-8") as fh:
                json.dump(sorted(self.deleted), fh)

    # ------------------------------------------------------------- patches

    def install(self) -> None:
        """Route Python's file functions through the copies. For the rest of this process."""
        ov, real = self, self.real

        def open_(file, mode="r", *args, **kwargs):
            if not ov.outside(file):
                return ov.real_open(file, mode, *args, **kwargs)
            if any(c in mode for c in "wax+"):
                target = ov.for_writing(file, keep="w" not in mode)
            else:
                target = ov.for_reading(file)
            return ov.real_open(target, mode, *args, **kwargs)

        def os_open(path, flags, mode=0o777, *, dir_fd=None):
            if dir_fd is not None or not ov.outside(path):
                return real["open"](path, flags, mode, dir_fd=dir_fd)
            if flags & _WRITE_FLAGS:
                target = ov.for_writing(path, keep=not flags & os.O_TRUNC)
            else:
                target = ov.for_reading(path)
            return real["open"](target, flags, mode)

        def stat(path, *, dir_fd=None, follow_symlinks=True):
            if dir_fd is not None or not ov.outside(path):
                return real["stat"](path, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
            return real["stat"](ov.for_reading(path), follow_symlinks=follow_symlinks)

        def _names(path) -> tuple[list, list]:
            copy = ov.copy_of(path)
            ours = real["listdir"](copy) if ov._isdir(copy) else []
            live = []
            if ov._key(path) not in ov.deleted and ov._isdir(ov._key(path)):
                live = [
                    name
                    for name in real["listdir"](ov._key(path))
                    if os.path.join(ov._key(path), name) not in ov.deleted and name not in ours
                ]
            if not ours and not live and not stat_exists(path):
                raise ov._missing(path)
            return ours, live

        def stat_exists(path) -> bool:
            try:
                stat(path)
            except OSError:
                return False
            return True

        def listdir(path="."):
            if isinstance(path, int) or not ov.outside(path):
                return real["listdir"](path)
            ours, live = _names(path)
            return ours + live

        def scandir(path="."):
            if isinstance(path, int) or not ov.outside(path):
                return real["scandir"](path)
            ours, live = _names(path)
            entries = []
            if ours:
                entries += [e for e in real["scandir"](ov.copy_of(path)) if e.name in ours]
            if live:
                keep = set(live)
                entries += [e for e in real["scandir"](ov._key(path)) if e.name in keep]
            return _Listing(entries)

        def mkdir(path, mode=0o777, *, dir_fd=None):
            if dir_fd is not None or not ov.outside(path):
                return real["mkdir"](path, mode, dir_fd=dir_fd)
            if stat_exists(path):
                raise FileExistsError(errno.EEXIST, os.strerror(errno.EEXIST), os.fspath(path))
            copy = ov.copy_of(path)
            os.makedirs(os.path.dirname(copy), exist_ok=True)
            ov.deleted.discard(ov._key(path))
            return real["mkdir"](copy, mode)

        def _forget(path, remove) -> None:
            copy, found = ov.copy_of(path), False
            if ov._exists(copy):
                remove(copy)
                found = True
            if ov._live(path):
                ov.deleted.add(ov._key(path))
                found = True
            if not found:
                raise ov._missing(path)

        def remove(path, *, dir_fd=None):
            if dir_fd is not None or not ov.outside(path):
                return real["remove"](path, dir_fd=dir_fd)
            _forget(path, real["remove"])

        def rmdir(path, *, dir_fd=None):
            if dir_fd is not None or not ov.outside(path):
                return real["rmdir"](path, dir_fd=dir_fd)
            _forget(path, real["rmdir"])

        def _move(name):
            def move(src, dst, *, src_dir_fd=None, dst_dir_fd=None):
                fds = src_dir_fd is not None or dst_dir_fd is not None
                if fds or not (ov.outside(src) or ov.outside(dst)):
                    return real[name](src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)
                source = ov.for_writing(src, keep=True) if ov.outside(src) else src
                target = ov.for_writing(dst, keep=False) if ov.outside(dst) else dst
                was_live = ov.outside(src) and ov._live(src)
                real[name](source, target)
                if was_live:
                    ov.deleted.add(ov._key(src))

            return move

        def _touching(name):
            def touch(path, *args, **kwargs):
                if kwargs.get("dir_fd") is not None or not ov.outside(path):
                    return real[name](path, *args, **kwargs)
                return real[name](ov.for_writing(path, keep=True), *args, **kwargs)

            return touch

        def connect(database, *args, **kwargs):
            if isinstance(database, (str, bytes, os.PathLike)):
                name = os.fsdecode(database)
                if name != ":memory:" and not name.startswith("file:") and ov.outside(name):
                    database = ov.for_writing(name, keep=True)
            return ov.real_connect(database, *args, **kwargs)

        builtins.open = io.open = open_
        os.open, os.stat = os_open, stat
        os.listdir, os.scandir, os.mkdir = listdir, scandir, mkdir
        os.remove = os.unlink = remove
        os.rmdir = rmdir
        os.rename, os.replace = _move("rename"), _move("replace")
        os.utime, os.chmod = _touching("utime"), _touching("chmod")
        sqlite3.connect = connect
