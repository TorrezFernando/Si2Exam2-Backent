"""PayPal integration without network: the provider API is replaced by `call` fakes."""
from types import SimpleNamespace
import pytest
from fastapi import HTTPException
from core import paypal, payment_gateway
from core.config import settings


@pytest.fixture
def configured(monkeypatch):
    for key, value in dict(PAYPAL_CLIENT_ID='test-client', PAYPAL_CLIENT_SECRET='test-secret', PAYPAL_ENV='sandbox',
                           PAYPAL_BOB_PER_USD=6.96, PUBLIC_API_URL='https://api.example.test',
                           PUBLIC_WEB_URL='https://web.example.test').items():
        monkeypatch.setattr(settings, key, value)


def order(**extra):
    return SimpleNamespace(**dict(dict(id=7, total_amount=139.2, transaction_ref='FS-ref', payment_id='PP-1'), **extra))


def completed(value='20.00', ref='FS-ref'):
    return {'status': 'COMPLETED', 'purchase_units': [{'custom_id': ref, 'payments': {'captures': [
        {'status': 'COMPLETED', 'amount': {'currency_code': 'USD', 'value': value}}]}}]}


def test_provider_selection_and_configuration(monkeypatch, configured):
    assert payment_gateway.active_provider() == 'PayPal'
    paypal.ensure_configured()
    monkeypatch.setattr(settings, 'PAYPAL_CLIENT_SECRET', '')
    assert payment_gateway.active_provider() == 'Libelula'
    with pytest.raises(HTTPException) as e:
        paypal.ensure_configured()
    assert e.value.status_code == 503


def test_register_converts_bob_to_usd_and_returns_approval_link(monkeypatch, configured):
    sent = {}
    def fake(method, path, payload=None, request_id=None, allow=()):
        sent.update(method=method, path=path, payload=payload, request_id=request_id)
        return {'id': 'PP-1', 'links': [{'rel': 'payer-action', 'href': 'https://www.sandbox.paypal.com/checkoutnow?token=PP-1'}]}
    monkeypatch.setattr(paypal, 'call', fake)
    assert paypal.register(order()) == ('PP-1', 'https://www.sandbox.paypal.com/checkoutnow?token=PP-1')
    unit = sent['payload']['purchase_units'][0]
    assert unit['amount'] == {'currency_code': 'USD', 'value': '20.00'} and unit['custom_id'] == 'FS-ref'
    assert sent['request_id'] == 'create-FS-ref'
    assert sent['payload']['payment_source']['paypal']['experience_context']['return_url'] == 'https://web.example.test/payment/7'


def test_register_rejects_missing_https_link(monkeypatch, configured):
    monkeypatch.setattr(paypal, 'call', lambda *a, **k: {'id': 'PP-1', 'links': [{'rel': 'payer-action', 'href': 'http://x'}]})
    with pytest.raises(HTTPException):
        paypal.register(order())


def test_approved_order_is_captured_and_amount_checked(monkeypatch, configured):
    calls = []
    def fake(method, path, payload=None, request_id=None, allow=()):
        calls.append((method, path))
        return {'status': 'APPROVED'} if method == 'GET' else completed()
    monkeypatch.setattr(paypal, 'call', fake)
    assert paypal.is_paid(order())
    assert calls == [('GET', '/v2/checkout/orders/PP-1'), ('POST', '/v2/checkout/orders/PP-1/capture')]


@pytest.mark.parametrize('result', [
    {'status': 'PAYER_ACTION_REQUIRED'},
    completed(value='19.99'),
    completed(ref='FS-other'),
])
def test_unapproved_or_mismatched_payment_is_not_paid(monkeypatch, configured, result):
    monkeypatch.setattr(paypal, 'call', lambda *a, **k: result)
    assert not paypal.is_paid(order())
