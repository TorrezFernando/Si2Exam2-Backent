"""Exercise actual Alembic history, rather than metadata.create_all()."""
import os
from uuid import uuid4
import pytest
from sqlalchemy import create_engine,text,inspect
from sqlalchemy.engine import make_url
from alembic import command
from alembic.config import Config
from core.config import settings
from db.database import Base


def test_postgres_full_migration_history_preserves_legacy_user(monkeypatch):
    url=os.environ.get('PHASE6_POSTGRES_TEST_URL')
    if not url:pytest.skip('Disposable PostgreSQL test URL not configured.')
    parsed=make_url(url)
    assert parsed.get_backend_name()=='postgresql' and (parsed.database or '').endswith('_test')
    schema='migration_'+uuid4().hex
    admin=create_engine(url)
    with admin.begin() as conn:conn.execute(text(f'CREATE SCHEMA {schema}'))
    isolated=parsed.update_query_dict({'options':f'-csearch_path={schema}'})
    monkeypatch.setattr(settings,'DATABASE_URL',isolated.render_as_string(hide_password=False))
    engine=create_engine(isolated)
    try:
        config=Config('alembic.ini')
        command.upgrade(config,'8dee39f2ad9c')
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO users (email,hashed_password,role,is_active) VALUES ('legacy@example.invalid','unused','cliente',true)"))
        command.upgrade(config,'head')
        command.upgrade(config,'head')
        inspector=inspect(engine)
        for name,table in Base.metadata.tables.items():
            assert name in inspector.get_table_names()
            assert set(table.columns.keys()) <= {c['name'] for c in inspector.get_columns(name)},name
        with engine.connect() as conn:
            row=conn.execute(text('SELECT email,role_id FROM users')).one()
            assert row.email=='legacy@example.invalid' and row.role_id is not None
        assert any(fk['referred_table']=='users' for fk in inspector.get_foreign_keys('orders'))
        assert any(fk['referred_table']=='users' for fk in inspector.get_foreign_keys('reservations'))
    finally:
        engine.dispose()
        with admin.begin() as conn:conn.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        admin.dispose()
