"""Initial demo pricing in USD; existing commercial settings are preserved."""
from db.models.tenant import SubscriptionPlan

DEFAULT_PLANS = (
    dict(code='basic', name='Plan Básico', max_branches=2, has_ai_reports=False, price=29.99),
    dict(code='professional', name='Plan Profesional', max_branches=10, has_ai_reports=True, price=99.99),
    dict(code='enterprise', name='Plan Empresarial', max_branches=30, has_ai_reports=True, price=199.99),
)


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
