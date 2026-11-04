"""Opt-in generic provider query; commerce data stays in the in-memory fixture."""
import asyncio
import os
from datetime import date
from types import SimpleNamespace
import pytest
from test_analytics import analytics
from test_sync_integrity import shop
from api.routers.analytics import interpret
from schemas.analytics import PromptIn


@pytest.mark.skipif(os.environ.get('PHASE6_LIVE_GEMINI')!='1',reason='Live Gemini requires explicit PHASE6_LIVE_GEMINI=1.')
def test_real_gemini_function_call_and_scoped_report(analytics):
    day=date.today().isoformat()
    request=PromptIn(prompt=f'Top 10 productos vendidos del {day} al {day}, todas las categorías, sin filtro de sucursal. Gráfico de barras.')
    result=asyncio.run(interpret(request,SimpleNamespace(headers={}),analytics.db,analytics.user))
    assert 'clarification' not in result
    assert result['spec']['kind']=='top_products'
    assert result['spec']['start']==result['spec']['end']==day
    assert sum(row[1] for row in result['rows'])==2
