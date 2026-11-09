from datetime import datetime, timezone
from uuid import uuid4
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
from db.models.tenant import Tenant, SubscriptionPlan, Subscription, SubscriptionPayment
from core.config import settings
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
    PlanCreate,
    PlanUpdate,
    AIModelOptions,
    SubscriptionCheckoutRequest,
    SubscriptionCheckoutResponse,
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

def _subscribers(db: Session, plan_id: int) -> int:
    return db.query(Subscription).filter(Subscription.plan_id == plan_id, Subscription.status == "active").count()


def _plan_out(db: Session, plan: SubscriptionPlan) -> SubscriptionPlanSchema:
    out = SubscriptionPlanSchema.model_validate(plan)
    out.subscribers_count = _subscribers(db, plan.id)
    return out


@router.get("/plans", response_model=List[SubscriptionPlanSchema])
def list_subscription_plans(
    include_inactive: bool = False,
    db: Session = Depends(get_db),
) -> Any:
    """Planes ofrecidos. `include_inactive` solo tiene efecto en el editor del Superadmin (la UI lo pide allí)."""
    query = db.query(SubscriptionPlan)
    if not include_inactive:
        query = query.filter(SubscriptionPlan.is_active == True)
    return [_plan_out(db, p) for p in query.order_by(SubscriptionPlan.price.asc()).all()]


@router.get("/plans/ai-models", response_model=AIModelOptions)
def list_ai_models(current_user: User = Depends(require_superadmin)) -> Any:
    from core.config import settings
    from core.plan_features import AI_TEXT_MODELS, AI_IMAGE_MODELS
    return AIModelOptions(text_models=list(AI_TEXT_MODELS), image_models=list(AI_IMAGE_MODELS),
                          default_text_model=settings.GEMINI_TEXT_MODEL or 'gemini-2.5-flash',
                          default_image_model=settings.GEMINI_MODEL)


REQUIRED_PLAN_FIELDS = {"name", "price", "max_branches", "has_ai_reports", "has_ai_shopper",
                        "has_ai_tryon", "has_offline_sync", "is_active"}


def _apply_plan_changes(db: Session, plan: SubscriptionPlan, changes: dict) -> dict:
    nulls = sorted(k for k, v in changes.items() if v is None and k in REQUIRED_PLAN_FIELDS)
    if nulls:
        raise HTTPException(422, f"Estos campos no pueden quedar vacíos: {', '.join(nulls)}.")
    if changes.get("is_active") is False and plan.id and _subscribers(db, plan.id):
        raise HTTPException(409, "No puedes desactivar un plan con empresas suscritas. Cámbialas de plan primero.")
    previous = {k: getattr(plan, k) for k in changes}
    for key, value in changes.items():
        setattr(plan, key, value.strip() if isinstance(value, str) else value)
    return previous


@router.post("/plans", response_model=SubscriptionPlanSchema, status_code=status.HTTP_201_CREATED)
def create_plan(plan_in: PlanCreate, db: Session = Depends(get_db),
                current_user: User = Depends(require_superadmin)) -> Any:
    """Crea un plan nuevo (Superadmin)."""
    if db.query(SubscriptionPlan).filter(SubscriptionPlan.code == plan_in.code).first():
        raise HTTPException(409, f"Ya existe un plan con el código '{plan_in.code}'.")
    plan = SubscriptionPlan(code=plan_in.code, has_ai_reports=False, has_ai_shopper=True, has_ai_tryon=True,
                            has_offline_sync=True, is_active=True)
    _apply_plan_changes(db, plan, plan_in.model_dump(exclude={"code"}, exclude_unset=True))
    db.add(plan)
    db.commit()
    db.refresh(plan)
    log_audit(db, current_user.id, "CREATE", "SUBSCRIPTION_PLAN", str(plan.id),
              plan_in.model_dump(), tenant_id=None)
    return _plan_out(db, plan)


@router.patch("/plans/{plan_id}", response_model=SubscriptionPlanSchema)
def update_plan(plan_id: int, plan_in: PlanUpdate, db: Session = Depends(get_db),
                current_user: User = Depends(require_superadmin)) -> Any:
    """
    Modifica límites, funciones de IA, modelos y precio de un plan (Superadmin).
    Los cambios aplican de inmediato a todas las empresas suscritas; bajar un límite
    no desactiva registros existentes, solo impide crear nuevos por encima del límite.
    """
    plan = db.query(SubscriptionPlan).filter(SubscriptionPlan.id == plan_id).with_for_update().first()
    if not plan:
        raise HTTPException(404, "Plan no encontrado.")
    changes = plan_in.model_dump(exclude_unset=True)
    previous = _apply_plan_changes(db, plan, changes)
    db.commit()
    db.refresh(plan)
    log_audit(db, current_user.id, "UPDATE", "SUBSCRIPTION_PLAN", str(plan.id),
              {"before": previous, "after": changes}, tenant_id=None)
    return _plan_out(db, plan)


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

    from core.plan_features import staff_count, product_count
    usage = SubscriptionUsage(
        branches_count=branches_count,
        branches_limit=plan.max_branches if plan else 0,
        users_count=users_count,
        users_limit=plan.max_users if plan else None,
        staff_count=staff_count(db, tenant.id),
        products_count=product_count(db, tenant.id),
        products_limit=plan.max_products if plan else None,
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

def _target_plan(db: Session, tenant: Tenant, plan_code: str) -> SubscriptionPlan:
    """Valida que el plan exista, esté disponible y admita las sucursales activas de la empresa."""
    new_plan = db.query(SubscriptionPlan).filter(
        SubscriptionPlan.code == plan_code.strip().lower(),
        SubscriptionPlan.is_active == True,
    ).first()
    if not new_plan:
        raise HTTPException(status_code=400, detail=f"El plan '{plan_code}' no existe o no está activo.")
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
    return new_plan


def _apply_plan_change(db: Session, tenant: Tenant, new_plan: SubscriptionPlan, actor_id, via: str) -> Subscription:
    """Reemplaza la suscripción activa por el nuevo plan con un ciclo de 30 días (sin commit)."""
    prev_subs = db.query(Subscription).filter(Subscription.tenant_id == tenant.id, Subscription.status == "active").all()
    keep_auto_cancel = True
    for ps in prev_subs:
        keep_auto_cancel = bool(ps.cancel_at_period_end)
        ps.status = "upgraded" if new_plan.price > (ps.plan.price if ps.plan else 0) else "downgraded"
    now = datetime.now(timezone.utc)
    new_sub = Subscription(tenant_id=tenant.id, plan_id=new_plan.id, start_date=now,
                           end_date=calculate_period_end(now), cancel_at_period_end=keep_auto_cancel, status="active")
    db.add(new_sub)
    db.flush()
    log_audit(db, actor_id, "CHANGE_PLAN", "SUBSCRIPTION", str(new_sub.id), {
        "tenant_id": tenant.id, "new_plan": new_plan.code, "price": new_plan.price, "via": via,
        "end_date": new_sub.end_date.isoformat(), "cancel_at_period_end": new_sub.cancel_at_period_end,
    }, tenant_id=tenant.id)
    return new_sub


def _renewal_plan(db: Session, tenant: Tenant) -> SubscriptionPlan:
    sub = tenant.active_subscription
    if sub and sub.plan:
        return sub.plan
    latest = tenant.latest_subscription
    plan = latest.plan if (latest and latest.plan and latest.plan.is_active) else (
        db.query(SubscriptionPlan).filter(SubscriptionPlan.code == "basic", SubscriptionPlan.is_active == True).first()
    )
    if not plan:
        raise HTTPException(status_code=400, detail="No hay un plan activo disponible para renovar.")
    return plan


def _apply_renewal(db: Session, tenant: Tenant, actor_id, via: str, plan: SubscriptionPlan | None = None) -> Subscription:
    """Suma 30 días a la suscripción activa o abre un ciclo nuevo si estaba vencida (sin commit)."""
    now = datetime.now(timezone.utc)
    sub = tenant.active_subscription
    if sub:
        current_end = ensure_aware(sub.end_date) or now
        sub.end_date = calculate_period_end(current_end if current_end > now else now)
        target_sub = sub
    else:
        plan = plan or _renewal_plan(db, tenant)
        target_sub = Subscription(tenant_id=tenant.id, plan_id=plan.id, start_date=now,
                                  end_date=calculate_period_end(now), cancel_at_period_end=True, status="active")
        db.add(target_sub)
    db.flush()
    log_audit(db, actor_id, "RENEW", "SUBSCRIPTION", str(target_sub.id),
              {"tenant_id": tenant.id, "end_date": target_sub.end_date.isoformat(), "via": via}, tenant_id=tenant.id)
    return target_sub


@router.post("/tenants/{tenant_id}/change-plan", response_model=TenantDetailSchema)
def change_tenant_plan(
    tenant_id: int,
    change_in: ChangePlanRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_superadmin),
) -> Any:
    """
    Asignación directa de plan por el Superadmin (sin cobro), con ciclo nuevo de 30 días.
    Las empresas cambian su propio plan con /my-subscription/checkout, que exige el pago.
    """
    process_expired_subscriptions(db, tenant_id=tenant_id)
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).with_for_update().first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")
    new_plan = _target_plan(db, tenant, change_in.plan_code)
    _apply_plan_change(db, tenant, new_plan, current_user.id, via="superadmin")
    db.commit()
    db.refresh(tenant)
    return _build_tenant_detail(db, tenant)


# ─── Cobro de planes para la propia empresa ──────────────────────────────────

def _payment_out(db: Session, row: SubscriptionPayment, tenant: Tenant | None = None) -> SubscriptionCheckoutResponse:
    return SubscriptionCheckoutResponse(
        payment_id=row.id, kind=row.kind, status=row.status, amount=row.amount, currency=row.currency,
        provider=row.provider, payment_url=row.payment_url if row.status == "pending" else None,
        plan=_plan_out(db, row.plan), error=row.error, created_at=row.created_at, paid_at=row.paid_at,
        tenant=_build_tenant_detail(db, tenant) if tenant is not None else None,
    )


def _apply_payment(db: Session, row: SubscriptionPayment, tenant: Tenant, actor_id) -> None:
    """Aplica el plan pagado. Si ya no puede aplicarse (p. ej. sobran sucursales) lo deja en 'failed' para revisión."""
    try:
        if row.kind == "change_plan":
            plan = _target_plan(db, tenant, row.plan.code)
            sub = _apply_plan_change(db, tenant, plan, actor_id, via=f"payment:{row.reference}")
        else:
            sub = _apply_renewal(db, tenant, actor_id, via=f"payment:{row.reference}", plan=row.plan)
        row.status, row.subscription_id, row.error = "applied", sub.id, None
    except HTTPException as error:
        row.status = "failed"
        row.error = f"Pago confirmado pero no aplicado: {error.detail}"[:255]


@router.post("/my-subscription/checkout", response_model=SubscriptionCheckoutResponse)
def start_subscription_checkout(
    checkout_in: SubscriptionCheckoutRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> Any:
    """
    Inicia el pago de un cambio de plan o de la renovación mensual de la propia empresa.
    El cambio solo se aplica cuando PayPal confirma el pago (ver /my-subscription/payments/{id}/verify).
    Un plan con precio 0 se aplica de inmediato.
    """
    from core import paypal
    tenant_id = company_id(current_user)
    check_company_admin(db, current_user, tenant_id)
    process_expired_subscriptions(db, tenant_id=tenant_id)
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).with_for_update().first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")
    if checkout_in.kind == "change_plan":
        if not checkout_in.plan_code:
            raise HTTPException(422, "Indica el plan al que deseas cambiar.")
        plan = _target_plan(db, tenant, checkout_in.plan_code)
        current = tenant.active_subscription
        if current and current.plan_id == plan.id:
            raise HTTPException(409, "La empresa ya tiene este plan. Usa la renovación para extender el mes.")
    else:
        plan = _renewal_plan(db, tenant)

    amount = float(round(plan.price or 0, 2))
    if amount <= 0:
        row = SubscriptionPayment(tenant_id=tenant.id, plan_id=plan.id, user_id=current_user.id, kind=checkout_in.kind,
                                  amount=0.0, provider="Gratis", reference=f"SUB-{uuid4().hex}", status="pending",
                                  paid_at=datetime.now(timezone.utc))
        db.add(row); db.flush()
        _apply_payment(db, row, tenant, current_user.id)
        db.commit(); db.refresh(tenant)
        return _payment_out(db, row, tenant)

    # Reutiliza un cobro pendiente idéntico para no crear órdenes duplicadas en PayPal.
    pending = db.query(SubscriptionPayment).filter(
        SubscriptionPayment.tenant_id == tenant.id, SubscriptionPayment.kind == checkout_in.kind,
        SubscriptionPayment.plan_id == plan.id, SubscriptionPayment.status == "pending",
        SubscriptionPayment.amount == amount, SubscriptionPayment.payment_url.isnot(None),
    ).order_by(SubscriptionPayment.id.desc()).first()
    if pending:
        db.commit()
        return _payment_out(db, pending)

    paypal.ensure_configured()
    row = SubscriptionPayment(tenant_id=tenant.id, plan_id=plan.id, user_id=current_user.id, kind=checkout_in.kind,
                              amount=amount, currency=paypal.CURRENCY, provider="PayPal",
                              reference=f"SUB-{uuid4().hex}", status="pending")
    db.add(row); db.flush()
    web = settings.PUBLIC_WEB_URL.rstrip("/")
    action = f"Cambio al {plan.name}" if checkout_in.kind == "change_plan" else f"Renovación {plan.name}"
    row.provider_order_id, row.payment_url = paypal.create_checkout(
        row.reference, amount, f"FashionStore SaaS · {action} · {tenant.name} (30 días)",
        f"{web}/admin/plans?sub_payment={row.id}", f"{web}/admin/plans?sub_payment={row.id}&cancelled=1")
    db.commit()
    log_audit(db, current_user.id, "CHECKOUT", "SUBSCRIPTION_PAYMENT", str(row.id),
              {"kind": row.kind, "plan": plan.code, "amount": amount}, tenant_id=tenant.id)
    return _payment_out(db, row)


@router.post("/my-subscription/payments/{payment_id}/verify", response_model=SubscriptionCheckoutResponse)
def verify_subscription_payment(
    payment_id: int,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> Any:
    """Confirma el pago con PayPal en el servidor y, si está pagado, aplica el plan una sola vez."""
    from core import paypal
    tenant_id = company_id(current_user)
    check_company_admin(db, current_user, tenant_id)
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).with_for_update().first()
    row = db.query(SubscriptionPayment).filter(SubscriptionPayment.id == payment_id,
        SubscriptionPayment.tenant_id == tenant_id).with_for_update().first()
    if not tenant or not row:
        raise HTTPException(404, "Pago no encontrado.")
    if row.status == "pending" and row.provider_order_id:
        if paypal.is_captured(row.provider_order_id, row.reference, row.amount):
            row.paid_at = datetime.now(timezone.utc)
            process_expired_subscriptions(db, tenant_id=tenant_id, commit=False)
            db.refresh(tenant)
            _apply_payment(db, row, tenant, current_user.id)
    db.commit()
    db.refresh(tenant)
    return _payment_out(db, row, tenant)


@router.get("/my-subscription/payments", response_model=List[SubscriptionCheckoutResponse])
def list_subscription_payments(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> Any:
    tenant_id = company_id(current_user)
    check_company_admin(db, current_user, tenant_id)
    rows = db.query(SubscriptionPayment).filter(SubscriptionPayment.tenant_id == tenant_id).order_by(
        SubscriptionPayment.id.desc()).limit(50).all()
    return [_payment_out(db, r) for r in rows]


@router.post("/my-subscription/change-plan", response_model=SubscriptionCheckoutResponse)
def change_my_plan(
    change_in: ChangePlanRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> Any:
    """Compatibilidad: el cambio de plan de la propia empresa pasa siempre por el cobro."""
    return start_subscription_checkout(SubscriptionCheckoutRequest(kind="change_plan", plan_code=change_in.plan_code),
                                       db, current_user)


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
    current_user: User = Depends(require_superadmin),
) -> Any:
    """Renovación directa por el Superadmin (sin cobro). Las empresas renuevan con /my-subscription/checkout."""
    process_expired_subscriptions(db, tenant_id=tenant_id)
    tenant = db.query(Tenant).filter(Tenant.id == tenant_id).with_for_update().first()
    if not tenant:
        raise HTTPException(status_code=404, detail="Empresa no encontrada")
    _apply_renewal(db, tenant, current_user.id, via="superadmin")
    db.commit()
    db.refresh(tenant)
    return _build_tenant_detail(db, tenant)


@router.post("/my-subscription/renew", response_model=SubscriptionCheckoutResponse)
def renew_my_subscription(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> Any:
    """Compatibilidad: la renovación de la propia empresa pasa siempre por el cobro."""
    return start_subscription_checkout(SubscriptionCheckoutRequest(kind="renew"), db, current_user)


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
