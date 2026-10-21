from typing import Any, List
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
import asyncio
from api.deps import get_current_user_optional, get_db
from db.models.user import User
from sqlalchemy.orm import Session
from db.models.product import Product
import json

router = APIRouter()

class ChatMessage(BaseModel):
    role: str
    content: str = Field(min_length=1, max_length=4000)

class ChatRequest(BaseModel):
    messages: List[ChatMessage] = Field(min_length=1, max_length=30)

class ChatResponse(BaseModel):
    response: str

@router.post("", response_model=ChatResponse)
async def chat_with_ai(
    request: ChatRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user_optional)
) -> Any:
    """
    Endpoint para chatear con el Personal Shopper IA.
    """
    from core.config import settings
    from google import genai
    
    products = db.query(Product).filter(Product.is_active.is_(True)).limit(50).all()
    catalog_context = "Catálogo de productos disponibles:\n"
    for p in products:
        cat = p.category.name if p.category else "Otros"
        catalog_context += f"- {p.name} ({cat}): ${p.base_price}. {p.description or ''}\n"

    system_instruction = (
        "Eres un Personal Shopper experto en moda para FashionStore. "
        "Tu objetivo es ayudar al usuario a encontrar la ropa perfecta de nuestro catálogo. "
        "Sé amable, persuasivo y da consejos de estilo. "
        "Usa emojis. Mantén tus respuestas concisas.\n\n"
        f"{catalog_context}"
    )

    try:
        # Usar la clave gratuita para el chatbot
        api_key = settings.GEMINI_API_KEY_2 or settings.GEMINI_API_KEY_1
        client = genai.Client(api_key=api_key)
        
        conversation = ""
        for msg in request.messages:
            conversation += f"{msg.role}: {msg.content}\n"
        
        prompt = f"{system_instruction}\n\nHistorial de chat:\n{conversation}\nAI:"
        
        models_to_try = [settings.GEMINI_TEXT_MODEL]
        response = None
        last_error = None
        for model_name in models_to_try:
            try:
                response = await asyncio.to_thread(client.models.generate_content,
                    model=model_name,
                    contents=prompt,
                )
                break
            except Exception as e:
                if "NOT_FOUND" in str(e):
                    last_error = e
                    continue
                else:
                    raise e
        
        if not response:
            raise ValueError(f"Ningún modelo funcionó. Último error: {last_error}")
        if not response.text:
            raise ValueError("Respuesta vacía de Gemini")
            
        return ChatResponse(response=response.text.strip())
    except Exception as e:
        print(f"Error en AI Chat: {e}")
        raise HTTPException(status_code=500, detail="Error al comunicarse con el asistente IA.")
