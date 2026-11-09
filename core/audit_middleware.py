"""Registra en la bitácora cifrada TODAS las acciones de los usuarios (operaciones que modifican datos),
con IP, navegador, usuario, fecha y hora, además de los eventos detallados que ya registra cada módulo."""
from starlette.concurrency import run_in_threadpool
from core.audit_crypto import request_agent, request_ip, request_user

MUTATING = {'POST', 'PUT', 'PATCH', 'DELETE'}


def client_ip(scope) -> str | None:
    headers = dict(scope.get('headers') or [])
    # Detrás de Cloud Run (nginx) y Caddy la IP real llega como primer valor de X-Forwarded-For.
    forwarded = headers.get(b'x-forwarded-for', b'').decode('latin-1').split(',')[0].strip()
    real = headers.get(b'x-real-ip', b'').decode('latin-1').strip()
    peer = (scope.get('client') or (None,))[0]
    ip = forwarded or real or peer
    return ip[:64] if ip else None


def token_claims(scope) -> dict:
    headers = dict(scope.get('headers') or [])
    auth = headers.get(b'authorization', b'').decode('latin-1')
    if not auth.lower().startswith('bearer '):
        return {}
    try:
        from jose import jwt
        from core.config import settings
        return jwt.decode(auth[7:], settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except Exception:
        return {}


def _store(tenant_id, user_id, method, path, status):
    from db.database import SessionLocal
    from db.models.audit import AuditLog
    with SessionLocal() as db:
        db.add(AuditLog(tenant_id=tenant_id, user_id=user_id, action=method, entity_type='HTTP',
                        entity_id=path[:200], details={'status': status}))
        db.commit()


class AuditContextMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        claims = token_claims(scope)
        try:
            user_id = int(claims['sub']) if claims.get('sub') is not None else None
        except (TypeError, ValueError):
            user_id = None
        headers = dict(scope.get('headers') or [])
        tokens = (request_ip.set(client_ip(scope)),
                  request_agent.set(headers.get(b'user-agent', b'').decode('latin-1')[:200] or None),
                  request_user.set(user_id))
        status = {'code': 500}

        async def capture(message):
            if message['type'] == 'http.response.start':
                status['code'] = message['status']
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            if scope['method'] in MUTATING and scope['path'].startswith('/api/'):
                try:
                    await run_in_threadpool(_store, claims.get('tenant_id'), user_id, scope['method'],
                                            scope['path'], status['code'])
                except Exception:
                    pass  # La bitácora nunca debe tumbar la operación del usuario.
            for var, token in zip((request_ip, request_agent, request_user), tokens):
                var.reset(token)
