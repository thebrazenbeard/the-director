"""Opt-in broker-owned queue. Call this API only from the trusted broker process.

Peer UIDs passed to handle() must come from the OS connector, never request data.
Delivery and worker claims are not evidence of an effect. No execution API exists.
"""
import hashlib
import json
import os
import secrets
from pathlib import Path
import sqlite3
import stat
import time

MAX_BYTES = 65536


class BrokerError(ValueError):
    pass


def encode(value):
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if depth > 16 or nodes > 8192:
            raise BrokerError("JSON complexity limit")
        if isinstance(item, dict):
            if len(item) > 8192 or not all(isinstance(key, str) for key in item):
                raise BrokerError("invalid JSON object")
            pending.extend((child, depth+1) for child in item.values())
        elif isinstance(item, list):
            if len(item) > 8192:
                raise BrokerError("JSON complexity limit")
            pending.extend((child, depth+1) for child in item)
    try:
        data = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError, RecursionError) as exc:
        raise BrokerError("invalid JSON") from exc
    if len(data) > MAX_BYTES:
        raise BrokerError("oversized JSON")
    return data


def identifier(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or not all(c.isalnum() or c in "_-" for c in value):
        raise BrokerError("invalid identifier")
    return value


def protected_directory(path, private=False):
    path = Path(path).absolute()
    if path.resolve() != path:
        raise BrokerError("path alias")
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & (0o077 if private else 0o022):
        raise BrokerError("unprotected directory")
    # Writable ancestors permit replacement of otherwise protected directories.
    for ancestor in path.parents:
        info = ancestor.lstat()
        if info.st_uid not in (0, os.geteuid()) or (info.st_mode & 0o022 and not (info.st_mode & stat.S_ISVTX and info.st_uid == 0)):
            raise BrokerError("unprotected ancestor")
    return path


class Broker:
    """One connection; serialize access in a foreground server. Never share with workers."""

    def __init__(self, database):
        if os.name != "posix":
            raise BrokerError("unsupported platform")
        path = Path(database)
        parent = protected_directory(path.parent, private=True)
        path = parent / path.name
        if path.exists() or path.is_symlink():
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1:
                raise BrokerError("unprotected database")
        else:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd)
        for suffix in ("-journal", "-wal", "-shm"):
            sidecar = Path(str(path) + suffix)
            if sidecar.exists() or sidecar.is_symlink():
                info = sidecar.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077 or info.st_nlink != 1:
                    raise BrokerError("unprotected database sidecar")
        # Lock waits are separately bounded; fsync/OS storage latency is not a
        # hard wall-clock limit and is not covered by the socket I/O deadline.
        self.db = sqlite3.connect(path, isolation_level=None, timeout=0.25)
        self.db.row_factory = sqlite3.Row
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(commands)")}
        if columns and "delivery_token" not in columns:
            self.db.close()
            raise BrokerError("incompatible broker schema; explicit operator migration required")
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS policy (singleton INTEGER PRIMARY KEY CHECK(singleton=1), epoch INTEGER NOT NULL);
            INSERT OR IGNORE INTO policy VALUES (1,1);
            CREATE TABLE IF NOT EXISTS workers (id TEXT PRIMARY KEY, uid INTEGER UNIQUE NOT NULL, enabled INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS commands (id TEXT PRIMARY KEY, worker TEXT NOT NULL, nonce TEXT UNIQUE NOT NULL,
                payload TEXT NOT NULL, digest TEXT NOT NULL, epoch INTEGER NOT NULL, status TEXT NOT NULL,
                lease_until REAL, claim TEXT, delivery_token TEXT);
            CREATE TABLE IF NOT EXISTS replay (worker TEXT NOT NULL, request TEXT NOT NULL, digest TEXT NOT NULL,
                response TEXT NOT NULL, PRIMARY KEY(worker,request));
        """)

    def close(self):
        self.db.close()

    @property
    def epoch(self):
        return self.db.execute("SELECT epoch FROM policy").fetchone()[0]

    def _transaction(self, operation):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            result = operation()
            self.db.execute("COMMIT")
            return result
        except BaseException:
            if self.db.in_transaction:
                try:
                    self.db.execute("ROLLBACK")
                except sqlite3.DatabaseError:
                    # Cleanup must not replace the original failure.
                    pass
            raise

    def register(self, worker, uid):
        identifier(worker)
        if type(uid) is not int or uid <= 0 or uid in (os.getuid(), os.geteuid()):
            raise BrokerError("worker requires distinct pre-provisioned UID")
        def op():
            if self.db.execute("SELECT count(*) FROM workers").fetchone()[0] >= 1024:
                raise BrokerError("identity capacity exhausted")
            try:
                self.db.execute("INSERT INTO workers VALUES (?,?,1)", (worker, uid))
            except sqlite3.IntegrityError as exc:
                raise BrokerError("duplicate identity") from exc
        self._transaction(op)

    def revoke(self, worker):
        identifier(worker)
        def op():
            if not self.db.execute("SELECT 1 FROM workers WHERE id=?", (worker,)).fetchone():
                raise BrokerError("unknown identity")
            self.db.execute("UPDATE workers SET enabled=0 WHERE id=?", (worker,))
            self._fence()
        self._transaction(op)

    def _fence(self):
        self.db.execute("UPDATE policy SET epoch=epoch+1")
        self.db.execute("UPDATE commands SET status='REVOKED' WHERE status IN ('QUEUED','DELIVERED','RECEIVED')")

    def cancel(self, command):
        identifier(command)
        def op():
            if not self.db.execute("SELECT 1 FROM commands WHERE id=?", (command,)).fetchone():
                raise BrokerError("unknown command")
            # Conservative global fence: all outstanding leases become invalid.
            self._fence()
        self._transaction(op)

    def admit(self, worker, command, nonce, payload, epoch):
        for value in (worker, command, nonce):
            identifier(value)
        body = encode(payload).decode()
        if len(body.encode()) > MAX_BYTES - 8192:
            raise BrokerError("payload too large")
        digest = hashlib.sha256(body.encode()).hexdigest()
        # Validate the whole wire envelope, not just the inner payload: both
        # depth and node budgets include metadata and enclosing objects.
        encode({"version": 1, "epoch": epoch, "command": {
            "id": command, "nonce": nonce, "digest": digest, "payload": payload,
            "lease_until": 999999999999999.0, "delivery_token": "f" * 64}})
        def op():
            row = self.db.execute("SELECT enabled FROM workers WHERE id=?", (worker,)).fetchone()
            if not row or not row[0] or type(epoch) is not int or epoch != self.epoch:
                raise BrokerError("identity or policy denied")
            old = self.db.execute("SELECT * FROM commands WHERE id=? OR nonce=?", (command, nonce)).fetchall()
            if old:
                if len(old) == 1 and all(old[0][key] == value for key, value in (("id",command),("nonce",nonce),("worker",worker),("payload",body),("epoch",epoch))):
                    return digest
                raise BrokerError("command replay conflict")
            if self.db.execute("SELECT count(*) FROM commands").fetchone()[0] >= 10000 or self.db.execute("SELECT count(*) FROM commands WHERE worker=?", (worker,)).fetchone()[0] >= 1000:
                raise BrokerError("command capacity exhausted")
            self.db.execute("INSERT INTO commands VALUES (?,?,?,?,?,?,'QUEUED',NULL,NULL,NULL)", (command,worker,nonce,body,digest,epoch))
            return digest
        return self._transaction(op)

    def handle(self, peer_uid, request):
        encode(request)
        if not isinstance(request, dict):
            raise BrokerError("request must be object")
        operation = request.get("op")
        fields = {"version", "op", "request_id", "epoch"}
        if operation in ("ACK_RECEIPT", "REPORT_CLAIM", "GET_RECEIPT"):
            fields.add("command_id")
            identifier(request.get("command_id"))
        if operation == "REPORT_CLAIM":
            fields.add("claim")
            if request.get("claim") not in ("SUCCEEDED", "FAILED", "UNKNOWN"):
                raise BrokerError("invalid claim")
        if operation in ("ACK_RECEIPT", "REPORT_CLAIM"):
            fields.add("delivery_token")
            token = request.get("delivery_token")
            if not isinstance(token, str) or len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
                raise BrokerError("invalid delivery token")
        if operation not in ("POLL", "ACK_RECEIPT", "REPORT_CLAIM", "GET_RECEIPT") or set(request) != fields or type(request.get("version")) is not int or request["version"] != 1:
            raise BrokerError("invalid request schema")
        identifier(request["request_id"])
        digest = hashlib.sha256(encode(request)).hexdigest()
        def op():
            if type(peer_uid) is not int or peer_uid in (0, os.getuid(), os.geteuid()):
                raise BrokerError("invalid peer")
            worker = self.db.execute("SELECT * FROM workers WHERE uid=?", (peer_uid,)).fetchone()
            if not worker or not worker["enabled"] or type(request["epoch"]) is not int or request["epoch"] != self.epoch:
                raise BrokerError("identity or policy denied")
            if operation in ("ACK_RECEIPT", "REPORT_CLAIM"):
                delivery = self.db.execute("SELECT * FROM commands WHERE id=? AND worker=?", (request["command_id"],worker["id"])).fetchone()
                if not delivery or delivery["epoch"] != self.epoch or delivery["delivery_token"] != request["delivery_token"]:
                    raise BrokerError("stale or unauthorized delivery token")
            prior = self.db.execute("SELECT * FROM replay WHERE worker=? AND request=?", (worker["id"],request["request_id"])).fetchone()
            if prior:
                if prior["digest"] != digest:
                    raise BrokerError("request replay conflict")
                response = json.loads(prior["response"])
                if operation == "POLL" and response["command"] and response["command"]["lease_until"] < time.time():
                    raise BrokerError("cached delivery expired; use new request identity")
                return response
            if self.db.execute("SELECT count(*) FROM replay").fetchone()[0] >= 50000 or self.db.execute("SELECT count(*) FROM replay WHERE worker=?", (worker["id"],)).fetchone()[0] >= 5000:
                raise BrokerError("replay capacity exhausted")
            response = {"version":1, "epoch":self.epoch}
            if operation == "POLL":
                row = self.db.execute("SELECT * FROM commands WHERE worker=? AND epoch=? AND (status='QUEUED' OR (status='DELIVERED' AND lease_until<?)) ORDER BY rowid LIMIT 1", (worker["id"],self.epoch,time.time())).fetchone()
                response["command"] = None
                if row:
                    lease = time.time() + 30
                    token = secrets.token_hex(32)
                    self.db.execute("UPDATE commands SET status='DELIVERED', lease_until=?, delivery_token=? WHERE id=?", (lease,token,row["id"]))
                    response["command"] = {"id":row["id"],"nonce":row["nonce"],"digest":row["digest"],"payload":json.loads(row["payload"]),"lease_until":lease,"delivery_token":token}
            else:
                row = self.db.execute("SELECT * FROM commands WHERE id=? AND worker=?", (request["command_id"],worker["id"])).fetchone()
                if not row:
                    raise BrokerError("unknown command")
                if operation != "GET_RECEIPT":
                    # Receipt is durable. After ACK, this generation may report
                    # an unverified claim even after restart/lease expiry. Only
                    # unacknowledged deliveries expire and can be redelivered.
                    if row["epoch"] != self.epoch or row["status"] not in ("DELIVERED","RECEIVED") or (row["status"] == "DELIVERED" and row["lease_until"] < time.time()):
                        raise BrokerError("invalid or expired delivery")
                    if operation == "ACK_RECEIPT":
                        self.db.execute("UPDATE commands SET status='RECEIVED' WHERE id=?", (row["id"],))
                    else:
                        if row["status"] != "RECEIVED":
                            raise BrokerError("receipt required")
                        self.db.execute("UPDATE commands SET status='CLAIM_REPORTED', claim=? WHERE id=?", (request["claim"],row["id"]))
                row = self.db.execute("SELECT * FROM commands WHERE id=?", (row["id"],)).fetchone()
                response.update(status=row["status"], claim=row["claim"], effect_verified=False)
            self.db.execute("INSERT INTO replay VALUES (?,?,?,?)", (worker["id"],request["request_id"],digest,encode(response).decode()))
            return response
        return self._transaction(op)
