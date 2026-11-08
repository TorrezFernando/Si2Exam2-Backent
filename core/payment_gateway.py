"""Chooses the online payment provider. Each order keeps the provider it was created with."""
from core import libelula, paypal
from core.config import settings

PROVIDERS = {'PayPal': paypal, 'Libelula': libelula}


def active_provider():
    """PayPal when its credentials are set; otherwise the original Libelula integration."""
    return 'PayPal' if settings.PAYPAL_CLIENT_ID and settings.PAYPAL_CLIENT_SECRET else 'Libelula'


def gateway(provider):
    return PROVIDERS.get(provider)
