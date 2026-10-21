from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from core.config import settings
from db.database import Base


def test_upgrade_legacy_schema_preserves_user(tmp_path, monkeypatch):
    url = f'sqlite:///{(tmp_path / "migration.db").as_posix()}'
    monkeypatch.setattr(settings, 'DATABASE_URL', url)
    config = Config('alembic.ini')
    command.upgrade(config, '8dee39f2ad9c')
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO users (email, hashed_password, role, is_active) VALUES ('legacy@example.com', 'hash', 'cliente', 1)"))
    command.upgrade(config, 'head')
    inspector = inspect(engine)
    for name, table in Base.metadata.tables.items():
        assert name in inspector.get_table_names()
        assert set(table.columns.keys()) <= {c['name'] for c in inspector.get_columns(name)}, name
    with engine.connect() as connection:
        user = connection.execute(text('SELECT email, role_id FROM users')).one()
        assert user.email == 'legacy@example.com'
        assert user.role_id is not None
