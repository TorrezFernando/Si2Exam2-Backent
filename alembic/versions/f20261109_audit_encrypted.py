"""Encrypted audit log: payload column, and existing plaintext rows sealed with the developer key."""
from alembic import op
import sqlalchemy as sa

revision = 'f20261109_audit_encrypted'
down_revision = 'f20261109_idempotency'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if 'audit_logs' not in inspector.get_table_names():
        return
    if 'payload' not in {c['name'] for c in inspector.get_columns('audit_logs')}:
        with op.batch_alter_table('audit_logs') as batch:
            batch.add_column(sa.Column('payload', sa.Text(), nullable=True))
    # Cifrar también los registros anteriores para que nadie los lea desde la base de datos.
    from core.audit_crypto import encrypt, SEALED
    logs = sa.table('audit_logs', sa.column('id', sa.Integer), sa.column('user_id', sa.Integer),
                    sa.column('action', sa.String), sa.column('entity_type', sa.String),
                    sa.column('entity_id', sa.String), sa.column('details', sa.JSON),
                    sa.column('created_at', sa.DateTime), sa.column('payload', sa.Text))
    rows = bind.execute(sa.select(logs).where(logs.c.payload.is_(None))).mappings().all()
    for row in rows:
        token = encrypt(dict(user_id=row['user_id'], action=row['action'], entity_type=row['entity_type'],
                             entity_id=row['entity_id'], details=row['details'], ip=None, user_agent=None,
                             at=str(row['created_at'])))
        bind.execute(logs.update().where(logs.c.id == row['id']).values(
            payload=token, user_id=None, action=SEALED, entity_type=SEALED, entity_id=None, details=None))


def downgrade():
    # Los registros cifrados no se descifran al bajar de versión: la llave no debe salir del sistema.
    inspector = sa.inspect(op.get_bind())
    if 'audit_logs' in inspector.get_table_names() and 'payload' in {c['name'] for c in inspector.get_columns('audit_logs')}:
        with op.batch_alter_table('audit_logs') as batch:
            batch.drop_column('payload')
