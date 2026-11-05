"""Add phone and branch_id to users

Revision ID: ca3f58319abb
Revises: 303d27b2b21a
Create Date: 2026-09-01 14:38:42.293549

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'ca3f58319abb'
down_revision: Union[str, Sequence[str], None] = '303d27b2b21a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """
    Agrega columnas phone y branch_id a la tabla users.
    Se usan dos operaciones separadas para evitar el bug de circular dependency
    en SQLite batch mode al agregar FK al mismo tiempo que columnas.
    """
    # Paso 1: Agregar columnas nuevas directamente (sin FK aun)
    op.add_column('users', sa.Column('phone', sa.String(), nullable=True))
    op.add_column('users', sa.Column('branch_id', sa.Integer(), nullable=True))

    # SQLite recreates only when required; PostgreSQL adds the FK in place.
    # Never force recreation: sales and reservations reference users.
    with op.batch_alter_table('users', recreate='auto') as batch_op:
        batch_op.create_foreign_key('fk_users_branch_id', 'branches', ['branch_id'], ['id'])


def downgrade() -> None:
    """Revertir cambios."""
    with op.batch_alter_table('users', recreate='auto') as batch_op:
        batch_op.drop_constraint('fk_users_branch_id', type_='foreignkey')
        batch_op.drop_column('branch_id')
        batch_op.drop_column('phone')
