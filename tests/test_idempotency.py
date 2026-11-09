"""Offline outbox replays: the same Idempotency-Key never runs a mutation twice."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool
from db.database import Base
import core.idempotency as idem
import db.models.idempotency  # noqa: F401  registers the table


@pytest.fixture
def api(monkeypatch):
    engine = create_engine('sqlite:///:memory:', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    monkeypatch.setattr(idem, '_session', sessionmaker(bind=engine))
    app = FastAPI()
    calls = []

    @app.post('/items')
    def create(payload: dict):
        calls.append(payload)
        return {'id': len(calls), **payload}

    @app.post('/reject')
    def reject():
        from fastapi import HTTPException
        calls.append('reject')
        raise HTTPException(409, 'Límite del plan')

    @app.post('/boom')
    def boom():
        calls.append('boom')
        raise RuntimeError('fail')

    app.add_middleware(idem.IdempotencyMiddleware)
    return TestClient(app, raise_server_exceptions=False), calls


def test_same_key_replays_stored_response(api):
    client, calls = api
    headers = {'Idempotency-Key': 'offline-0001-abc', 'Authorization': 'Bearer a'}
    first = client.post('/items', json={'name': 'Sucursal'}, headers=headers)
    second = client.post('/items', json={'name': 'Sucursal'}, headers=headers)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {'id': 1, 'name': 'Sucursal'}
    assert second.headers.get('idempotent-replay') == 'true'
    assert len(calls) == 1
    # Sin clave cada solicitud se ejecuta
    client.post('/items', json={'name': 'Otra'})
    assert len(calls) == 2


def test_key_is_scoped_to_caller_and_path(api):
    client, calls = api
    client.post('/items', json={}, headers={'Idempotency-Key': 'offline-0002-abc', 'Authorization': 'Bearer a'})
    other_user = client.post('/items', json={}, headers={'Idempotency-Key': 'offline-0002-abc', 'Authorization': 'Bearer b'})
    assert other_user.status_code == 422 and len(calls) == 1
    assert client.post('/items', json={}, headers={'Idempotency-Key': 'bad key!'}).status_code == 422


def test_server_errors_are_not_stored_and_can_retry(api):
    client, calls = api
    headers = {'Idempotency-Key': 'offline-0003-abc'}
    assert client.post('/boom', headers=headers).status_code == 500
    assert client.post('/boom', headers=headers).status_code == 500
    assert calls == ['boom', 'boom']


def test_rejections_are_not_stored_so_a_fixed_cause_can_retry(api):
    client, calls = api
    headers = {'Idempotency-Key': 'offline-0004-abc'}
    assert client.post('/reject', headers=headers).status_code == 409
    assert client.post('/reject', headers=headers).status_code == 409
    assert calls == ['reject', 'reject']
