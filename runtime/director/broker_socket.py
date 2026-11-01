"""Linux-only foreground IPC. No listener is created on import.

Socket parent must be provisioned by an operator; workers need traversal, not
write access. Provision a dedicated worker-access group and a broker-owned
setgid socket directory (e.g. 02710) so the 0660 socket inherits that group.
Never give this group access to the separate private database directory. Socket
availability does not constitute HOST_QUALIFIED deployment evidence.
"""
import json
import os
from pathlib import Path
import socket
import sqlite3
import struct
import sys
import time

from .broker import BrokerError, MAX_BYTES, encode, protected_directory


def require_linux():
    if sys.platform != "linux" or not hasattr(socket, "SO_PEERCRED"):
        raise BrokerError("credential-bound transport unsupported")


def _exact(connection, count, deadline):
    result = bytearray()
    while len(result) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BrokerError("connection deadline")
        connection.settimeout(remaining)
        part = connection.recv(count - len(result))
        if not part:
            raise BrokerError("truncated frame")
        result.extend(part)
    return bytes(result)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise BrokerError("duplicate JSON key")
        result[key] = value
    return result


def read_request(connection, deadline):
    size = struct.unpack("!I", _exact(connection, 4, deadline))[0]
    if not 0 < size <= MAX_BYTES:
        raise BrokerError("invalid frame size")
    try:
        result = json.loads(_exact(connection, size, deadline).decode("utf-8"), object_pairs_hook=_pairs,
                            parse_constant=lambda value: (_ for _ in ()).throw(BrokerError("nonfinite JSON")))
        encode(result)  # Enforce finite values and structural depth/node bounds.
        return result
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise BrokerError("invalid JSON frame") from exc


def serve_connection(broker, connection):
    """One request, two-second socket I/O deadline; not a storage-time bound.

    SQLite lock waits have a separate 250ms timeout; OS/fsync latency is not
    hard-bounded. Identity is obtained before parsing routing data.
    """
    require_linux()
    deadline = time.monotonic() + 2
    try:
        _, uid, _ = struct.unpack("3i", connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")))
        response = broker.handle(uid, read_request(connection, deadline))
    except (BrokerError, OSError, ValueError, sqlite3.DatabaseError):
        response = {"version": 1, "error": "DENIED"}
    data = encode(response)
    remaining = deadline - time.monotonic()
    if remaining > 0:
        connection.settimeout(remaining)
        connection.sendall(struct.pack("!I", len(data)) + data)


def serve_foreground(broker, socket_path, stop, max_connections=None):
    """Explicit foreground service until trusted stop; no client-count default.

    Optional max_connections is only a finite test/operator invocation budget,
    NOT unattended service configuration: clients can exhaust that budget.
    Sequential service remains susceptible to group-member availability abuse.
    """
    require_linux()
    if max_connections is not None and (type(max_connections) is not int or not 1 <= max_connections <= 100000):
        raise BrokerError("invalid connection budget")
    path = Path(socket_path)
    parent = protected_directory(path.parent)
    path = parent / path.name
    if path.exists() or path.is_symlink():
        raise BrokerError("socket path already exists; operator cleanup required")
    identity = None
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
        try:
            listener.bind(str(path))
            identity = path.lstat()
            os.chmod(path, 0o660)  # Group access is additional to kernel UID authentication.
            listener.listen(8)
            listener.settimeout(0.2)
            accepted = 0
            while not stop.is_set() and (max_connections is None or accepted < max_connections):
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                accepted += 1
                with connection:
                    try:
                        serve_connection(broker, connection)
                    except OSError:
                        pass
        finally:
            if identity is not None:
                try:
                    current = path.lstat()
                    if (current.st_dev, current.st_ino) == (identity.st_dev, identity.st_ino):
                        path.unlink()
                except FileNotFoundError:
                    pass
