from datetime import datetime, timezone
from typing import Any, List
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.orm import Session, joinedload

from api.deps import (
    get_db,
    get_current_active_user,
    require_superadmin,
)
from api.audit_logger import log_audit
from core import security
from core.access import check_company_admin, company_id
from core.subscription_plans import (
    calculate_period_end,
    days_until_end,
    ensure_aware,
    process_expired_subscriptions,
)
from db.models.user import User
from db.models.tenant import Tenant, SubscriptionPlan, Subscription
from db.models.branch import Branch
from db.models.product import Product
from db.models.sale import Order
from db.models.rbac import Role
from schemas.saas import (
    SubscriptionPlanSchema,
    TenantDetailSchema,
    TenantCreate,
    TenantUpdate,
    ChangePlanRequest,
    AutoCancelUpdateRequest,
    MySubscriptionResponse,
    SubscriptionUsage,
    SaaSStatsResponse,
)

router = APIRouter()


def _build_tenant_detail(db: Session, tenant: Tenant) -> TenantDetailSchema:
    sub = tenant.active_subscription
    latest = sub or tenant.latest_subscription
    branches_c = db.query(Branch).filter(Branch.tenant_id == tenant.id).count()
    users_c = db.query(User).filter(User.tenant_id == tenant.id).count()
    orders_c = db.query(Order).filter(Order.tenant_id == tenant.id).count()

    return TenantDetailSchema(
        id=tenant.id,
        name=tenant.name,
        subdomain=tenant.subdomain,
        exchange_window_days=tenant.exchange_window_days,
        is_active=tenant.is_active,
        created_at=tenant.created_at,
        updated_at=tenant.updated_at,
        active_plan=sub.plan if sub else None,
        subscription_status=sub.status if sub else (latest.status if latest else "no_subscription"),
        subscription_end_date=sub.end_date if sub else (latest.end_date if latest else None),
        cancel_at_period_end=bool(sub.cancel_at_period_end) if sub else (bool(latest.cancel_at_period_end) if latest else True),
        days_remaining=days_until_end(sub.end_date) if sub else 0,
        branches_count=branches_c,
        users_count=users_c,
        orders_count=orders_c,
    )


# ─── 1. LISTAR PLANES DE SUSCRIPCIÓN (Público / Autenticado) ──────────────────

@router.get("/plans", response_model=List[SubscriptionPlanSchema])
def list_subscription_plans(
    db: Session = Depends(get_db),
) -> Any:
    """Retorna la lista de planes de suscripción disponibles en la plataforma SaaS."""
    return (
        db.query(SubscriptionPlan)
        .filter(SubscriptionPlan.is_active == True)
        .order_by(SubscriptionPlan.price.asc())
        .all()
    )


# ─── 2. MI SUSCRIPCIÓN Y CONSUMO (Empresa actual; separado de Superadmin) ─────

@router.get("/my-subscription", response_model=MySubscriptionResponse)
def get_my_subscription(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> Any:
    """
    Retorna la información del plan contratado, vigencia mensual y métricas de consumo de la empresa actual.
    Si el usuario es Superadmin global, retorna vista de plataforma sin mezclar datos de ninguna empresa.
    """
    is_super = bool(current_user.role and current_user.role.name == "superadmin")
    if is_super and current_user.tenant_id is None:
        process_expired_subscriptions(db)
        return MySubscriptionResponse(
            is_superadmin=True,
            tenant=None,
            plan=None,
            status="global",
            start_date=None,
            end_date=None,
            cancel_at_period_end=False,
            cancelled_at=None,
            days_remaining=None,
            usage=None,
        )

    tenant_id = company_id(current_user)
    process_expired_subscriptions(db, tenant_id=tenant_id)

    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")

    sub = tenant.active_subscription
    latest = sub or tenant.latest_subscription
    plan = sub.plan if sub else None

    branches_count = db.query(Branch).filter(Branch.tenant_id == tenant.id, Branch.is_active == True).count()
    users_count = db.query(User).filter(User.tenant_id == tenant.id).count()
    products_count = db.query(Product).filter(Product.tenant_id == tenant.id).count()
    orders_count = db.query(Order).filter(Order.tenant_id == tenant.id).count()

    usage = SubscriptionUsage(
        branches_count=branches_count,
        branches_limit=plan.max_branches if plan else 0,
        users_count=users_count,
        products_count=products_count,
        orders_count=orders_count,
    )

    return MySubscriptionResponse(
        is_superadmin=False,
        tenant=tenant,
        plan=plan,
        status=sub.status if sub else (latest.status if latest else "inactive"),
        start_date=sub.start_date if sub else (latest.start_date if latest else None),
        end_date=sub.end_date if sub else (latest.end_date if latest else None),
        cancel_at_period_end=bool(sub.cancel_at_period_end) if sub else (bool(latest.cancel_at_period_end) if latest else True),
        cancelled_at=latest.cancelled_at if latest else None,
        days_remaining=days_until_end(sub.end_date) if sub else 0,
        usage=usage,
    )


# ─── 3. GESTIÓN MULTI-EMPRESA (SUPERADMIN) ───────────────────────────────────

@router.get("/tenants", response_model=List[TenantDetailSchema])
def list_tenants(
    db: Session = Depends(get_db),
    _superadmin: User = Depends(require_superadmin),
) -> Any:
    """Lista todas las empresas registradas con sus planes activos, vigencia y estadísticas (Superadmin)."""
    process_expired_subscriptions(db)
    tenants = db.query(Tenant).order_by(Tenant.id.asc()).all()
    return [_build_tenant_detail(db, t) for t in tenants]


@router.post("/tenants", response_model=TenantDetailSchema, status_code=status.HTTP_201_CREATED)
def create_tenant(
    tenant_in: TenantCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_superadmin),
) -> Any:
    """Crea una nueva empresa/tenant con su suscripción mensual inicial y administrador (Superadmin)."""
    existing = db.query(Tenant).filter(Tenant.subdomain == tenant_in.subdomain.strip().lower()).first()
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Ya existe una empresa registrada con el subdominio '{tenant_in.subdomain}'.",
        )

    plan = db.query(SubscriptionPlan).filter(
        SubscriptionPlan.code == tenant_in.plan_code.strip().lower(),
        SubscriptionPlan.is_active == True,
    ).first()
    if not plan:
        raise HTTPException(400, 'El plan solicitado no existe o no está activo.')

    new_tenant = Tenant(
        name=tenant_in.name.strip(),
        subdomain=tenant_in.subdomain.strip().lower(),
        exchange_window_days=tenant_in.exchange_window_days,
        is_active=tenant_in.is_active,
    )
    db.add(new_tenant)
    db.flush()

    now = datetime.now(timezone.utc)
    sub = Subscription(
        tenant_id=new_tenant.id,
        plan_id=plan.id,
        start_date=now,
        end_date=calculate_period_end(now),
        cancel_at_period_end=True,
        status="active",
    )
    db.add(sub)
    db.flush()

    if tenant_in.admin_email and tenant_in.admin_password:
        existing_user = db.query(User).filter(User.email == tenant_in.admin_email).first()
        if not existing_user:
            admin_role = db.query(Role).filter(Role.name == "admin").first()
            new_admin = User(
                email=tenant_in.admin_email,
                hashed_password=security.get_password_hash(tenant_in.admin_password),
                full_name=tenant_in.admin_name or f"Admin {new_tenant.name}",
                role_id=admin_role.id if admin_role else None,
                tenant_id=new_tenant.id,
                is_active=True,
            )
            db.add(new_admin)

    db.commit()
    db.refresh(new_tenant)

    log_audit(
        db,
        current_user.id,
        "CREATE",
        "TENANT",
        str(new_tenant.id),
        {
            "name": new_tenant.name,
            "subdomain": new_tenant.subdomain,
            "plan": plan.code,
            "end_date": sub.end_date.isoformat(),
            "cancel_at_period_end": True,
        },
        tenant_id=new_tenant.id,
    )

    return _build_tenant_detail(db, new_tenant)


@router.get("/tenants/{tenant_id}", response_model=TenantDetailSchema)
def get_tenant(
    tenant_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> Any:
    """Obtiene los detalles de una empresa específica."""
    is_super = bool(current_user.role and current_user.role.name == "superadmin")
    if not is_super and current_user.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="No tienes acceso a esta empresa.")

    process_expired_subscriptions(db, tenant_id=tenant_id)
    t = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")

    return _build_tenant_detail(db, t)


@router.patch("/tenants/{tenant_id}", response_model=TenantDetailSchema)
def update_tenant(
    tenant_id: int,
    tenant_in: TenantUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> Any:
    """Actualiza la información de una empresa (Nombre, Subdominio, Plazo de cambios, Estado)."""
    check_company_admin(db, current_user, tenant_id)
    is_super = bool(current_user.role and current_user.role.name == "superadmin")
    if not is_super and current_user.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="No tienes permisos para modificar esta empresa.")

    process_expired_subscriptions(db, tenant_id=tenant_id)
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")

    if tenant_in.subdomain and tenant_in.subdomain.strip().lower() != tenant.subdomain:
        existing = db.query(Tenant).filter(
            Tenant.subdomain == tenant_in.subdomain.strip().lower(),
            Tenant.id != tenant.id
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail="Ese subdominio ya está registrado por otra empresa.")
        tenant.subdomain = tenant_in.subdomain.strip().lower()

    if tenant_in.name is not None:
        tenant.name = tenant_in.name.strip()
    if tenant_in.exchange_window_days is not None:
        tenant.exchange_window_days = tenant_in.exchange_window_days
    if tenant_in.is_active is not None:
        tenant.is_active = tenant_in.is_active

    db.commit()
    db.refresh(tenant)

    log_audit(
        db,
        current_user.id,
        "UPDATE",
        "TENANT",
        str(tenant.id),
        {"name": tenant.name, "is_active": tenant.is_active, "window": tenant.exchange_window_days},
        tenant_id=tenant.id,
    )

    return _build_tenant_detail(db, tenant)


# ─── 4. CAMBIAR PLAN, CANCELACIÓN AUTOMÁTICA A FIN DE MES Y RENOVACIÓN ───────

@router.post("/tenants/{tenant_id}/change-plan", response_model=TenantDetailSchema)
def change_tenant_plan(
    tenant_id: int,
    change_in: ChangePlanRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> Any:
    """
    Actualiza el plan de suscripción de la empresa (Upgrade / Downgrade) iniciando un ciclo mensual de 30 días.
    Accesible para el Superadmin o el Admin de la propia empresa.
    """
    check_company_admin(db, current_user, tenant_id)
    is_super = bool(current_user.role and current_user.role.name == "superadmin")
    if not is_super and current_user.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="No tienes permisos para modificar el plan de esta empresa.")

    process_expired_subscriptions(db, tenant_id=tenant_id)
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).with_for_update().first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")

    new_plan = db.query(SubscriptionPlan).filter(
        SubscriptionPlan.code == change_in.plan_code.strip().lower(),
        SubscriptionPlan.is_active == True,
    ).first()
    if not new_plan:
        raise HTTPException(status_code=400, detail=f"El plan '{change_in.plan_code}' no existe o no está activo.")

    active_branches = db.query(Branch).filter(Branch.tenant_id == tenant.id, Branch.is_active == True).count()
    if active_branches > new_plan.max_branches:
        raise HTTPException(
            status_code=400,
            detail=(
                f"No se puede cambiar al {new_plan.name} porque la empresa cuenta actualmente con "
                f"{active_branches} sucursales activas (el plan permite un máximo de {new_plan.max_branches}). "
                "Desactiva las sucursales sobrantes antes de reducir el plan."
            ),
        )

    prev_subs = db.query(Subscription).filter(Subscription.tenant_id == tenant.id, Subscription.status == "active").all()
    keep_auto_cancel = True
    for ps in prev_subs:
        keep_auto_cancel = bool(ps.cancel_at_period_end)
        ps.status = "upgraded" if new_plan.price > (ps.plan.price if ps.plan else 0) else "downgraded"

    now = datetime.now(timezone.utc)
    new_sub = Subscription(
        tenant_id=tenant.id,
        plan_id=new_plan.id,
        start_date=now,
        end_date=calculate_period_end(now),
        cancel_at_period_end=keep_auto_cancel,
        status="active",
    )
    db.add(new_sub)
    db.commit()
    db.refresh(tenant)

    log_audit(
        db,
        current_user.id,
        "CHANGE_PLAN",
        "SUBSCRIPTION",
        str(new_sub.id),
        {
            "tenant_id": tenant.id,
            "new_plan": new_plan.code,
            "price": new_plan.price,
            "end_date": new_sub.end_date.isoformat(),
            "cancel_at_period_end": new_sub.cancel_at_period_end,
        },
        tenant_id=tenant.id,
    )

    return _build_tenant_detail(db, tenant)


@router.post("/my-subscription/change-plan", response_model=TenantDetailSchema)
def change_my_plan(
    change_in: ChangePlanRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> Any:
    """Permite al Admin de una empresa actualizar o cambiar su propio plan de suscripción."""
    tenant_id = company_id(current_user)
    return change_tenant_plan(tenant_id, change_in, db, current_user)


@router.patch("/tenants/{tenant_id}/auto-cancel", response_model=TenantDetailSchema)
def set_tenant_auto_cancel(
    tenant_id: int,
    payload: AutoCancelUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> Any:
    """Activa o desactiva la cancelación automática al finalizar el mes de suscripción."""
    check_company_admin(db, current_user, tenant_id)
    process_expired_subscriptions(db, tenant_id=tenant_id)

    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")

    sub = tenant.active_subscription
    if not sub:
        raise HTTPException(
            status_code=400,
            detail="La empresa no tiene una suscripción activa en este momento. Renueva o contrata un plan primero.",
        )

    sub.cancel_at_period_end = payload.cancel_at_period_end
    db.commit()
    db.refresh(tenant)

    log_audit(
        db,
        current_user.id,
        "UPDATE_AUTO_CANCEL",
        "SUBSCRIPTION",
        str(sub.id),
        {"tenant_id": tenant.id, "cancel_at_period_end": sub.cancel_at_period_end, "end_date": sub.end_date.isoformat() if sub.end_date else None},
        tenant_id=tenant.id,
    )
    return _build_tenant_detail(db, tenant)


@router.patch("/my-subscription/auto-cancel", response_model=TenantDetailSchema)
def set_my_auto_cancel(
    payload: AutoCancelUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> Any:
    """Permite al Admin de Empresa programar o desactivar la cancelación automática a fin de mes."""
    tenant_id = company_id(current_user)
    return set_tenant_auto_cancel(tenant_id, payload, db, current_user)


@router.post("/tenants/{tenant_id}/renew", response_model=TenantDetailSchema)
def renew_tenant_subscription(
    tenant_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> Any:
    """Renueva por 30 días adicionales la suscripción actual (o reactiva la última suscripción cancelada/vencida)."""
    check_company_admin(db, current_user, tenant_id)
    process_expired_subscriptions(db, tenant_id=tenant_id)

    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).with_for_update().first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")

    now = datetime.now(timezone.utc)
    sub = tenant.active_subscription
    if sub:
        current_end = ensure_aware(sub.end_date) or now
        base = current_end if current_end > now else now
        sub.end_date = calculate_period_end(base)
        target_sub = sub
    else:
        latest = tenant.latest_subscription
        plan = latest.plan if (latest and latest.plan and latest.plan.is_active) else (
            db.query(SubscriptionPlan).filter(SubscriptionPlan.code == "basic", SubscriptionPlan.is_active == True).first()
        )
        if not plan:
            raise HTTPException(status_code=400, detail="No hay un plan activo disponible para renovar.")
        target_sub = Subscription(
            tenant_id=tenant.id,
            plan_id=plan.id,
            start_date=now,
            end_date=calculate_period_end(now),
            cancel_at_period_end=True,
            status="active",
        )
        db.add(target_sub)

    db.commit()
    db.refresh(tenant)

    log_audit(
        db,
        current_user.id,
        "RENEW",
        "SUBSCRIPTION",
        str(target_sub.id),
        {"tenant_id": tenant.id, "end_date": target_sub.end_date.isoformat()},
        tenant_id=tenant.id,
    )
    return _build_tenant_detail(db, tenant)


@router.post("/my-subscription/renew", response_model=TenantDetailSchema)
def renew_my_subscription(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> Any:
    """Permite al Admin de Empresa renovar su suscripción por un mes adicional (30 días)."""
    tenant_id = company_id(current_user)
    return renew_tenant_subscription(tenant_id, db, current_user)


@router.post("/subscriptions/process-expirations")
def trigger_subscription_expirations(
    db: Session = Depends(get_db),
    _superadmin: User = Depends(require_superadmin),
) -> Any:
    """Ejecuta bajo demanda la cancelación automática de suscripciones con mes vencido (Superadmin)."""
    processed = process_expired_subscriptions(db)
    return {"processed": processed}


# ─── 5. MÉTRICAS GLOBALES SAAS (SUPERADMIN) ───────────────────────────────────

@router.get("/stats", response_model=SaaSStatsResponse)
def get_saas_stats(
    db: Session = Depends(get_db),
    _superadmin: User = Depends(require_superadmin),
) -> Any:
    """Retorna indicadores clave de rendimiento (KPIs) de toda la plataforma SaaS."""
    process_expired_subscriptions(db)
    total_tenants = db.query(Tenant).count()
    active_tenants = db.query(Tenant).filter(Tenant.is_active == True).count()
    total_branches = db.query(Branch).count()
    total_users = db.query(User).count()
    total_orders = db.query(Order).count()

    plans_dist = {}
    active_subs = (
        db.query(Subscription)
        .options(joinedload(Subscription.plan))
        .filter(Subscription.status == "active")
        .all()
    )
    monthly_mrr = 0.0
    for s in active_subs:
        if s.plan:
            code = s.plan.code
            plans_dist[code] = plans_dist.get(code, 0) + 1
            monthly_mrr += s.plan.price

    return SaaSStatsResponse(
        total_tenants=total_tenants,
        active_tenants=active_tenants,
        total_branches=total_branches,
        total_users=total_users,
        total_orders=total_orders,
        plans_distribution=plans_dist,
        monthly_recurring_revenue=round(monthly_mrr, 2),
    )
