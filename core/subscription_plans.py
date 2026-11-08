"""Initial demo pricing in USD and monthly subscription auto-cancellation lifecycle."""
from datetime import datetime, timedelta, timezone
from db.models.tenant import SubscriptionPlan, Subscription
from db.models.audit import AuditLog

BILLING_CYCLE_DAYS = 30

DEFAULT_PLANS = (
    dict(code='basic', name='Plan Básico', max_branches=2, has_ai_reports=False, price=29.99),
    dict(code='professional', name='Plan Profesional', max_branches=10, has_ai_reports=True, price=99.99),
    dict(code='enterprise', name='Plan Empresarial', max_branches=30, has_ai_reports=True, price=199.99),
)


def ensure_aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def calculate_period_end(start_dt: datetime | None = None) -> datetime:
    base = ensure_aware(start_dt) or datetime.now(timezone.utc)
    return base + timedelta(days=BILLING_CYCLE_DAYS)


def days_until_end(end_dt: datetime | None, now: datetime | None = None) -> int | None:
    aware_end = ensure_aware(end_dt)
    if aware_end is None:
        return None
    ref = ensure_aware(now) or datetime.now(timezone.utc)
    delta_seconds = (aware_end - ref).total_seconds()
    if delta_seconds <= 0:
        return 0
    return max(1, int((delta_seconds + 86399) // 86400))


def process_expired_subscriptions(db, tenant_id: int | None = None, commit: bool = True) -> int:
    """
    Revisa suscripciones activas cuyo ciclo mensual (`end_date`) ya terminó:
    - Si `end_date` era NULL en registros heredados, asigna 30 días de vigencia.
    - Si `end_date <= ahora`, aplica la cancelación automática de fin de mes
      (`status = 'cancelled'` si `cancel_at_period_end` es True, o `'expired'` si no se renovó).
    """
    now = datetime.now(timezone.utc)
    query = db.query(Subscription).filter(Subscription.status == 'active')
    if tenant_id is not None:
        query = query.filter(Subscription.tenant_id == tenant_id)

    active_subs = query.all()
    processed = 0
    backfilled = 0

    for sub in active_subs:
        if sub.end_date is None:
            start = ensure_aware(sub.start_date) or now
            base = start if start > now else now
            sub.end_date = base + timedelta(days=BILLING_CYCLE_DAYS)
            backfilled += 1
            continue

        end_aware = ensure_aware(sub.end_date)
        if end_aware is not None and end_aware <= now:
            auto_cancel = bool(getattr(sub, 'cancel_at_period_end', True))
            sub.status = 'cancelled' if auto_cancel else 'expired'
            sub.cancelled_at = now
            db.add(
                AuditLog(
                    tenant_id=sub.tenant_id,
                    user_id=None,
                    action='AUTO_CANCEL' if auto_cancel else 'EXPIRE',
                    entity_type='SUBSCRIPTION',
                    entity_id=str(sub.id),
                    details={
                        'plan_id': sub.plan_id,
                        'ended_at': end_aware.isoformat(),
                        'reason': (
                            'Cancelación automática al finalizar el mes contratado'
                            if auto_cancel
                            else 'Vencimiento mensual sin renovación'
                        ),
                    },
                )
            )
            processed += 1

    if processed > 0 or backfilled > 0:
        if commit:
            db.commit()
        else:
            db.flush()

    return processed


def ensure_plans(db):
    plans = {}
    for values in DEFAULT_PLANS:
        plan = db.query(SubscriptionPlan).filter_by(code=values['code']).first()
        if plan is None:
            plan = SubscriptionPlan(**values, has_offline_sync=True, is_active=True)
            db.add(plan)
        plans[values['code']] = plan
    db.flush()
    return plans
