from collections import defaultdict
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from fastapi import HTTPException
from api.deps import get_user_permissions
from db.models import Tenant, Branch, Product, ProductVariant, Inventory, Order, OrderItem, Category


def authorize(db, user, request, branch_id=None):
    tenant_id=user.tenant_id
    if tenant_id is None and user.role and user.role.name=='superadmin':
        try: tenant_id=int(request.headers.get('x-tenant-id',''))
        except ValueError: raise HTTPException(403,'Selecciona una empresa explícitamente.')
    tenant=db.get(Tenant,tenant_id) if tenant_id else None
    if not tenant or not tenant.is_active: raise HTTPException(403,'Empresa no disponible.')
    sub=tenant.active_subscription
    now=datetime.now(timezone.utc)
    def aware(d):return d.replace(tzinfo=d.tzinfo or timezone.utc)
    if not sub or not sub.plan or not sub.plan.is_active or not sub.plan.has_ai_reports or aware(sub.start_date)>now or (sub.end_date and aware(sub.end_date)<=now):
        raise HTTPException(403,'La suscripción vigente debe habilitar reportes IA (Plan Profesional).')
    perms=get_user_permissions(db,user)
    if 'sales:read' not in perms:raise HTTPException(403,'Permiso sales:read requerido.')
    if 'branches:write' not in perms:
        if not user.branch_id or (branch_id and branch_id!=user.branch_id):raise HTTPException(403,'Sucursal no autorizada.')
        branch_id=user.branch_id
    if branch_id and not db.query(Branch).filter_by(id=branch_id,tenant_id=tenant.id,is_active=True).first():raise HTTPException(403,'Sucursal no autorizada.')
    return tenant,branch_id


def sale_rows(db,tenant,branch,start,end,category=None):
    q=db.query(Order.created_at,Order.branch_id,Branch.name,Product.id,Product.name,Product.category_id,Category.name,OrderItem.quantity,OrderItem.unit_price).select_from(Order).join(OrderItem,OrderItem.order_id==Order.id).join(ProductVariant,ProductVariant.id==OrderItem.variant_id).join(Product,Product.id==ProductVariant.product_id).join(Branch,Branch.id==Order.branch_id).outerjoin(Category,Category.id==Product.category_id).filter(Order.tenant_id==tenant.id,OrderItem.tenant_id==tenant.id,ProductVariant.tenant_id==tenant.id,Product.tenant_id==tenant.id,Branch.tenant_id==tenant.id,Order.is_paid.is_(True),Order.payment_status=='paid',Order.created_at>=datetime.combine(start,time.min),Order.created_at<datetime.combine(end+timedelta(days=1),time.min))
    if branch:q=q.filter(Order.branch_id==branch)
    if category:q=q.filter(Product.category_id==category)
    rows=q.limit(50001).all()
    if len(rows)>50000:raise HTTPException(422,'Reduce el rango de fechas; se superaron 50.000 detalles.')
    return rows


def report(db,user,request,spec):
    tenant,branch=authorize(db,user,request,spec.branch_id)
    if spec.category_id and not db.query(Category).filter(Category.id==spec.category_id,Category.tenant_id==tenant.id).first():raise HTTPException(403,'Categoría no autorizada.')
    totals=defaultdict(Decimal)
    if spec.kind=='low_stock':
        q=db.query(Product.name,ProductVariant.sku,Branch.name,Inventory.stock).select_from(Inventory).join(ProductVariant,ProductVariant.id==Inventory.variant_id).join(Product,Product.id==ProductVariant.product_id).join(Branch,Branch.id==Inventory.branch_id).filter(Inventory.tenant_id==tenant.id,Product.tenant_id==tenant.id,ProductVariant.tenant_id==tenant.id,Branch.tenant_id==tenant.id,Product.is_active.is_(True))
        if branch:q=q.filter(Branch.id==branch)
        if spec.category_id:q=q.filter(Product.category_id==spec.category_id)
        rows=[[f'{p} · {sku} · {b}',stock] for p,sku,b,stock in q.order_by(Inventory.stock,Inventory.id).limit(20)]
        unit='Unidades disponibles'
    else:
        for dt,bid,bname,pid,pname,cid,cname,qty,price in sale_rows(db,tenant,branch,spec.start,spec.end,spec.category_id):
            if spec.kind=='monthly_branch':key=f'{dt:%Y-%m} · {bname}'
            elif spec.kind=='top_products':key=f'{pid} · {pname}'
            elif spec.kind=='weekly_sales':key=(dt.date()-timedelta(days=dt.weekday())).isoformat()
            elif spec.kind=='quarterly_branches':key=f'{dt.year}-T{(dt.month-1)//3+1} · {bname}'
            else:key=f'{cid or 0} · {cname or "Sin categoría"}'
            totals[key]+=Decimal(qty) if spec.kind=='top_products' else Decimal(str(price))*qty
        entries=sorted(totals.items(),key=lambda x:(-x[1],x[0]))[:10] if spec.kind=='top_products' else sorted(totals.items())
        rows=[[label,float(value)] for label,value in entries]
        unit='Unidades vendidas' if spec.kind=='top_products' else 'Ingresos (Bs)'
    return dict(company=tenant.name,spec=spec.model_dump(mode='json'),columns=['Grupo',unit],rows=rows,chart=spec.chart,
        note='Stock actual; las fechas no aplican.' if spec.kind=='low_stock' else 'Solo ventas pagadas. Fechas de operación registradas en el servidor.',generated_at=datetime.now(timezone.utc).isoformat())
