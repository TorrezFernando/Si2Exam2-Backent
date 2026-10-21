from pydantic_settings import BaseSettings
from typing import Optional, List


class Settings(BaseSettings):
    ENVIRONMENT: str = "development"
    PROJECT_NAME: str = "FashionStore API"
    VERSION: str = "1.0.0"
    API_V1_STR: str = "/api/v1"

    # ─── DATABASE ─────────────────────────────────────────
    # Leer la URL completa desde el .env (compatible con PostgreSQL y SQLite)
    DATABASE_URL: str = "sqlite:///./fashionstore.db"

    @property
    def SQLALCHEMY_DATABASE_URI(self) -> str:
        return self.DATABASE_URL

    # ─── SECURITY ─────────────────────────────────────────
    # Asegúrate de configurar SECRET_KEY en producción a través del archivo .env
    SECRET_KEY: str = "super_secret_key_change_in_production"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 días por defecto
    BACKEND_CORS_ORIGINS: List[str] = ["*"]  # Lista de orígenes permitidos para CORS

    # ─── FIRST ADMIN (seed) ───────────────────────────────
    FIRST_ADMIN_EMAIL: str = "admin@fashionstore.com"
    # Asegúrate de configurar FIRST_ADMIN_PASSWORD en el archivo .env
    FIRST_ADMIN_PASSWORD: str = "Admin@1234"
    FIRST_ADMIN_NAME: str = "Administrador Principal"

    # ─── GOOGLE GEMINI AI ─────────────────────────────────
    # Configuracion IA (Gemini)
    GEMINI_API_KEY_1: str = ""
    GEMINI_API_KEY_2: str = ""
    GEMINI_API_KEY_3: str = ""
    GEMINI_API_KEY_4: str = ""
    GEMINI_TEXT_MODEL: str = ""
    GEMINI_MODEL: str = "gemini-3.1-flash-image"
    AI_TRYON_ENABLED: bool = True
    TRYON_UPLOAD_DIR: str = "uploads/tryon"

    # Set only a Libelula-issued TEST appkey for the academic deployment.
    LIBELULA_TEST_APPKEY: str = ""
    PUBLIC_API_URL: str = ""
    PUBLIC_WEB_URL: str = ""

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = True


settings = Settings()


def validate_production():
    if settings.ENVIRONMENT != 'production':
        return
    if len(settings.SECRET_KEY) < 32 or settings.SECRET_KEY == 'super_secret_key_change_in_production':
        raise RuntimeError('Configura un SECRET_KEY aleatorio de al menos 32 caracteres')
    if not settings.DATABASE_URL.startswith(('postgresql://', 'postgresql+psycopg2://')):
        raise RuntimeError('El despliegue del examen requiere PostgreSQL')
    if '*' in settings.BACKEND_CORS_ORIGINS:
        raise RuntimeError('Configura los dominios permitidos en BACKEND_CORS_ORIGINS')
