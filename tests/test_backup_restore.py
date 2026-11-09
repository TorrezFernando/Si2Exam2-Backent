"""Backup / Restore de todo el sistema: la restauración devuelve exactamente los datos respaldados."""
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from db.database import Base
import db.models  # noqa: F401
from db.models import Tenant, Branch
from core import backup


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///:memory:', connect_args={'check_same_thread': False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text('CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)'))
        conn.execute(text("INSERT INTO alembic_version VALUES ('rev-actual')"))
    monkeypatch.setattr(backup.settings, 'BACKUP_DIR', str(tmp_path / 'backups'))
    uploads = tmp_path / 'uploads'
    (uploads / 'products').mkdir(parents=True)
    (uploads / 'products' / 'camisa.png').write_bytes(b'imagen-original')
    monkeypatch.setattr(backup, 'uploads_dir', lambda: uploads)
    with Session(engine) as db:
        tenant = Tenant(name='Empresa A', subdomain='empresa-a'); db.add(tenant); db.flush()
        db.add(Branch(name='Central', address='Calle 1', tenant_id=tenant.id)); db.commit()
    return engine, uploads


def names(engine):
    with Session(engine) as db:
        return sorted(b.name for b in db.query(Branch).all())


def test_restore_returns_exact_backup_state_and_images(env):
    engine, uploads = env
    info = backup.create_backup(engine, include_uploads=True)
    assert info['includes_uploads'] and info['rows'] >= 2
    # Cambios posteriores a la copia: se borra una sucursal, se crea otra y se pierde la imagen
    with Session(engine) as db:
        db.query(Branch).delete(); db.add(Branch(name='Nueva', address='x', tenant_id=1)); db.commit()
    (uploads / 'products' / 'camisa.png').unlink()
    result = backup.restore_backup(engine, backup.safe_name(info['name']))
    assert names(engine) == ['Central']
    assert (uploads / 'products' / 'camisa.png').read_bytes() == b'imagen-original'
    assert result['images_restored'] == 1 and result['safety_backup'].startswith('fashionstore-antes-de-restaurar')
    # La copia automática previa conserva el estado que se reemplazó
    backup.restore_backup(engine, backup.safe_name(result['safety_backup']))
    assert names(engine) == ['Nueva']


def test_rejects_damaged_or_other_version(env):
    engine, _ = env
    info = backup.create_backup(engine, include_uploads=False)
    with engine.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num='rev-nueva'"))
    with pytest.raises(HTTPException) as error:
        backup.restore_backup(engine, backup.safe_name(info['name']))
    assert error.value.status_code == 409 and names(engine) == ['Central']
    damaged = backup.backup_dir() / 'fashionstore-danada-20261109.fsbak'
    damaged.write_bytes(b'no es un tar')
    with pytest.raises(HTTPException):
        backup.restore_backup(engine, damaged)
    with pytest.raises(HTTPException):
        backup.safe_name('../../etc/passwd')
