from datetime import datetime, timedelta
from typing import Any, Union, Optional
from jose import jwt
from passlib.context import CryptContext
from core.config import settings

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifica que la contraseña en texto plano coincida con el hash almacenado."""
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    """Genera el hash bcrypt de una contraseña."""
    return pwd_context.hash(password)


def create_access_token(
    subject: Union[str, int],
    role: str = "cliente",
    expires_delta: Optional[timedelta] = None,
    tenant_id: Optional[int] = None,
    plan_code: Optional[str] = None,
    plan_id: Optional[int] = None,
) -> str:
    """
    RNF01 / RNF02: Crea un JWT firmado con:
    - sub       : ID del usuario (como string)
    - role      : Rol del usuario
    - tenant_id : ID de la empresa contratante (Tenant)
    - plan_code : Código del plan activo ('basic', 'professional')
    - plan_id   : ID del plan de suscripción
    - exp       : Tiempo de expiración
    """
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)

    to_encode = {
        "exp": expire,
        "sub": str(subject),
        "role": role,
        "tenant_id": tenant_id,
        "plan_code": plan_code,
        "plan_id": plan_id,
    }
    return jwt.encode(to_encode, settings.SECRET_KEY, algorithm=settings.ALGORITHM)

