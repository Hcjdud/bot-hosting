import os

class Config:
    # Токен бота (берется из переменных окружения Render)
    BOT_TOKEN = os.getenv("BOT_TOKEN", "8270979575:AAGK9BnLpi-wfFTnvziUMl1vj89YRAFbIjg")
    
    # ID администраторов
    ADMIN_IDS = [int(id) for id in os.getenv("ADMIN_IDS", "8443743937").split(",") if id]
    
    # База данных (Render подставит свои значения)
    DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:password@localhost:5432/hosting_db")
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    
    # Настройки
    DEBUG = os.getenv("DEBUG", "False").lower() == "true"
    MAX_BOTS_PER_USER = int(os.getenv("MAX_BOTS_PER_USER", "5"))
    AUTO_DELETE_COMMANDS = os.getenv("AUTO_DELETE_COMMANDS", "True").lower() == "true"
    COMMAND_LIFETIME = int(os.getenv("COMMAND_LIFETIME", "5"))
    ANTI_SLEEP_ENABLED = os.getenv("ANTI_SLEEP_ENABLED", "True").lower() == "true"
    ANTI_SLEEP_INTERVAL = int(os.getenv("ANTI_SLEEP_INTERVAL", "300"))
    
    # Render
    RENDER = os.getenv("RENDER", "False").lower() == "true"
    RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "")

config = Config()
