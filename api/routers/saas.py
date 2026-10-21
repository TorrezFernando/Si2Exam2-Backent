from typing import Any, List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func

from api.deps import (
    get_db,
    get_current_active_user,
    get_current_user_optional,
    require_superadmin,
    get_current_tenant_id,
)
from api.audit_logger import log_audit
from core import security
from db.models.user import User
from db.models.tenant import Tenant, SubscriptionPlan, Subscription
from db.models.branch import Branch
from db.models.product import Product
from db.models.sale import Order
from db.models.rbac import Role
from schemas.saas import (
    SubscriptionPlanSchema,
    TenantSchema,
    TenantDetailSchema,
    TenantCreate,
    TenantUpdate,
    ChangePlanRequest,
    MySubscriptionResponse,
    SubscriptionUsage,
    SaaSStatsResponse,
)

router = APIRouter()


# ─── 1. LISTAR PLANES DE SUSCRIPCIÓN (Público / Autenticado) ──────────────────

@router.get("/plans", response_model=List[SubscriptionPlanSchema])
def list_subscription_plans(
    db: Session = Depends(get_db),
) -> Any:
    """Retorna la lista de planes de suscripción disponibles en la plataforma SaaS."""
    return db.query(SubscriptionPlan).filter(SubscriptionPlan.is_active == True).order_by(SubscriptionPlan.price.asc()).all()


# ─── 2. MI SUSCRIPCIÓN Y CONSUMO (Tenant actual o Superadmin) ─────────────────

@router.get("/my-subscription", response_model=MySubscriptionResponse)
def get_my_subscription(
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> Any:
    """
    Retorna la información del plan contratado y métricas de consumo de la empresa actual.
    Si el usuario es Superadmin, devuelve el modo global o el tenant seleccionado vía 'x-tenant-id'.
    """
    is_super = bool(current_user.role and current_user.role.name == "superadmin")
    
    # Determinar tenant_id objetivo
    tenant_id = current_user.tenant_id
    if is_super and not tenant_id:
        header_tenant = request.headers.get("x-tenant-id")
        if header_tenant:
            try:
                tenant_id = int(header_tenant)
            except ValueError:
                pass
        else:
            # En modo superadmin global sin tenant específico
            first_tenant = db.query(Tenant).filter(Tenant.is_active == True).first()
            if first_tenant:
                tenant_id = first_tenant.id

    if not tenant_id:
        # Superadmin sin tenants registrados
        return MySubscriptionResponse(
            is_superadmin=True,
            tenant=None,
            plan=None,
            status="active",
            start_date=None,
            usage=None,
        )

    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")

    sub = tenant.active_subscription
    plan = sub.plan if sub else None

    # Métricas de consumo
    branches_count = db.query(Branch).filter(Branch.tenant_id == tenant.id, Branch.is_active == True).count()
    users_count = db.query(User).filter(User.tenant_id == tenant.id).count()
    products_count = db.query(Product).filter(Product.tenant_id == tenant.id).count()
    orders_count = db.query(Order).filter(Order.tenant_id == tenant.id).count()

    usage = SubscriptionUsage(
        branches_count=branches_count,
        branches_limit=plan.max_branches if plan else 2,
        users_count=users_count,
        products_count=products_count,
        orders_count=orders_count,
    )

    return MySubscriptionResponse(
        is_superadmin=is_super,
        tenant=tenant,
        plan=plan,
        status=sub.status if sub else "inactive",
        start_date=sub.start_date if sub else None,
        usage=usage,
    )


# ─── 3. GESTIÓN MULTI-EMPRESA (SUPERADMIN) ───────────────────────────────────

@router.get("/tenants", response_model=List[TenantDetailSchema])
def list_tenants(
    db: Session = Depends(get_db),
    _superadmin: User = Depends(require_superadmin),
) -> Any:
    """Lista todas las empresas registradas con sus planes activos y estadísticas (Superadmin)."""
    tenants = db.query(Tenant).order_by(Tenant.id.asc()).all()
    results = []

    for t in tenants:
        sub = t.active_subscription
        branches_c = db.query(Branch).filter(Branch.tenant_id == t.id).count()
        users_c = db.query(User).filter(User.tenant_id == t.id).count()
        orders_c = db.query(Order).filter(Order.tenant_id == t.id).count()

        results.append(
            TenantDetailSchema(
                id=t.id,
                name=t.name,
                subdomain=t.subdomain,
                exchange_window_days=t.exchange_window_days,
                is_active=t.is_active,
                created_at=t.created_at,
                updated_at=t.updated_at,
                active_plan=sub.plan if sub else None,
                subscription_status=sub.status if sub else "no_subscription",
                branches_count=branches_c,
                users_count=users_c,
                orders_count=orders_c,
            )
        )

    return results


@router.post("/tenants", response_model=TenantDetailSchema, status_code=status.HTTP_201_CREATED)
def create_tenant(
    tenant_in: TenantCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_superadmin),
) -> Any:
    """Crea una nueva empresa/tenant con su suscripción inicial y administrador (Superadmin)."""
    existing = db.query(Tenant).filter(Tenant.subdomain == tenant_in.subdomain.strip().lower()).first()
    if existing:
        raise HTTPException(
            status_code=400,
            detail=f"Ya existe una empresa registrada con el subdominio '{tenant_in.subdomain}'.",
        )

    # Buscar plan
    plan = db.query(SubscriptionPlan).filter(SubscriptionPlan.code == tenant_in.plan_code).first()
    if not plan:
        plan = db.query(SubscriptionPlan).filter(SubscriptionPlan.code == "basic").first()

    # Crear Tenant
    new_tenant = Tenant(
        name=tenant_in.name.strip(),
        subdomain=tenant_in.subdomain.strip().lower(),
        exchange_window_days=tenant_in.exchange_window_days,
        is_active=tenant_in.is_active,
    )
    db.add(new_tenant)
    db.flush()

    # Crear Suscripción
    if plan:
        sub = Subscription(
            tenant_id=new_tenant.id,
            plan_id=plan.id,
            status="active",
        )
        db.add(sub)
        db.flush()

    # Crear Usuario Administrador de la nueva empresa si se proveyó
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
        {"name": new_tenant.name, "subdomain": new_tenant.subdomain, "plan": plan.code if plan else None},
        tenant_id=new_tenant.id,
    )

    return TenantDetailSchema(
        id=new_tenant.id,
        name=new_tenant.name,
        subdomain=new_tenant.subdomain,
        exchange_window_days=new_tenant.exchange_window_days,
        is_active=new_tenant.is_active,
        created_at=new_tenant.created_at,
        updated_at=new_tenant.updated_at,
        active_plan=plan,
        subscription_status="active",
        branches_count=0,
        users_count=1 if tenant_in.admin_email else 0,
        orders_count=0,
    )


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

    t = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not t:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")

    sub = t.active_subscription
    branches_c = db.query(Branch).filter(Branch.tenant_id == t.id).count()
    users_c = db.query(User).filter(User.tenant_id == t.id).count()
    orders_c = db.query(Order).filter(Order.tenant_id == t.id).count()

    return TenantDetailSchema(
        id=t.id,
        name=t.name,
        subdomain=t.subdomain,
        exchange_window_days=t.exchange_window_days,
        is_active=t.is_active,
        created_at=t.created_at,
        updated_at=t.updated_at,
        active_plan=sub.plan if sub else None,
        subscription_status=sub.status if sub else "no_subscription",
        branches_count=branches_c,
        users_count=users_c,
        orders_count=orders_c,
    )


@router.patch("/tenants/{tenant_id}", response_model=TenantDetailSchema)
def update_tenant(
    tenant_id: int,
    tenant_in: TenantUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> Any:
    """Actualiza la información de una empresa (Nombre, Subdominio, Plazo de cambios, Estado)."""
    is_super = bool(current_user.role and current_user.role.name == "superadmin")
    if not is_super and current_user.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="No tienes permisos para modificar esta empresa.")

    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")

    # Si se intenta cambiar subdomain, verificar duplicados
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

    sub = tenant.active_subscription
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
        subscription_status=sub.status if sub else "no_subscription",
        branches_count=branches_c,
        users_count=users_c,
        orders_count=orders_c,
    )


# ─── 4. CAMBIAR PLAN DE SUSCRIPCIÓN ──────────────────────────────────────────

@router.post("/tenants/{tenant_id}/change-plan", response_model=TenantDetailSchema)
def change_tenant_plan(
    tenant_id: int,
    change_in: ChangePlanRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> Any:
    """
    Actualiza el plan de suscripción de la empresa (Upgrade / Downgrade).
    Accesible para el Superadmin o el Admin de la propia empresa.
    """
    is_super = bool(current_user.role and current_user.role.name == "superadmin")
    if not is_super and current_user.tenant_id != tenant_id:
        raise HTTPException(status_code=403, detail="No tienes permisos para modificar el plan de esta empresa.")

    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")

    new_plan = db.query(SubscriptionPlan).filter(
        SubscriptionPlan.code == change_in.plan_code.strip().lower(),
        SubscriptionPlan.is_active == True,
    ).first()
    if not new_plan:
        raise HTTPException(status_code=400, detail=f"El plan '{change_in.plan_code}' no existe o no está activo.")

    # Si se cambia a un plan menor (ej. Basic con máx 2 sucursales), verificar que no tenga más de 2 activas
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

    # Desactivar suscripciones previas
    prev_subs = db.query(Subscription).filter(Subscription.tenant_id == tenant.id, Subscription.status == "active").all()
    for ps in prev_subs:
        ps.status = "upgraded" if new_plan.price > (ps.plan.price if ps.plan else 0) else "downgraded"

    # Crear nueva suscripción activa
    new_sub = Subscription(
        tenant_id=tenant.id,
        plan_id=new_plan.id,
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
        {"tenant_id": tenant.id, "new_plan": new_plan.code, "price": new_plan.price},
        tenant_id=tenant.id,
    )

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
        active_plan=new_plan,
        subscription_status="active",
        branches_count=branches_c,
        users_count=users_c,
        orders_count=orders_c,
    )


@router.post("/my-subscription/change-plan", response_model=TenantDetailSchema)
def change_my_plan(
    change_in: ChangePlanRequest,
    request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> Any:
    """Permite al Admin de una empresa actualizar o cambiar su propio plan de suscripción."""
    tenant_id = get_current_tenant_id(request, current_user)
    return change_tenant_plan(tenant_id, change_in, db, current_user)



# ─── 5. MÉTRICAS GLOBALES SAAS (SUPERADMIN) ───────────────────────────────────

@router.get("/stats", response_model=SaaSStatsResponse)
def get_saas_stats(
    db: Session = Depends(get_db),
    _superadmin: User = Depends(require_superadmin),
) -> Any:
    """Retorna indicadores clave de rendimiento (KPIs) de toda la plataforma SaaS."""
    total_tenants = db.query(Tenant).count()
    active_tenants = db.query(Tenant).filter(Tenant.is_active == True).count()
    total_branches = db.query(Branch).count()
    total_users = db.query(User).count()
    total_orders = db.query(Order).count()

    # Distribución por planes
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
