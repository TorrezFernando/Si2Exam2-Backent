"""Constructor de reportes personalizados.

El usuario elige la fuente, las columnas, los criterios de selección y el orden. Todo se traduce a
consultas SQLAlchemy sobre una lista blanca de fuentes y columnas (nunca SQL escrito por el usuario),
siempre restringidas a su empresa y, si no es multisucursal, a su sucursal.
"""
import csv
import enum
import html
import io
import smtplib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from email.message import EmailMessage
from typing import Callable, Literal
import sqlalchemy as sa
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field
from api.deps import get_user_permissions
from core.access import company_id
from core.config import settings
from db.models.branch import Branch, Inventory
from db.models.product import Category, Product, ProductVariant
from db.models.rbac import Role
from db.models.reservation import Reservation
from db.models.sale import Order, OrderItem
from db.models.user import User

MAX_ROWS = 5000
OPS = {
    'text': ['eq', 'ne', 'contains', 'starts'],
    'number': ['eq', 'ne', 'gt', 'gte', 'lt', 'lte'],
    'date': ['eq', 'gte', 'lte'],
    'bool': ['eq'],
}
OP_LABELS = {'eq': 'igual a', 'ne': 'distinto de', 'contains': 'contiene', 'starts': 'empieza con',
             'gt': 'mayor que', 'gte': 'mayor o igual', 'lt': 'menor que', 'lte': 'menor o igual'}


@dataclass
class Col:
    label: str
    type: str
    expr: object


@dataclass
class Source:
    label: str
    permissions: tuple
    columns: dict
    base: Callable
    tenant: object
    branch: object | None


def _sources() -> dict:
    customer = sa.orm.aliased(User)
    res_customer = sa.orm.aliased(User)
    return {
        'ventas': Source('Ventas', ('sales:read',), {
            'id': Col('N° venta', 'number', Order.id),
            'fecha': Col('Fecha', 'date', Order.created_at),
            'sucursal': Col('Sucursal', 'text', Branch.name),
            'cliente': Col('Cliente', 'text', customer.full_name),
            'email': Col('Email cliente', 'text', customer.email),
            'tipo': Col('Tipo de venta', 'text', Order.order_type),
            'metodo_pago': Col('Forma de pago', 'text', Order.payment_method),
            'estado_pago': Col('Estado del pago', 'text', Order.payment_status),
            'pagado': Col('Pagado', 'bool', Order.is_paid),
            'offline': Col('Registrada offline', 'bool', Order.is_offline_operation),
            'total': Col('Total (Bs)', 'number', Order.total_amount),
        }, lambda q: q.select_from(Order).join(Branch, Branch.id == Order.branch_id)
                      .outerjoin(customer, customer.id == Order.user_id),
            Order.tenant_id, Order.branch_id),
        'detalle_ventas': Source('Detalle de ventas (productos vendidos)', ('sales:read',), {
            'fecha': Col('Fecha', 'date', Order.created_at),
            'venta': Col('N° venta', 'number', Order.id),
            'sucursal': Col('Sucursal', 'text', Branch.name),
            'producto': Col('Producto', 'text', Product.name),
            'categoria': Col('Categoría', 'text', Category.name),
            'talla': Col('Talla', 'text', ProductVariant.size),
            'color': Col('Color', 'text', ProductVariant.color),
            'sku': Col('SKU', 'text', ProductVariant.sku),
            'cantidad': Col('Cantidad', 'number', OrderItem.quantity),
            'precio': Col('Precio unitario (Bs)', 'number', OrderItem.unit_price),
            'subtotal': Col('Subtotal (Bs)', 'number', OrderItem.quantity * OrderItem.unit_price),
            'pagado': Col('Venta pagada', 'bool', Order.is_paid),
        }, lambda q: q.select_from(OrderItem).join(Order, Order.id == OrderItem.order_id)
                      .join(Branch, Branch.id == Order.branch_id)
                      .join(ProductVariant, ProductVariant.id == OrderItem.variant_id)
                      .join(Product, Product.id == ProductVariant.product_id)
                      .outerjoin(Category, Category.id == Product.category_id),
            Order.tenant_id, Order.branch_id),
        'productos': Source('Productos', ('catalog:read', 'catalog:write', 'inventory:read'), {
            'id': Col('ID', 'number', Product.id),
            'nombre': Col('Producto', 'text', Product.name),
            'categoria': Col('Categoría', 'text', Category.name),
            'precio': Col('Precio base (Bs)', 'number', Product.base_price),
            'temporada': Col('Temporada', 'text', Product.season),
            'tela': Col('Tipo de tela', 'text', Product.fabric_type),
            'promocion': Col('Promoción (%)', 'number', Product.promotion_percent),
            'activo': Col('Activo', 'bool', Product.is_active),
        }, lambda q: q.select_from(Product).outerjoin(Category, Category.id == Product.category_id),
            Product.tenant_id, None),
        'inventario': Source('Inventario por sucursal', ('inventory:read', 'inventory:write'), {
            'sucursal': Col('Sucursal', 'text', Branch.name),
            'producto': Col('Producto', 'text', Product.name),
            'sku': Col('SKU', 'text', ProductVariant.sku),
            'talla': Col('Talla', 'text', ProductVariant.size),
            'color': Col('Color', 'text', ProductVariant.color),
            'stock': Col('Stock', 'number', Inventory.stock),
            'precio': Col('Precio base (Bs)', 'number', Product.base_price),
        }, lambda q: q.select_from(Inventory).join(Branch, Branch.id == Inventory.branch_id)
                      .join(ProductVariant, ProductVariant.id == Inventory.variant_id)
                      .join(Product, Product.id == ProductVariant.product_id),
            Inventory.tenant_id, Inventory.branch_id),
        'reservas': Source('Reservas', ('reservations:read', 'reservations:write', 'sales:read'), {
            'id': Col('N° reserva', 'number', Reservation.id),
            'fecha': Col('Fecha de registro', 'date', Reservation.created_at),
            'retiro': Col('Fecha de retiro', 'date', Reservation.pickup_date),
            'sucursal': Col('Sucursal', 'text', Branch.name),
            'cliente': Col('Cliente', 'text', res_customer.full_name),
            'email': Col('Email cliente', 'text', res_customer.email),
            'estado': Col('Estado', 'text', Reservation.status),
            'offline': Col('Registrada offline', 'bool', Reservation.is_offline_operation),
        }, lambda q: q.select_from(Reservation).join(Branch, Branch.id == Reservation.branch_id)
                      .outerjoin(res_customer, res_customer.id == Reservation.user_id),
            Reservation.tenant_id, Reservation.branch_id),
        'clientes': Source('Clientes', ('users:read', 'sales:read'), {
            'id': Col('ID', 'number', User.id),
            'nombre': Col('Nombre', 'text', User.full_name),
            'email': Col('Email', 'text', User.email),
            'telefono': Col('Teléfono', 'text', User.phone),
            'activo': Col('Activo', 'bool', User.is_active),
            'registro': Col('Fecha de registro', 'date', User.created_at),
        }, lambda q: q.select_from(User).join(Role, Role.id == User.role_id).where(Role.name == 'cliente'),
            User.tenant_id, None),
    }


class FilterIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    column: str = Field(max_length=40)
    op: Literal['eq', 'ne', 'contains', 'starts', 'gt', 'gte', 'lt', 'lte']
    value: str | float | int | bool | None = None


class OrderIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    column: str = Field(max_length=40)
    direction: Literal['asc', 'desc'] = 'asc'


class ReportRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    title: str | None = Field(None, max_length=120)
    source: str = Field(max_length=40)
    columns: list[str] = Field(min_length=1, max_length=20)
    filters: list[FilterIn] = Field(default_factory=list, max_length=15)
    order_by: list[OrderIn] = Field(default_factory=list, max_length=5)
    limit: int = Field(1000, ge=1, le=MAX_ROWS)


class EmailRequest(ReportRequest):
    to: list[str] = Field(min_length=1, max_length=5)
    format: Literal['xlsx', 'pdf', 'html', 'csv'] = 'pdf'
    message: str | None = Field(None, max_length=1000)


def _allowed(source: Source, perms: set) -> bool:
    return bool(perms & set(source.permissions))


def metadata(db, user) -> list[dict]:
    company_id(user)  # solo usuarios de empresa
    perms = get_user_permissions(db, user)
    return [{'key': key, 'label': src.label,
             'columns': [{'key': k, 'label': c.label, 'type': c.type, 'ops': [{'key': o, 'label': OP_LABELS[o]} for o in OPS[c.type]]}
                         for k, c in src.columns.items()]}
            for key, src in _sources().items() if _allowed(src, perms)]


def _parse(col: Col, value):
    if col.type == 'number':
        try:
            return float(value)
        except (TypeError, ValueError):
            raise HTTPException(422, f'"{col.label}" requiere un número.')
    if col.type == 'date':
        try:
            return date.fromisoformat(str(value)[:10])
        except ValueError:
            raise HTTPException(422, f'"{col.label}" requiere una fecha AAAA-MM-DD.')
    if col.type == 'bool':
        return value in (True, 1, 'true', 'True', 'si', 'sí', 'Sí', '1')
    if value is None or str(value).strip() == '':
        raise HTTPException(422, f'Indica un valor para "{col.label}".')
    return str(value).strip()


def _condition(col: Col, op: str, value):
    if op not in OPS[col.type]:
        raise HTTPException(422, f'El criterio "{OP_LABELS.get(op, op)}" no aplica a "{col.label}".')
    value = _parse(col, value)
    expr = col.expr
    if col.type == 'text':
        text_expr = sa.func.lower(sa.cast(expr, sa.String))
        needle = value.lower()
        return {'eq': text_expr == needle, 'ne': text_expr != needle,
                'contains': text_expr.contains(needle, autoescape=True),
                'starts': text_expr.startswith(needle, autoescape=True)}[op]
    if col.type == 'date':
        start = datetime.combine(value, time.min)
        end = start + timedelta(days=1)
        return {'eq': sa.and_(expr >= start, expr < end), 'gte': expr >= start, 'lte': expr < end}[op]
    if col.type == 'bool':
        return expr.is_(True) if value else sa.or_(expr.is_(False), expr.is_(None))
    return {'eq': expr == value, 'ne': expr != value, 'gt': expr > value,
            'gte': expr >= value, 'lt': expr < value, 'lte': expr <= value}[op]


def _display(value):
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, datetime):
        return value.strftime('%Y-%m-%d %H:%M')
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, float):
        return round(value, 2)
    if isinstance(value, bool):
        return 'Sí' if value else 'No'
    return value


def run(db, user, spec: ReportRequest) -> dict:
    tenant = company_id(user)
    sources = _sources()
    source = sources.get(spec.source)
    perms = get_user_permissions(db, user)
    if not source or not _allowed(source, perms):
        raise HTTPException(403, 'No tienes acceso a esta fuente de datos.')
    unknown = [c for c in spec.columns + [f.column for f in spec.filters] + [o.column for o in spec.order_by]
               if c not in source.columns]
    if unknown:
        raise HTTPException(422, f'Columnas no disponibles: {", ".join(sorted(set(unknown)))}.')
    columns = list(dict.fromkeys(spec.columns))
    query = source.base(sa.select(*[source.columns[c].expr.label(c) for c in columns]))
    query = query.where(source.tenant == tenant)
    applied = []
    if source.branch is not None and 'branches:write' not in perms:
        if not user.branch_id:
            raise HTTPException(403, 'No tienes sucursal asignada.')
        query = query.where(source.branch == user.branch_id)
        applied.append('Sucursal asignada al usuario')
    for f in spec.filters:
        col = source.columns[f.column]
        query = query.where(_condition(col, f.op, f.value))
        applied.append(f'{col.label} {OP_LABELS[f.op]} {f.value}')
    for o in spec.order_by:
        expr = source.columns[o.column].expr
        query = query.order_by(expr.desc() if o.direction == 'desc' else expr.asc())
    rows = db.execute(query.limit(spec.limit + 1)).all()
    truncated = len(rows) > spec.limit
    rows = rows[:spec.limit]
    return {
        'title': spec.title or f'Reporte de {source.label.lower()}',
        'source': source.label,
        'columns': [{'key': c, 'label': source.columns[c].label, 'type': source.columns[c].type} for c in columns],
        'rows': [[_display(v) for v in r] for r in rows],
        'filters': applied,
        'order_by': [f'{source.columns[o.column].label} ({"descendente" if o.direction == "desc" else "ascendente"})' for o in spec.order_by],
        'count': len(rows), 'truncated': truncated,
        'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M'),
    }


# ─── Exportación ──────────────────────────────────────────────────────────────

def to_csv(report) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([c['label'] for c in report['columns']])
    writer.writerows(report['rows'])
    return ('﻿' + buffer.getvalue()).encode('utf-8')


def to_xlsx(report) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    wb = Workbook()
    ws = wb.active
    ws.title = 'Reporte'
    ws.append([report['title']]); ws['A1'].font = Font(bold=True, size=14)
    ws.append([f"Fuente: {report['source']} · Generado: {report['generated_at']} · Registros: {report['count']}"])
    if report['filters']:
        ws.append(['Criterios: ' + '; '.join(report['filters'])])
    if report['order_by']:
        ws.append(['Orden: ' + ', '.join(report['order_by'])])
    ws.append([])
    ws.append([c['label'] for c in report['columns']])
    header_row = ws.max_row
    for cell in ws[header_row]:
        cell.font = Font(bold=True, color='FFFFFF'); cell.fill = PatternFill('solid', fgColor='4F46E5')
    for row in report['rows']:
        ws.append([v if not isinstance(v, str) or not v[:1] in '=+-@' else "'" + v for v in row])
    for idx, col in enumerate(report['columns'], start=1):
        width = max([len(str(col['label']))] + [len(str(r[idx - 1])) for r in report['rows'][:200]]) + 2
        ws.column_dimensions[ws.cell(row=header_row, column=idx).column_letter].width = min(width, 45)
    out = io.BytesIO(); wb.save(out)
    return out.getvalue()


def _latin(text) -> str:
    return str(text).encode('latin-1', 'replace').decode('latin-1')


def to_pdf(report) -> bytes:
    from fpdf import FPDF
    pdf = FPDF(orientation='L' if len(report['columns']) > 5 else 'P', format='A4')
    pdf.set_auto_page_break(True, margin=12)
    pdf.add_page()
    pdf.set_font('Helvetica', 'B', 14); pdf.cell(0, 9, _latin(report['title']), new_x='LMARGIN', new_y='NEXT')
    pdf.set_font('Helvetica', '', 9)
    pdf.multi_cell(0, 5, _latin(f"Fuente: {report['source']} - Generado: {report['generated_at']} - Registros: {report['count']}"), new_x='LMARGIN', new_y='NEXT')
    if report['filters']:
        pdf.multi_cell(0, 5, _latin('Criterios: ' + '; '.join(report['filters'])), new_x='LMARGIN', new_y='NEXT')
    if report['order_by']:
        pdf.multi_cell(0, 5, _latin('Orden: ' + ', '.join(report['order_by'])), new_x='LMARGIN', new_y='NEXT')
    pdf.ln(2)
    width = (pdf.w - pdf.l_margin - pdf.r_margin) / len(report['columns'])
    pdf.set_font('Helvetica', 'B', 8); pdf.set_fill_color(79, 70, 229); pdf.set_text_color(255, 255, 255)
    for col in report['columns']:
        pdf.cell(width, 7, _latin(col['label'])[:30], border=1, fill=True)
    pdf.ln(); pdf.set_text_color(0, 0, 0); pdf.set_font('Helvetica', '', 8)
    for i, row in enumerate(report['rows']):
        pdf.set_fill_color(243, 244, 246) if i % 2 else pdf.set_fill_color(255, 255, 255)
        for value in row:
            pdf.cell(width, 6, _latin('' if value is None else value)[:38], border=1, fill=True)
        pdf.ln()
    return bytes(pdf.output())


def to_html(report) -> bytes:
    esc = lambda v: html.escape('' if v is None else str(v))
    head = ''.join(f'<th>{esc(c["label"])}</th>' for c in report['columns'])
    body = ''.join('<tr>' + ''.join(f'<td>{esc(v)}</td>' for v in row) + '</tr>' for row in report['rows'])
    criteria = ''.join(f'<li>{esc(f)}</li>' for f in report['filters']) or '<li>Sin criterios</li>'
    order = esc(', '.join(report['order_by']) or 'Sin orden específico')
    page = f"""<!doctype html><html lang="es"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(report['title'])}</title><style>
body{{font-family:Segoe UI,Arial,sans-serif;margin:24px;color:#111827}}h1{{margin:0 0 4px;color:#4f46e5}}
.meta{{color:#6b7280;font-size:13px}}table{{border-collapse:collapse;width:100%;margin-top:16px;font-size:13px}}
th{{background:#4f46e5;color:#fff;text-align:left}}th,td{{border:1px solid #e5e7eb;padding:6px 8px}}tr:nth-child(even) td{{background:#f9fafb}}
</style></head><body><h1>{esc(report['title'])}</h1>
<p class="meta">FashionStore · Fuente: {esc(report['source'])} · Generado: {esc(report['generated_at'])} · Registros: {report['count']}{' (truncado)' if report['truncated'] else ''}</p>
<p class="meta"><strong>Criterios:</strong></p><ul class="meta">{criteria}</ul><p class="meta"><strong>Orden:</strong> {order}</p>
<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></body></html>"""
    return page.encode('utf-8')


FORMATS = {
    'xlsx': (to_xlsx, 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet', 'xlsx'),
    'pdf': (to_pdf, 'application/pdf', 'pdf'),
    'html': (to_html, 'text/html; charset=utf-8', 'html'),
    'csv': (to_csv, 'text/csv; charset=utf-8', 'csv'),
}


def export(report, fmt: str) -> tuple[bytes, str, str]:
    if fmt not in FORMATS:
        raise HTTPException(404, 'Formato no soportado. Usa xlsx, pdf, html o csv.')
    builder, media, ext = FORMATS[fmt]
    return builder(report), media, ext


def send_email(report, fmt: str, recipients: list[str], message: str | None, sender_name: str) -> None:
    if not settings.SMTP_HOST or not settings.SMTP_FROM:
        raise HTTPException(503, 'El envío por correo no está configurado en el servidor (SMTP_HOST y SMTP_FROM).')
    import re
    for address in recipients:
        if not re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', address):
            raise HTTPException(422, f'Correo inválido: {address}')
    content, media, ext = export(report, fmt)
    mail = EmailMessage()
    mail['Subject'] = f"FashionStore · {report['title']}"
    mail['From'] = settings.SMTP_FROM
    mail['To'] = ', '.join(recipients)
    mail.set_content(f"{message or 'Adjunto el reporte solicitado.'}\n\n"
                     f"Fuente: {report['source']} · Registros: {report['count']} · Enviado por {sender_name}.")
    mail.add_alternative(to_html(report).decode('utf-8'), subtype='html')
    maintype, subtype = media.split(';')[0].split('/')
    mail.add_attachment(content, maintype=maintype, subtype=subtype, filename=f'reporte.{ext}')
    try:
        with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=30) as smtp:
            smtp.starttls()
            if settings.SMTP_USER:
                smtp.login(settings.SMTP_USER, settings.SMTP_PASSWORD)
            smtp.send_message(mail)
    except (smtplib.SMTPException, OSError):
        raise HTTPException(502, 'No se pudo enviar el correo. Revisa la configuración SMTP del servidor.')
