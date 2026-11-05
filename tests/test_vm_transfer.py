import os
from pathlib import Path
from uuid import uuid4
import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url
from alembic import command
from alembic.config import Config
from core.config import settings
from tools.transfer_vm_data import import_bundle


def test_real_bundle_import_into_migrated_postgres(monkeypatch,tmp_path):
    url=os.environ.get('PHASE6_POSTGRES_TEST_URL')
    bundle=os.environ.get('VM_TRANSFER_BUNDLE')
    if not url or not bundle:pytest.skip('Explicit disposable PostgreSQL and private transfer bundle required.')
    parsed=make_url(url)
    assert parsed.get_backend_name()=='postgresql' and (parsed.database or '').endswith('_test')
    schema='transfer_'+uuid4().hex
    admin=sa.create_engine(url)
    with admin.begin() as conn:conn.execute(sa.text(f'CREATE SCHEMA {schema}'))
    isolated=parsed.update_query_dict({'options':f'-csearch_path={schema}'})
    monkeypatch.setattr(settings,'DATABASE_URL',isolated.render_as_string(hide_password=False))
    target=sa.create_engine(isolated)
    try:
        command.upgrade(Config('alembic.ini'),'head')
        try:
            import_bundle(Path(bundle),target,tmp_path/'uploads')
        except Exception as error:
            # Never include exported customer information or password hashes in test output.
            pytest.fail('Transfer failed: '+type(error).__name__,pytrace=False)
        with target.connect() as conn:
            assert conn.execute(sa.text('SELECT count(*) FROM tenants')).scalar()==2
            assert conn.execute(sa.text("SELECT count(*) FROM orders WHERE payment_provider='DEMO_SYNTHETIC'")).scalar()==1372
            assert conn.execute(sa.text('SELECT count(*) FROM users')).scalar()>0
        with pytest.raises(RuntimeError,match='Destino con datos'):
            import_bundle(Path(bundle),target,tmp_path/'uploads')
    finally:
        target.dispose()
        with admin.begin() as conn:conn.execute(sa.text(f'DROP SCHEMA {schema} CASCADE'))
        admin.dispose()
