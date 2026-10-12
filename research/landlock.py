"""
Landlock: the kernel's own wall around a replayed Python strategy.

A Python audit hook sees what Python does. It does not see a write made by
compiled code (pyarrow writing a parquet file, DuckDB opening a database), so
on its own it cannot promise that a backtested script leaves the live
installation's files alone. Landlock can: it is a Linux security module any
unprivileged process may use to restrict itself, from then on and for every
thread and child, whatever language does the asking. Docker's default seccomp
profile allows it.

``restrict`` lets the process write only beneath the paths it is given, and,
where the kernel supports it (Landlock ABI 4, Linux 6.7), connect over TCP only
to one port. Reading stays allowed everywhere: a script still imports its
libraries and reads its own data. A kernel without Landlock answers 0, and the
replay runs behind the audit hook alone; the run says which wall it had.

The syscall numbers are the same on every architecture Linux supports Landlock
on (they were added after the syscall tables were unified).
"""

from __future__ import annotations

import ctypes
import os

_CREATE_RULESET, _ADD_RULE, _RESTRICT_SELF = 444, 445, 446
_CREATE_RULESET_VERSION = 1
_PR_SET_NO_NEW_PRIVS = 38

# Filesystem rights (uapi/linux/landlock.h), with the ABI that introduced each.
WRITE_FILE = 1 << 1
REMOVE_DIR = 1 << 4
REMOVE_FILE = 1 << 5
MAKE_CHAR = 1 << 6
MAKE_DIR = 1 << 7
MAKE_REG = 1 << 8
MAKE_SOCK = 1 << 9
MAKE_FIFO = 1 << 10
MAKE_BLOCK = 1 << 11
MAKE_SYM = 1 << 12
REFER = 1 << 13  # ABI 2
TRUNCATE = 1 << 14  # ABI 3
# Network rights (ABI 4).
BIND_TCP = 1 << 0
CONNECT_TCP = 1 << 1

_RULE_PATH_BENEATH = 1
_RULE_NET_PORT = 2


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64), ("handled_access_net", ctypes.c_uint64)]


class _PathBeneath(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


class _NetPort(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64), ("port", ctypes.c_uint64)]


def abi() -> int:
    """The kernel's Landlock ABI version, or 0 when there is none to use."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        version = libc.syscall(_CREATE_RULESET, None, ctypes.c_size_t(0), _CREATE_RULESET_VERSION)
    except (OSError, AttributeError):
        return 0
    return max(int(version), 0)


def restrict(writable: list[str], port: int | None = None) -> int:
    """Allow this process to write only beneath ``writable``, and to connect only to ``port``.

    Args:
        writable: Directories (everything beneath them) or single files.
        port: The one TCP port it may connect to; enforced from ABI 4.

    Returns:
        The ABI in force, or 0 if the kernel offers no Landlock and nothing
        was restricted.

    Raises:
        OSError: The kernel has Landlock but refused the restriction.
    """
    version = abi()
    if version < 1:
        return 0
    libc = ctypes.CDLL(None, use_errno=True)
    dir_rights = (
        WRITE_FILE
        | REMOVE_DIR
        | REMOVE_FILE
        | MAKE_CHAR
        | MAKE_DIR
        | MAKE_REG
        | MAKE_SOCK
        | MAKE_FIFO
        | MAKE_BLOCK
        | MAKE_SYM
    )
    file_rights = WRITE_FILE
    if version >= 2:
        dir_rights |= REFER
    if version >= 3:
        dir_rights |= TRUNCATE
        file_rights |= TRUNCATE
    net = BIND_TCP | CONNECT_TCP if version >= 4 and port else 0
    attr = _RulesetAttr(dir_rights, net)
    ruleset = libc.syscall(_CREATE_RULESET, ctypes.byref(attr), ctypes.sizeof(attr), 0)
    if ruleset < 0:
        raise OSError(ctypes.get_errno(), "Landlock refused the ruleset")
    try:
        for path in writable:
            if not os.path.exists(path):
                continue
            fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                rights = dir_rights if os.path.isdir(path) else file_rights
                rule = _PathBeneath(rights, fd)
                if libc.syscall(_ADD_RULE, ruleset, _RULE_PATH_BENEATH, ctypes.byref(rule), 0):
                    raise OSError(ctypes.get_errno(), f"Landlock refused a rule for {path}")
            finally:
                os.close(fd)
        if net:
            rule = _NetPort(CONNECT_TCP, port)
            if libc.syscall(_ADD_RULE, ruleset, _RULE_NET_PORT, ctypes.byref(rule), 0):
                raise OSError(ctypes.get_errno(), "Landlock refused the port rule")
        if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0):
            raise OSError(ctypes.get_errno(), "no_new_privs could not be set")
        if libc.syscall(_RESTRICT_SELF, ruleset, 0):
            raise OSError(ctypes.get_errno(), "Landlock could not restrict this process")
    finally:
        os.close(ruleset)
    return version
