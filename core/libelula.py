"""Libelula REST integration; test credentials must be supplied by the merchant.

Contract: Libelula Manual de Integracion v2.145, registrar / consultar_pagos.
No client callback is accepted as evidence of payment.
"""
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
import httpx
from fastapi import HTTPException
from core.config import settings
from core.commerce import money

API_URL = 'https://api.libelula.bo/rest/deuda'


def ensure_configured():
    if not settings.LIBELULA_TEST_APPKEY or not all(
        urlparse(url).scheme == 'https' and urlparse(url).netloc
        for url in (settings.PUBLIC_API_URL, settings.PUBLIC_WEB_URL)
    ):
        raise HTTPException(503, 'Pagos no disponibles: configura la llave de pruebas de Libelula y las URLs HTTPS')


def call(endpoint, payload):
    ensure_configured()
    try:
        with httpx.Client(timeout=30) as client:
            response = client.post(f'{API_URL}/{endpoint}', json={
                'appkey': settings.LIBELULA_TEST_APPKEY, **payload})
            response.raise_for_status()
            result = response.json()
        if not isinstance(result, dict) or str(result.get('error')) != '0':
            raise ValueError('Gateway rejected request')
        return result
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, 'No se pudo confirmar la operacion con Libelula; conserva tu numero de orden')


def register(order):
    result = call('registrar', {
        'identificador': order.transaction_ref,
        'email_cliente': order.user.email,
        'nombre_cliente': order.user.full_name or 'Cliente',
        'descripcion': f'FashionStore orden {order.id}',
        'moneda': 'BOB',
        'callback_url': f'{settings.PUBLIC_API_URL.rstrip("/")}/api/v1/payments/libelula/callback',
        'url_retorno': f'{settings.PUBLIC_WEB_URL.rstrip("/")}/payment/{order.id}',
        'lineas_detalle_deuda': [
            {'concepto': i.variant.product.name, 'cantidad': i.quantity,
             'costo_unitario': i.unit_price, 'codigo_producto': i.variant.sku}
            for i in order.items],
    })
    url = result.get('url_pasarela_pagos', '')
    if urlparse(url).scheme != 'https' or not result.get('id_transaccion'):
        raise HTTPException(502, 'Libelula no devolvio un enlace de pago HTTPS valido')
    return str(result['id_transaccion']), url


def is_paid(order):
    # Include a one-day margin for the provider's local timezone.
    result = call('consultar_pagos', {
        'fecha_inicial': (order.created_at - timedelta(days=1)).strftime('%Y-%m-%d'),
        'fecha_final': (datetime.now(timezone.utc) + timedelta(days=1)).strftime('%Y-%m-%d'),
    })
    for payment in result.get('datos', []):
        if (str(payment.get('id_transaccion')) == order.payment_id
            and str(payment.get('identificador')) == order.transaction_ref
            and payment.get('fecha_pago')
            and payment.get('moneda', 'BOB') == 'BOB'):
            try:
                return money(payment['monto_pagado']) == money(order.total_amount)
            except (KeyError, ValueError, ArithmeticError):
                return False
    return False
