"""Product promotion schedule."""
from alembic import op
import sqlalchemy as sa
revision = 'f20261030_promotions'
down_revision = 'f20261030_incoming'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('products') as batch:
        batch.add_column(sa.Column('promotion_percent', sa.Float(), nullable=True))
        batch.add_column(sa.Column('promotion_start', sa.DateTime(timezone=True), nullable=True))
        batch.add_column(sa.Column('promotion_end', sa.DateTime(timezone=True), nullable=True))


def downgrade():
    with op.batch_alter_table('products') as batch:
        for name in ('promotion_end', 'promotion_start', 'promotion_percent'):
            batch.drop_column(name)
