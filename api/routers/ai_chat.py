from typing import Any, List
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
import asyncio
from api.deps import get_current_user_optional, get_db
from db.models.user import User
from db.models.tenant import Tenant
from sqlalchemy.orm import Session
from db.models.product import Product
from core.storefront import selected_store

router = APIRouter()

class ChatMessage(BaseModel):
    role: str
    content: str = Field(min_length=1, max_length=4000)

class ChatRequest(BaseModel):
    messages: List[ChatMessage] = Field(min_length=1, max_length=30)
    store: str | None = None

class ChatResponse(BaseModel):
    response: str


def _fallback_shopper_response(tenant_name: str, products: list[Product], user_message: str) -> str:
    msg = user_message.lower().strip()
    if not products:
        return f"¡Hola! Bienvenido a {tenant_name} ✨. Actualmente no tenemos productos activos cargados en esta tienda."
    matches = [
        p for p in products
        if any(
            token in f"{p.name} {p.description or ''} {p.category.name if p.category else ''}".lower()
            for token in msg.split()
            if len(token) >= 3
        )
    ]
    featured = (matches or products)[:4]
    items_txt = ", ".join(
        f"{p.name} (${p.base_price:.0f})" for p in featured
    )
    return (
        f"¡Hola! Soy tu Personal Shopper de {tenant_name} ✨👗. "
        f"Te recomiendo estas opciones destacadas de nuestro catálogo: {items_txt}. "
        "Cuéntame qué estilo, ocasión o prenda buscas y te asesoro al instante."
    )


@router.post("", response_model=ChatResponse)
async def chat_with_ai(
    request: ChatRequest,
    http_request: Request,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_optional)
) -> Any:
    """
    Endpoint para chatear con el Personal Shopper IA.
    """
    from core.config import settings
    from google import genai
    
    tenant = selected_store(db, current_user, request.store, http_request)
    if tenant is None and current_user and current_user.role and current_user.role.name == "superadmin":
        tenant = db.query(Tenant).filter_by(is_active=True).order_by(Tenant.id).first()
    if tenant is None:
        return ChatResponse(response='Selecciona una tienda o inicia sesión para consultar su catálogo.')
    products = db.query(Product).filter(Product.is_active.is_(True), Product.tenant_id == tenant.id).limit(50).all()
    catalog_context = "Catálogo de productos disponibles:\n"
    for p in products:
        cat = p.category.name if p.category else "Otros"
        catalog_context += f"- {p.name} ({cat}): ${p.base_price}. {p.description or ''}\n"

    system_instruction = (
        f"Eres un Personal Shopper experto en moda para {tenant.name}. "
        "Tu objetivo es ayudar al usuario a encontrar la ropa perfecta de nuestro catálogo. "
        "Sé amable, persuasivo y da consejos de estilo. "
        "Usa emojis. Mantén tus respuestas concisas.\n\n"
        f"{catalog_context}"
    )

    conversation = ""
    for msg in request.messages:
        conversation += f"{msg.role}: {msg.content}\n"
    prompt = f"{system_instruction}\n\nHistorial de chat:\n{conversation}\nAI:"

    api_keys = list(dict.fromkeys(
        k for k in [
            settings.GEMINI_API_KEY_2,
            settings.GEMINI_API_KEY_1,
            settings.GEMINI_API_KEY_3,
            settings.GEMINI_API_KEY_4,
        ] if k
    ))
    models_to_try = list(dict.fromkeys(
        m for m in [
            settings.GEMINI_TEXT_MODEL,
            "gemini-2.5-flash",
            "gemini-2.0-flash",
            "gemini-1.5-flash",
        ] if m
    ))

    for api_key in api_keys:
        client = genai.Client(api_key=api_key)
        try:
            for model_name in models_to_try:
                try:
                    response = await asyncio.to_thread(
                        client.models.generate_content,
                        model=model_name,
                        contents=prompt,
                    )
                    if response and response.text:
                        return ChatResponse(response=response.text.strip())
                except Exception:
                    continue
        finally:
            client.close()

    return ChatResponse(
        response=_fallback_shopper_response(
            tenant.name,
            products,
            request.messages[-1].content if request.messages else "",
        )
    )
