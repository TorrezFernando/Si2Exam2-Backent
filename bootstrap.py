"""Initialize roles and the first administrator without deleting business data."""
from db.database import SessionLocal
from db.models import User, Role, Permission
from core.config import settings
from core.security import get_password_hash

PERMISSIONS = ['audit:read', 'roles:manage', 'users:read', 'users:write', 'branches:read', 'branches:write',
    'catalog:read', 'catalog:write', 'inventory:read', 'inventory:write', 'sales:read', 'sales:write',
    'reservations:read', 'reservations:write', 'saas:manage']
ROLES = {
    'superadmin': PERMISSIONS,
    'admin': [p for p in PERMISSIONS if p != 'saas:manage'],
    'cliente': ['catalog:read'],
    'encargado': ['catalog:read', 'catalog:write', 'inventory:read', 'inventory:write', 'sales:read', 'reservations:read', 'reservations:write'],
    'cajero': ['catalog:read', 'inventory:read', 'sales:read', 'sales:write', 'reservations:read', 'reservations:write'],
}


def bootstrap():
    with SessionLocal() as db:
        permissions = {}
        for name in PERMISSIONS:
            permission = db.query(Permission).filter_by(name=name).first()
            if permission is None:
                permission = Permission(name=name)
                db.add(permission)
                db.flush()
            permissions[name] = permission
        for name, grants in ROLES.items():
            role = db.query(Role).filter_by(name=name).first()
            if role is None:
                db.add(Role(name=name, is_predefined=True, permissions=[permissions[g] for g in grants]))
            elif not role.permissions:
                # Bootstrap is an explicit administrative initialization command.
                role.permissions = [permissions[g] for g in grants]
        db.flush()
        # Inicializar Planes de Suscripción
        from db.models.tenant import Tenant, SubscriptionPlan, Subscription
        plan_basic = db.query(SubscriptionPlan).filter_by(code='basic').first()
        if not plan_basic:
            plan_basic = SubscriptionPlan(
                name='Plan Básico', code='basic', max_branches=2,
                has_ai_reports=False, has_offline_sync=True, price=29.99
            )
            db.add(plan_basic)
        plan_pro = db.query(SubscriptionPlan).filter_by(code='professional').first()
        if not plan_pro:
            plan_pro = SubscriptionPlan(
                name='Plan Profesional', code='professional', max_branches=10,
                has_ai_reports=True, has_offline_sync=True, price=99.99
            )
            db.add(plan_pro)
        db.flush()

        # Tenant inicial
        tenant = db.query(Tenant).filter_by(subdomain='fashionstore').first()
        if not tenant:
            tenant = Tenant(name='FashionStore Corp', subdomain='fashionstore', is_active=True, exchange_window_days=30)
            db.add(tenant)
            db.flush()
            sub = Subscription(tenant_id=tenant.id, plan_id=plan_pro.id, status='active')
            db.add(sub)
            db.flush()

        if not db.query(User).filter_by(email=settings.FIRST_ADMIN_EMAIL).first():
            password = settings.FIRST_ADMIN_PASSWORD
            if not password or password == 'Admin@1234' or len(password) < 12:
                raise RuntimeError('Configura FIRST_ADMIN_PASSWORD con al menos 12 caracteres')
            role = db.query(Role).filter_by(name='admin').one()
            db.add(User(email=settings.FIRST_ADMIN_EMAIL, full_name=settings.FIRST_ADMIN_NAME,
                        hashed_password=get_password_hash(password), role=role, tenant_id=tenant.id))

        if not db.query(User).filter_by(email="superadmin@fashionstore.com").first():
            super_role = db.query(Role).filter_by(name='superadmin').first()
            if super_role:
                db.add(User(
                    email="superadmin@fashionstore.com",
                    full_name="Super Administrador Global SaaS",
                    hashed_password=get_password_hash("SuperAdmin@1234"),
                    role=super_role,
                    tenant_id=None,
                    is_active=True,
                ))
        db.commit()



if __name__ == '__main__':
    bootstrap()
