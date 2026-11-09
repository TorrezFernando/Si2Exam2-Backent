"""PayPal Orders v2 integration (sandbox by default).

Same contract as core.libelula: ensure_configured / register / is_paid.
The client redirect is never accepted as evidence of payment: the server reads
the order from PayPal, captures it when approved and checks reference and amount.
PayPal does not settle BOB, so the order total is converted to USD with
PAYPAL_BOB_PER_USD; keep that rate fixed while orders are pending.
"""
from decimal import Decimal, ROUND_HALF_UP
from urllib.parse import urlparse
import httpx
from fastapi import HTTPException
from core.config import settings

API_URLS = {'sandbox': 'https://api-m.sandbox.paypal.com', 'live': 'https://api-m.paypal.com'}
CURRENCY = 'USD'


def base_url():
    return API_URLS.get(settings.PAYPAL_ENV, API_URLS['sandbox'])


def ensure_configured():
    if (not settings.PAYPAL_CLIENT_ID or not settings.PAYPAL_CLIENT_SECRET
            or settings.PAYPAL_ENV not in API_URLS or settings.PAYPAL_BOB_PER_USD <= 0
            or not all(urlparse(url).scheme == 'https' and urlparse(url).netloc
                       for url in (settings.PUBLIC_API_URL, settings.PUBLIC_WEB_URL))):
        raise HTTPException(503, 'Pagos no disponibles: configura PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET y las URLs HTTPS')


def usd_amount(order):
    value = Decimal(str(order.total_amount)) / Decimal(str(settings.PAYPAL_BOB_PER_USD))
    return value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def _fail():
    raise HTTPException(502, 'No se pudo confirmar la operacion con PayPal; conserva tu numero de orden')


def _token(client):
    response = client.post(f'{base_url()}/v1/oauth2/token', data={'grant_type': 'client_credentials'},
                           auth=(settings.PAYPAL_CLIENT_ID, settings.PAYPAL_CLIENT_SECRET))
    response.raise_for_status()
    return response.json()['access_token']


def call(method, path, payload=None, request_id=None, allow=()):
    """Authenticated request; statuses listed in `allow` return None instead of failing."""
    ensure_configured()
    try:
        with httpx.Client(timeout=30) as client:
            headers = {'Authorization': f'Bearer {_token(client)}', 'Content-Type': 'application/json'}
            if request_id:
                headers['PayPal-Request-Id'] = request_id  # idempotent retries
            response = client.request(method, f'{base_url()}{path}', json=payload, headers=headers)
            if response.status_code in allow:
                return None
            response.raise_for_status()
            result = response.json()
        if not isinstance(result, dict):
            raise ValueError('Unexpected PayPal response')
        return result
    except (httpx.HTTPError, ValueError, KeyError):
        _fail()


def create_checkout(reference, amount_usd, description, return_url, cancel_url):
    """Crea una orden PayPal en USD y devuelve (id, enlace de aprobación HTTPS)."""
    result = call('POST', '/v2/checkout/orders', {
        'intent': 'CAPTURE',
        'purchase_units': [{
            'reference_id': reference[:256],
            'custom_id': reference,
            'invoice_id': reference,
            'description': description[:127],
            'amount': {'currency_code': CURRENCY, 'value': f'{Decimal(str(amount_usd)):.2f}'},
        }],
        'payment_source': {'paypal': {'experience_context': {
            'brand_name': 'FashionStore',
            'user_action': 'PAY_NOW',
            'shipping_preference': 'NO_SHIPPING',
            'return_url': return_url,
            'cancel_url': cancel_url,
        }}},
    }, request_id=f'create-{reference}')
    url = next((link.get('href', '') for link in result.get('links', [])
                if link.get('rel') in ('payer-action', 'approve')), '')
    if urlparse(url).scheme != 'https' or not result.get('id'):
        raise HTTPException(502, 'PayPal no devolvio un enlace de pago HTTPS valido')
    return str(result['id']), url


def register(order):
    web = settings.PUBLIC_WEB_URL.rstrip('/')
    return create_checkout(order.transaction_ref, usd_amount(order),
                           f'FashionStore orden {order.id} (Bs {order.total_amount:.2f})',
                           f'{web}/payment/{order.id}', f'{web}/payment/{order.id}?cancelled=1')


def _captured(result, reference, expected):
    units = result.get('purchase_units') or []
    if result.get('status') != 'COMPLETED' or len(units) != 1:
        return False
    unit = units[0]
    if unit.get('custom_id') not in (None, reference):
        return False
    for capture in (unit.get('payments') or {}).get('captures') or []:
        amount = capture.get('amount') or {}
        if (capture.get('status') == 'COMPLETED' and amount.get('currency_code') == CURRENCY
                and amount.get('value') == expected
                and capture.get('custom_id', reference) == reference):
            return True
    return False


def is_captured(provider_id, reference, amount_usd):
    """Lee la orden en PayPal, la captura si el comprador la aprobó y valida referencia e importe."""
    path = f'/v2/checkout/orders/{provider_id}'
    result = call('GET', path)
    if result.get('status') == 'APPROVED':
        # A concurrent capture (422) is re-read below.
        captured = call('POST', f'{path}/capture', {}, request_id=f'capture-{reference}', allow=(422,))
        result = captured if captured and captured.get('status') == 'COMPLETED' else call('GET', path)
    if result.get('status') == 'COMPLETED' and not (result.get('purchase_units') or [{}])[0].get('payments'):
        result = call('GET', path)
    return _captured(result, reference, f'{Decimal(str(amount_usd)):.2f}')


def is_paid(order):
    return is_captured(order.payment_id, order.transaction_ref, usd_amount(order))
