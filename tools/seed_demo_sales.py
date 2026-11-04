"""Idempotent synthetic sales for the last six complete months, per company."""
import argparse
import calendar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
import random
import sqlite3
import sys
from zoneinfo import ZoneInfo

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from sqlalchemy.engine import make_url
from db.database import SessionLocal
from db.models import Tenant, Branch, Product, ProductVariant, Inventory, User, Role, Order, OrderItem
from db.models.operations import InventoryMovement
from core.config import settings
from core.security import get_password_hash
from core.commerce import money


def months_before(as_of, count=6):
    end=as_of.replace(day=1)
    n=end.year*12+end.month-1-count
    return date(n//12,n%12+1,1),end


def generate(db,as_of,tenant_ids):
    start,end=months_before(as_of)
    results=[]
    client_role=db.query(Role).filter_by(name='cliente').first()
    if not client_role:raise ValueError('Falta el rol cliente.')
    for tenant_id in tenant_ids:
        tenant=db.get(Tenant,tenant_id)
        if not tenant:raise ValueError('Empresa no encontrada.')
        # Lock the company to serialize concurrent invocations on PostgreSQL.
        db.query(Tenant).filter_by(id=tenant_id).with_for_update().one()
        choices=[]
        for branch in db.query(Branch).filter_by(tenant_id=tenant_id,is_active=True).order_by(Branch.id):
            variants=db.query(ProductVariant).join(Product).join(Inventory,Inventory.variant_id==ProductVariant.id).filter(
                ProductVariant.tenant_id==tenant_id,Product.tenant_id==tenant_id,Product.is_active.is_(True),
                Inventory.tenant_id==tenant_id,Inventory.branch_id==branch.id).order_by(ProductVariant.id).all()
            if variants:choices.append((branch,variants))
        if not choices:raise ValueError(f'Empresa {tenant_id}: falta catálogo e inventario en sucursal activa.')
        email=f'demo-sales-t{tenant_id}@example.invalid'
        customer=db.query(User).filter_by(email=email,tenant_id=tenant_id).first()
        if not customer:
            customer=User(tenant_id=tenant_id,email=email,full_name='Cliente de ventas ficticias DEMO',is_active=False,
                role_id=client_role.id,hashed_password=get_password_hash(__import__('secrets').token_urlsafe(32)))
            db.add(customer);db.flush()
        added=0;existing=0;day=start;monthly={}
        while day<end:
            rng=random.Random(f'demo-sales-v1:{tenant_id}:{day.isoformat()}')
            tickets=rng.randint(2,5) if day.weekday()<5 else rng.randint(3,6)
            month=day.strftime('%Y-%m');monthly.setdefault(month,dict(orders=0,units=0,total=Decimal('0')))
            for ticket in range(tickets):
                key=f'DEMO-SALES-V1:t{tenant_id}:{day}:{ticket}'
                order=db.query(Order).filter_by(request_key=key).first()
                if order:
                    existing+=1
                    line_units=sum(item.quantity for item in order.items)
                else:
                    branch,variants=rng.choice(choices)
                    selected=rng.sample(variants,min(len(variants),rng.randint(1,3)))
                    lines=[]
                    for variant in selected:
                        base=variant.price_override if variant.price_override is not None else variant.product.base_price
                        price=money(Decimal(str(base))*Decimal(rng.randint(85,115))/100)
                        lines.append((variant,rng.randint(1,3),price))
                    local=datetime(day.year,day.month,day.day,rng.randint(9,19),rng.randint(0,59),tzinfo=ZoneInfo('America/La_Paz'))
                    timestamp=local.astimezone(timezone.utc)
                    order=Order(tenant_id=tenant_id,user_id=customer.id,branch_id=branch.id,order_type='presencial',
                        payment_method='efectivo',payment_status='paid',is_paid=True,payment_provider='DEMO_SYNTHETIC',
                        total_amount=float(sum((qty*price for _,qty,price in lines),Decimal(0))),request_key=key,
                        transaction_ref=key,created_at=timestamp)
                    db.add(order);db.flush()
                    for variant,qty,price in lines:
                        db.add(OrderItem(tenant_id=tenant_id,order_id=order.id,variant_id=variant.id,quantity=qty,unit_price=float(price)))
                        # Synthetic receipt and sale balance: present-day stock is preserved.
                        for delta,kind in [(qty,'demo_receipt'),(-qty,'demo_sale')]:
                            db.add(InventoryMovement(tenant_id=tenant_id,branch_id=branch.id,variant_id=variant.id,
                                quantity=delta,kind=kind,user_id=customer.id,reference=key,created_at=timestamp))
                    line_units=sum(qty for _,qty,_ in lines)
                    added+=1
                monthly[month]['orders']+=1
                monthly[month]['units']+=line_units
                monthly[month]['total']+=money(order.total_amount)
            day+=timedelta(days=1)
        results.append(dict(tenant_id=tenant_id,name=tenant.name,added=added,existing=existing,
            months={k:{**v,'total':float(v['total'])} for k,v in monthly.items()}))
    return results


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--as-of',type=date.fromisoformat,required=True)
    parser.add_argument('--tenants',type=int,nargs='+',required=True)
    args=parser.parse_args()
    url=make_url(settings.SQLALCHEMY_DATABASE_URI)
    if url.get_backend_name()!='sqlite':raise RuntimeError('Esta carga local requiere SQLite; configura una migración supervisada para otra base.')
    root=Path(__file__).resolve().parents[1];source=Path(url.database).resolve()
    if not source.is_relative_to(root) or not source.is_file():raise RuntimeError('Base local fuera del proyecto.')
    backups=root/'.backups';backups.mkdir(exist_ok=True)
    backup=backups/f'before_demo_sales_{datetime.now().strftime("%Y%m%d_%H%M%S")}.db'
    with sqlite3.connect(source) as src,sqlite3.connect(backup) as dst:src.backup(dst)
    with SessionLocal() as db:
        try:
            results=generate(db,args.as_of,args.tenants);db.commit()
        except Exception:db.rollback();raise
    import json
    print(json.dumps(results,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
