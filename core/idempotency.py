"""ASGI middleware: a mutating request with `Idempotency-Key` runs at most once.

The offline outbox of the web client replays queued operations with a stable key.
If the first attempt reached the server but the response was lost, the retry gets
the stored response instead of creating a duplicate branch, product, sale, etc.
Records are scoped to the caller's Authorization header and to method + path.
"""
import hashlib
import json
import re
from starlette.concurrency import run_in_threadpool

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
KEY_PATTERN = re.compile(r"^[A-Za-z0-9_\-:.]{8,80}$")
MAX_STORED_BODY = 1_000_000


def _session():
    from db.database import SessionLocal
    return SessionLocal()


def _find(key):
    from db.models.idempotency import IdempotencyRecord
    with _session() as db:
        row = db.query(IdempotencyRecord).filter_by(key=key).first()
        return None if row is None else dict(auth_hash=row.auth_hash, method=row.method, path=row.path,
                                             status=row.status_code, content_type=row.content_type, body=row.body)


def _store(key, auth_hash, method, path, status, content_type, body):
    from sqlalchemy.exc import IntegrityError
    from db.models.idempotency import IdempotencyRecord
    with _session() as db:
        db.add(IdempotencyRecord(key=key, auth_hash=auth_hash, method=method, path=path[:255],
                                 status_code=status, content_type=content_type, body=body))
        try:
            db.commit()
        except IntegrityError:
            db.rollback()  # A concurrent attempt stored it first.


async def _respond(send, status, body: bytes, content_type: str | None, replayed: bool):
    headers = [(b"content-type", (content_type or "application/json").encode()),
               (b"content-length", str(len(body)).encode())]
    if replayed:
        headers.append((b"idempotent-replay", b"true"))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body})


class IdempotencyMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in MUTATING:
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers") or [])
        raw_key = headers.get(b"idempotency-key")
        if not raw_key:
            return await self.app(scope, receive, send)
        key = raw_key.decode("latin-1").strip()
        if not KEY_PATTERN.match(key):
            body = json.dumps({"detail": "Idempotency-Key inválida."}).encode()
            return await _respond(send, 422, body, "application/json", False)
        auth_hash = hashlib.sha256(headers.get(b"authorization", b"")).hexdigest()
        method, path = scope["method"], scope["path"]

        previous = await run_in_threadpool(_find, key)
        if previous:
            if (previous["auth_hash"], previous["method"], previous["path"]) != (auth_hash, method, path[:255]):
                body = json.dumps({"detail": "La clave de idempotencia ya se usó para otra operación."}).encode()
                return await _respond(send, 422, body, "application/json", False)
            return await _respond(send, previous["status"], (previous["body"] or "").encode(),
                                  previous["content_type"], True)

        captured = {"status": 500, "content_type": None, "chunks": [], "size": 0}

        async def capture(message):
            if message["type"] == "http.response.start":
                captured["status"] = message["status"]
                for name, value in message.get("headers") or []:
                    if name.lower() == b"content-type":
                        captured["content_type"] = value.decode("latin-1")
            elif message["type"] == "http.response.body" and captured["size"] <= MAX_STORED_BODY:
                chunk = message.get("body", b"")
                captured["chunks"].append(chunk)
                captured["size"] += len(chunk)
            await send(message)

        await self.app(scope, receive, capture)
        status = captured["status"]
        # Only successful outcomes are stored: a rejected request (4xx/5xx) applied nothing, so once the
        # cause is fixed (plan limit, stock, permissions, data) the client may retry it with the same key.
        if 200 <= status < 300 and captured["size"] <= MAX_STORED_BODY:
            body = b"".join(captured["chunks"]).decode("utf-8", errors="replace")
            await run_in_threadpool(_store, key, auth_hash, method, path, status, captured["content_type"], body)
