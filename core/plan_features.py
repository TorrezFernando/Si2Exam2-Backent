"""Plan limits and AI features configured by the Superadmin, enforced per company."""
from datetime import datetime, timezone
from fastapi import HTTPException
from core.config import settings

# Opciones ofrecidas en el editor de planes; el backend acepta cualquier id "gemini-*" válido.
AI_TEXT_MODELS = ('gemini-2.5-flash', 'gemini-2.5-flash-lite', 'gemini-2.5-pro', 'gemini-2.0-flash')
AI_IMAGE_MODELS = ('gemini-3.1-flash-image', 'gemini-2.5-flash-image')
MODEL_PATTERN = r'^gemini-[a-z0-9][a-z0-9.\-]{1,50}$'


def _aware(value):
    return value.replace(tzinfo=value.tzinfo or timezone.utc) if value else None


def current_plan(db, tenant_id):
    """Plan de la suscripción vigente de la empresa, o None si no hay una activa y dentro de su periodo."""
    if not tenant_id:
        return None
    from db.models.tenant import Tenant
    from core.subscription_plans import process_expired_subscriptions
    process_expired_subscriptions(db, tenant_id=tenant_id)
    tenant = db.get(Tenant, tenant_id)
    if not tenant:
        return None
    db.refresh(tenant)
    sub = tenant.active_subscription
    if not sub or not sub.plan:
        return None
    end = _aware(sub.end_date)
    if end and end <= datetime.now(timezone.utc):
        return None
    return sub.plan


def text_model(plan):
    return (plan.ai_text_model if plan else None) or settings.GEMINI_TEXT_MODEL or 'gemini-2.5-flash'


def image_model(plan):
    return (plan.ai_image_model if plan else None) or settings.GEMINI_MODEL


def staff_count(db, tenant_id):
    from db.models.user import User
    from db.models.rbac import Role
    return db.query(User).outerjoin(Role, Role.id == User.role_id).filter(
        User.tenant_id == tenant_id, User.is_active.is_(True),
        (Role.name.is_(None)) | (Role.name != 'cliente')).count()


def product_count(db, tenant_id):
    from db.models.product import Product
    return db.query(Product).filter(Product.tenant_id == tenant_id, Product.is_active.is_(True)).count()


LIMITS = {
    'users': ('max_users', staff_count, 'usuarios del personal'),
    'products': ('max_products', product_count, 'productos activos'),
}


def enforce_limit(db, tenant_id, kind):
    """Rechaza la creación si la empresa ya alcanzó el límite de su plan (NULL = ilimitado).
    Sin suscripción vigente no se aplica límite: eso lo controla el ciclo de suscripción, no este guardia."""
    attribute, counter, label = LIMITS[kind]
    plan = current_plan(db, tenant_id)
    if not plan:
        return None
    limit = getattr(plan, attribute)
    if limit is not None and counter(db, tenant_id) >= limit:
        raise HTTPException(403, f'Límite de {label} alcanzado ({limit}) para el {plan.name}. '
                                 'Cambia de plan o desactiva registros antes de continuar.')
    return plan
