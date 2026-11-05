import asyncio
import base64
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from test_sync_integrity import shop
from api import deps
from api.routers import ai_tryon
from core import ai_service


def photo():
    buf=BytesIO();Image.new('RGB',(10,10),'blue').save(buf,format='PNG');return buf.getvalue()


def api(shop,monkeypatch):
    monkeypatch.setattr(ai_tryon.settings,'AI_TRYON_ENABLED',True)
    app=FastAPI();app.include_router(ai_tryon.router,prefix='/tryon')
    app.dependency_overrides[deps.get_db]=lambda:shop.db
    app.dependency_overrides[deps.get_current_active_user]=lambda:shop.user
    return TestClient(app)


def test_exact_variant_size_color_and_image_are_forwarded(shop,monkeypatch):
    client=api(shop,monkeypatch)
    shop.variant.image_url='data:image/png;base64,'+base64.b64encode(photo()).decode()
    shop.db.commit()
    generator=AsyncMock(return_value=photo())
    monkeypatch.setattr(ai_tryon,'generate_tryon',generator)
    monkeypatch.setattr(ai_tryon,'save_tryon_result',lambda *args:'/uploads/result.png')
    result=client.post('/tryon',data={'product_id':shop.variant.product_id,'variant_id':shop.variant.id},files={'user_photo':('person.png',photo(),'image/png')})
    assert result.status_code==200,result.text
    assert result.json()['selected_size']=='M'
    assert generator.call_args.kwargs['selected_size']=='M'
    assert generator.call_args.kwargs['selected_color']=='Azul'
    assert client.post('/tryon',data={'product_id':shop.variant.product_id,'variant_id':99999},files={'user_photo':('x.png',photo(),'image/png')}).status_code==404


def test_invalid_photos_do_not_call_provider(shop,monkeypatch):
    client=api(shop,monkeypatch)
    shop.variant.image_url='data:image/png;base64,'+base64.b64encode(photo()).decode();shop.db.commit()
    generator=AsyncMock();monkeypatch.setattr(ai_tryon,'generate_tryon',generator)
    result=client.post('/tryon',data={'product_id':shop.variant.product_id,'variant_id':shop.variant.id},files={'user_photo':('x.png',b'not an image','image/png')})
    assert result.status_code==400
    generator.assert_not_called()


def test_empty_provider_response_is_recoverable_and_prompt_contains_size(monkeypatch):
    captured={}
    def generate(**kwargs):
        captured.update(kwargs);return SimpleNamespace(candidates=[])
    monkeypatch.setattr(ai_service,'_get_api_keys',lambda:['private-test-key'])
    monkeypatch.setattr(ai_service.genai,'Client',lambda **kwargs:SimpleNamespace(models=SimpleNamespace(generate_content=generate),close=lambda:None))
    import pytest
    with pytest.raises(ValueError,match='no generó una imagen'):
        asyncio.run(ai_service.generate_tryon(photo(),photo(),selected_size='XL',selected_color='Negro'))
    assert 'XL' in captured['contents'][0] and 'Negro' in captured['contents'][0]
    assert 'not a measurement or fit guarantee' in captured['contents'][0]
