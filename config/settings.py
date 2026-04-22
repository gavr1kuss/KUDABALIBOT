from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Конфигурация бота из .env"""

    # Telegram
    bot_token: SecretStr
    telegram_api_id: int
    telegram_api_hash: str

    # AI (любой OpenAI-совместимый провайдер: DeepSeek, Claude через vibecode-claude, и т.д.)
    ai_api_key: SecretStr
    ai_base_url: str = "https://api.deepseek.com"
    ai_model: str = "deepseek-chat"

    # Админ
    admin_id: int

    # База данных
    database_url: str = "sqlite+aiosqlite:///events.db"

    # Сборщик
    history_days: int = 2
    scan_interval_hours: int = 6

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


config = Settings()
