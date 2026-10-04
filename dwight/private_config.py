"""Prepare the operator's local key entry file without accepting secrets in CLI arguments."""
from __future__ import annotations

import os
from pathlib import Path
import re
import stat

DATA_KEY_NAMES = ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY", "DATABENTO_API_KEY", "MASSIVE_API_KEY")
NEWS_CONFIG_NAMES = ("BENZINGA_RELAY_REST", "BENZINGA_RELAY_WS", "BENZINGA_RELAY_KEY")
MAX_ENV_BYTES = 65_536


def prepare_data_keys(path: Path | str = Path(".env")) -> dict:
    """Append missing private market-data key fields without exposing values."""
    return _prepare_fields(path, DATA_KEY_NAMES,
                           "Private data keys: fill locally; never paste them into chat or a public Space.")


def prepare_news_config(path: Path | str = Path(".env")) -> dict:
    """Prepare private relay addresses and its separate key; no connections."""
    return _prepare_fields(path, NEWS_CONFIG_NAMES,
                           "Private news relay: fill complete HTTPS/WSS addresses and its separate key locally.")


def _prepare_fields(path: Path | str, fields: tuple[str, ...], comment: str) -> dict:
    """Append missing empty fields, preserving existing bytes and credentials.

    No values are accepted, read into the process environment, or returned.
    The file is mode 0600 before writing. Symlinks and ambiguous duplicate
    entries are rejected, and a POSIX file lock serializes preparation.
    """
    target = Path(path).expanduser().absolute()
    if any(part.is_symlink() for part in (target, *target.parents)):
        raise ValueError("Private key file paths must not contain symbolic links")
    if not target.parent.is_dir():
        raise ValueError("Private key file parent must already exist")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    descriptor = os.open(target, flags, 0o600)
    try:
        import fcntl
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ENV_BYTES:
            raise ValueError("Private key file must be a regular file no larger than 64 KiB")
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        os.fchmod(descriptor, 0o600)
        raw = os.read(descriptor, MAX_ENV_BYTES + 1)
        if len(raw) > MAX_ENV_BYTES:
            raise ValueError("Private key file exceeds its size limit")
        try:
            content = raw.decode("utf-8")
        except UnicodeError:
            raise ValueError("Private key file must use UTF-8") from None
        names = set()
        for line in content.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, _ = line.partition("=")
            key = key.strip()
            if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                raise ValueError("Use simple KEY=value entries in the private key file")
            if key in names:
                raise ValueError("Duplicate key entries are not allowed in the private key file")
            names.add(key)
        missing = [name for name in fields if name not in names]
        if missing:
            addition = ("\n" if raw and not raw.endswith(b"\n") else "")
            addition += "# " + comment + "\n"
            addition += "".join(name + "=\n" for name in missing)
            encoded = addition.encode("utf-8")
            if len(raw) + len(encoded) > MAX_ENV_BYTES:
                raise ValueError("Private key file would exceed its size limit")
            with os.fdopen(descriptor, "ab", closefd=False) as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(descriptor)
        return {"status": "prepared", "file": str(target), "added_fields": missing,
                "values_reported": False, "network_checked": False, "submits_orders": False}
    finally:
        os.close(descriptor)
