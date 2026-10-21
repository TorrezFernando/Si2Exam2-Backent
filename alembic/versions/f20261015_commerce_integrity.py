"""Complete legacy schema and add transactional commerce records.

Revision ID: f20261015
Revises: 8dee39f2ad9c
"""
from alembic import op
import sqlalchemy as sa

revision = 'f20261015'
down_revision = '8dee39f2ad9c'
branch_labels = depends_on = None


def upgrade():
    bind = op.get_bind()
    tables = set(sa.inspect(bind).get_table_names())

    def create(name, *columns):
        if name not in tables:
            op.create_table(name, *columns)

    def pk():
        return sa.Column('id', sa.Integer(), primary_key=True)

    create('permissions', pk(), sa.Column('name', sa.String(), unique=True, nullable=False), sa.Column('description', sa.String()))
    create('roles', pk(), sa.Column('name', sa.String(), unique=True, nullable=False), sa.Column('description', sa.String()),
           sa.Column('is_predefined', sa.Boolean(), nullable=False, server_default=sa.false()))
    create('role_permissions', sa.Column('role_id', sa.Integer(), sa.ForeignKey('roles.id', ondelete='CASCADE'), primary_key=True),
           sa.Column('permission_id', sa.Integer(), sa.ForeignKey('permissions.id', ondelete='CASCADE'), primary_key=True))
    create('user_permissions', sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
           sa.Column('permission_id', sa.Integer(), sa.ForeignKey('permissions.id', ondelete='CASCADE'), primary_key=True),
           sa.Column('is_granted', sa.Boolean(), nullable=False, server_default=sa.true()))
    create('audit_logs', pk(), sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL')),
           sa.Column('action', sa.String(), nullable=False), sa.Column('entity_type', sa.String(), nullable=False),
           sa.Column('entity_id', sa.String()), sa.Column('details', sa.JSON()),
           sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()))
    create('user_preferences', pk(), sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False, unique=True),
           sa.Column('preferences_json', sa.Text()))
    create('suppliers', pk(), sa.Column('name', sa.String(), nullable=False, unique=True), sa.Column('contact', sa.String()),
           sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()))
    for name in ('seasons', 'collections'):
        create(name, pk(), sa.Column('name', sa.String(), nullable=False, unique=True), sa.Column('description', sa.Text()))
    create('inventory_movements', pk(),
           sa.Column('branch_id', sa.Integer(), sa.ForeignKey('branches.id'), nullable=False),
           sa.Column('variant_id', sa.Integer(), sa.ForeignKey('product_variants.id'), nullable=False),
           sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
           sa.Column('quantity', sa.Integer(), nullable=False), sa.Column('kind', sa.String(), nullable=False),
           sa.Column('reference', sa.String(), nullable=False),
           sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))

    additions = {
        'branches': [sa.Column('city', sa.String()), sa.Column('image_url', sa.String())],
        'categories': [sa.Column('image_url', sa.String())],
        'users': [sa.Column('role_id', sa.Integer())],
        'products': [sa.Column('image_url', sa.String()), sa.Column('garment_image_url', sa.String()),
            sa.Column('garment_type', sa.String()), sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column('supplier_id', sa.Integer()), sa.Column('season_id', sa.Integer()), sa.Column('collection_id', sa.Integer())],
        'product_variants': [sa.Column('image_url', sa.String())],
        'reservations': [sa.Column('pickup_date', sa.DateTime(timezone=True)), sa.Column('notes', sa.String())],
        'reservation_items': [sa.Column('unit_price', sa.Float())],
        'orders': [sa.Column('order_type', sa.String(), nullable=False, server_default='presencial'),
            sa.Column('payment_method', sa.String(), nullable=False, server_default='efectivo'),
            sa.Column('payment_status', sa.String(), nullable=False, server_default='pending'),
            sa.Column('card_type', sa.String()), sa.Column('card_last_four', sa.String()), sa.Column('transaction_ref', sa.String()),
            sa.Column('payment_url', sa.String()), sa.Column('reservation_id', sa.Integer()), sa.Column('request_key', sa.String())],
    }
    for table, columns in additions.items():
        existing = {c['name'] for c in sa.inspect(bind).get_columns(table)}
        for column in columns:
            if column.name not in existing:
                op.add_column(table, column)
                if table == 'orders' and column.name == 'payment_status':
                    bind.execute(sa.text("UPDATE orders SET payment_status = CASE WHEN is_paid THEN 'paid' ELSE 'pending' END"))

    references = [('users', 'role_id', 'roles'), ('products', 'supplier_id', 'suppliers'),
                  ('products', 'season_id', 'seasons'), ('products', 'collection_id', 'collections'),
                  ('orders', 'reservation_id', 'reservations')]
    for table, column, target in references:
        existing = sa.inspect(bind).get_foreign_keys(table)
        if not any(fk['constrained_columns'] == [column] for fk in existing):
            with op.batch_alter_table(table) as batch:
                batch.create_foreign_key(f'fk_{table}_{column}', target, [column], ['id'])

    bad = bind.execute(sa.text('SELECT branch_id, variant_id FROM inventories GROUP BY branch_id, variant_id HAVING COUNT(*) > 1')).first()
    negative = bind.execute(sa.text('SELECT id FROM inventories WHERE stock < 0')).first()
    if bad or negative:
        raise RuntimeError('Inventario duplicado o negativo: conciliar datos antes de migrar. No se modificaron cantidades automaticamente.')
    for table, name, columns in [('inventories', 'uq_inventory_branch_variant', ['branch_id', 'variant_id']),
                                  ('orders', 'uq_order_reservation', ['reservation_id']),
                                  ('orders', 'uq_order_request', ['request_key'])]:
        if not any(u['column_names'] == columns for u in sa.inspect(bind).get_unique_constraints(table)):
            with op.batch_alter_table(table) as batch:
                batch.create_unique_constraint(name, columns)
    if not any(c['name'] == 'ck_inventory_stock_nonnegative' for c in sa.inspect(bind).get_check_constraints('inventories')):
        with op.batch_alter_table('inventories') as batch:
            batch.create_check_constraint('ck_inventory_stock_nonnegative', 'stock >= 0')

    # Preserve legacy role names without granting unknown roles any permissions.
    if 'role' in {c['name'] for c in sa.inspect(bind).get_columns('users')}:
        roles = sa.table('roles', sa.column('id'), sa.column('name'), sa.column('is_predefined'))
        for name in bind.execute(sa.text('SELECT DISTINCT role FROM users WHERE role IS NOT NULL')).scalars():
            role_id = bind.execute(sa.select(roles.c.id).where(roles.c.name == name)).scalar()
            if role_id is None:
                bind.execute(roles.insert().values(name=name, is_predefined=True))
                role_id = bind.execute(sa.select(roles.c.id).where(roles.c.name == name)).scalar_one()
            bind.execute(sa.text('UPDATE users SET role_id=:id WHERE role=:name AND role_id IS NULL'), {'id': role_id, 'name': name})


def downgrade():
    raise RuntimeError('Esta migracion preserva historial comercial. Restaurar una copia de seguridad para volver al esquema anterior.')
