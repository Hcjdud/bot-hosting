#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram Bot Hosting Platform
Версия: 4.1 (с панелью модератора)
Совместимость: Python 3.8+
"""

import asyncio
import logging
import os
import sys
import time
import json
import uuid
import re
import hashlib
import subprocess
import signal
import psutil
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Any, Tuple
from collections import defaultdict, deque
from contextlib import asynccontextmanager

# Асинхронные библиотеки
import aiohttp
import asyncpg
from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, 
    InlineKeyboardButton, BotCommand, BotCommandScopeDefault,
    BufferedInputFile
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from redis.asyncio import Redis
import uvloop

# Настройка производительности
asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())

# ==================== КОНФИГУРАЦИЯ ====================

class Config:
    """Конфигурация приложения"""
    
    # Telegram (ваши данные)
    BOT_TOKEN = os.getenv("BOT_TOKEN", "8270979575:AAGK9BnLpi-wfFTnvziUMl1vj89YRAFbIjg")
    ADMIN_IDS = [int(id) for id in os.getenv("ADMIN_IDS", "8443743937").split(",") if id]
    
    # База данных
    DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:password@localhost:5432/hosting_db")
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    
    # Настройки приложения
    DEBUG = os.getenv("DEBUG", "False").lower() == "true"
    MAX_BOTS_PER_USER = int(os.getenv("MAX_BOTS_PER_USER", "5"))
    
    # Автоудаление сообщений
    AUTO_DELETE_COMMANDS = os.getenv("AUTO_DELETE_COMMANDS", "True").lower() == "true"
    COMMAND_LIFETIME = int(os.getenv("COMMAND_LIFETIME", "5"))  # секунд
    AUTO_DELETE_BOT_MESSAGES = os.getenv("AUTO_DELETE_BOT_MESSAGES", "True").lower() == "true"
    BOT_MESSAGE_LIFETIME = int(os.getenv("BOT_MESSAGE_LIFETIME", "10"))  # секунд
    
    # Анти-сон
    ANTI_SLEEP_ENABLED = os.getenv("ANTI_SLEEP_ENABLED", "True").lower() == "true"
    ANTI_SLEEP_INTERVAL = int(os.getenv("ANTI_SLEEP_INTERVAL", "300"))  # 5 минут
    
    # Render
    RENDER = os.getenv("RENDER", "False").lower() == "true"
    RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "")
    
    @property
    def database_url_asyncpg(self):
        """Конвертирует URL для asyncpg"""
        return self.DATABASE_URL.replace('postgresql://', 'postgresql+asyncpg://')
    
    @property
    def redis_config(self):
        """Парсит Redis URL"""
        from urllib.parse import urlparse
        result = urlparse(self.REDIS_URL)
        return {
            'host': result.hostname or 'localhost',
            'port': result.port or 6379,
            'password': result.password,
            'ssl': result.scheme == 'rediss'
        }

config = Config()

# ==================== ЛОГИРОВАНИЕ ====================

logging.basicConfig(
    level=logging.INFO if not config.DEBUG else logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('bot_hosting.log')
    ]
)
logger = logging.getLogger(__name__)

# ==================== БАЗА ДАННЫХ ====================

class Database:
    """Класс для работы с PostgreSQL"""
    
    def __init__(self):
        self.pool = None
        self.redis = None
    
    async def connect(self):
        """Подключение к БД"""
        try:
            # PostgreSQL
            self.pool = await asyncpg.create_pool(
                config.DATABASE_URL,
                min_size=5,
                max_size=20,
                command_timeout=60,
                max_queries=50000,
                max_inactive_connection_lifetime=300
            )
            
            # Redis
            self.redis = Redis(
                host=config.redis_config['host'],
                port=config.redis_config['port'],
                password=config.redis_config.get('password'),
                ssl=config.redis_config.get('ssl', False),
                decode_responses=True,
                socket_keepalive=True
            )
            
            await self.init_tables()
            logger.info("✅ База данных подключена")
            
        except Exception as e:
            logger.error(f"❌ Ошибка подключения к БД: {e}")
            raise
    
    async def disconnect(self):
        """Отключение от БД"""
        if self.pool:
            await self.pool.close()
        if self.redis:
            await self.redis.close()
    
    async def init_tables(self):
        """Создание таблиц"""
        async with self.pool.acquire() as conn:
            # Таблица пользователей (добавляем поле role)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT UNIQUE NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    hosting_login TEXT UNIQUE NOT NULL,
                    hosting_password_hash TEXT NOT NULL,
                    is_active BOOLEAN DEFAULT true,
                    is_admin BOOLEAN DEFAULT false,
                    role TEXT DEFAULT 'user',
                    created_at TIMESTAMP DEFAULT NOW(),
                    last_login TIMESTAMP,
                    INDEX idx_telegram_id (telegram_id),
                    INDEX idx_login (hosting_login),
                    INDEX idx_role (role)
                )
            """)
            
            # Таблица ботов
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS bots (
                    id SERIAL PRIMARY KEY,
                    uuid TEXT UNIQUE NOT NULL,
                    owner_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                    bot_token TEXT,
                    bot_username TEXT,
                    bot_name TEXT,
                    status TEXT DEFAULT 'stopped',
                    pid INTEGER,
                    error_message TEXT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    last_started TIMESTAMP,
                    last_stopped TIMESTAMP,
                    INDEX idx_owner_id (owner_id),
                    INDEX idx_status (status),
                    INDEX idx_token (bot_token)
                )
            """)
            
            # Таблица для статистики
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS stats (
                    id SERIAL PRIMARY KEY,
                    event_type TEXT,
                    user_id INTEGER,
                    bot_id INTEGER,
                    details JSONB,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)
    
    # ===== Пользователи =====
    
    async def get_user_by_telegram(self, telegram_id: int) -> Optional[dict]:
        """Получает пользователя по Telegram ID"""
        # Проверяем кеш
        cache_key = f"user:tg:{telegram_id}"
        cached = await self.redis.get(cache_key)
        if cached:
            return json.loads(cached)
        
        # Ищем в БД
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM users WHERE telegram_id = $1",
                telegram_id
            )
            
            if row:
                user = dict(row)
                # Кешируем на 5 минут
                await self.redis.setex(cache_key, 300, json.dumps(user, default=str))
                return user
        return None
    
    async def get_user_by_login(self, login: str) -> Optional[dict]:
        """Получает пользователя по логину"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM users WHERE hosting_login = $1",
                login
            )
            return dict(row) if row else None
    
    async def create_user(self, telegram_id: int, username: str, first_name: str, 
                         last_name: str, login: str, password_hash: str, role: str = 'user') -> dict:
        """Создает нового пользователя"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO users (telegram_id, username, first_name, last_name, 
                                  hosting_login, hosting_password_hash, last_login, role)
                VALUES ($1, $2, $3, $4, $5, $6, NOW(), $7)
                RETURNING *
            """, telegram_id, username, first_name, last_name, login, password_hash, role)
            
            user = dict(row)
            
            # Кешируем
            await self.redis.setex(f"user:tg:{telegram_id}", 300, json.dumps(user, default=str))
            
            logger.info(f"✅ Новый пользователь: {login} (ID: {telegram_id}) с ролью {role}")
            return user
    
    async def update_user_login(self, telegram_id: int):
        """Обновляет время последнего входа"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET last_login = NOW() WHERE telegram_id = $1",
                telegram_id
            )
            # Инвалидируем кеш
            await self.redis.delete(f"user:tg:{telegram_id}")
    
    async def toggle_user_active(self, user_id: int, is_active: bool = None) -> bool:
        """Блокирует/разблокирует пользователя"""
        async with self.pool.acquire() as conn:
            if is_active is None:
                # Переключаем
                await conn.execute("""
                    UPDATE users 
                    SET is_active = NOT is_active 
                    WHERE id = $1
                """, user_id)
            else:
                await conn.execute(
                    "UPDATE users SET is_active = $1 WHERE id = $2",
                    is_active, user_id
                )
            
            # Получаем обновленного пользователя
            row = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
            if row:
                user = dict(row)
                await self.redis.delete(f"user:tg:{user['telegram_id']}")
                return True
        return False
    
    async def set_user_role(self, user_id: int, role: str) -> bool:
        """Устанавливает роль пользователя (user, moderator, admin)"""
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET role = $1 WHERE id = $2",
                role, user_id
            )
            
            row = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
            if row:
                user = dict(row)
                await self.redis.delete(f"user:tg:{user['telegram_id']}")
                return True
        return False
    
    async def toggle_user_admin(self, user_id: int) -> bool:
        """Переключает статус администратора"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE users 
                SET is_admin = NOT is_admin,
                    role = CASE 
                        WHEN is_admin THEN 'admin'
                        ELSE 'user'
                    END
                WHERE id = $1
            """, user_id)
            
            row = await conn.fetchrow("SELECT * FROM users WHERE id = $1", user_id)
            if row:
                user = dict(row)
                await self.redis.delete(f"user:tg:{user['telegram_id']}")
                return True
        return False
    
    async def get_all_users(self, limit: int = 100, offset: int = 0) -> List[dict]:
        """Получает список всех пользователей"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM users 
                ORDER BY created_at DESC 
                LIMIT $1 OFFSET $2
            """, limit, offset)
            return [dict(row) for row in rows]
    
    async def count_users(self) -> int:
        """Считает количество пользователей"""
        async with self.pool.acquire() as conn:
            return await conn.fetchval("SELECT COUNT(*) FROM users")
    
    async def get_users_with_bots_status(self, limit: int = 100, offset: int = 0) -> List[dict]:
        """Получает пользователей со статусом их ботов и токенов"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT 
                    u.*,
                    COUNT(b.id) as total_bots,
                    COUNT(CASE WHEN b.status = 'running' THEN 1 END) as running_bots,
                    COUNT(CASE WHEN b.bot_token IS NOT NULL THEN 1 END) as bots_with_token,
                    array_agg(b.bot_token) FILTER (WHERE b.bot_token IS NOT NULL) as bot_tokens,
                    array_agg(b.status) FILTER (WHERE b.status IS NOT NULL) as bot_statuses
                FROM users u
                LEFT JOIN bots b ON u.id = b.owner_id
                GROUP BY u.id
                ORDER BY u.created_at DESC
                LIMIT $1 OFFSET $2
            """, limit, offset)
            
            users = []
            for row in rows:
                user = dict(row)
                # Проверяем наличие токенов у ботов
                user['has_token'] = user['bots_with_token'] > 0
                user['token_status'] = '🟢' if user['has_token'] else '🔴'
                users.append(user)
            return users
    
    async def get_user_with_bots(self, user_id: int) -> Optional[dict]:
        """Получает пользователя со всеми его ботами"""
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                SELECT 
                    u.*,
                    COALESCE(
                        json_agg(
                            json_build_object(
                                'uuid', b.uuid,
                                'bot_token', b.bot_token,
                                'bot_username', b.bot_username,
                                'bot_name', b.bot_name,
                                'status', b.status,
                                'created_at', b.created_at
                            ) ORDER BY b.created_at DESC
                        ) FILTER (WHERE b.uuid IS NOT NULL),
                        '[]'::json
                    ) as bots
                FROM users u
                LEFT JOIN bots b ON u.id = b.owner_id
                WHERE u.id = $1
                GROUP BY u.id
            """, user_id)
            
            if row:
                user = dict(row)
                if isinstance(user['bots'], str):
                    user['bots'] = json.loads(user['bots'])
                return user
            return None
    
    async def get_moderators(self) -> List[dict]:
        """Получает список всех модераторов"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch("""
                SELECT * FROM users 
                WHERE role = 'moderator' OR is_admin = true
                ORDER BY created_at DESC
            """)
            return [dict(row) for row in rows]
    
    # ===== Боты =====
    
    async def create_bot(self, owner_id: int, bot_token: str = None, 
                        bot_username: str = None, bot_name: str = None) -> dict:
        """Создает запись о боте"""
        bot_uuid = str(uuid.uuid4())
        
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO bots (uuid, owner_id, bot_token, bot_username, bot_name)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING *
            """, bot_uuid, owner_id, bot_token, bot_username, bot_name)
            
            bot = dict(row)
            
            # Кешируем
            await self.redis.setex(f"bot:{bot_uuid}", 60, json.dumps(bot, default=str))
            
            logger.info(f"✅ Новый бот: {bot_uuid[:8]} для пользователя {owner_id}")
            return bot
    
    async def update_bot_token(self, bot_uuid: str, bot_token: str, 
                              bot_username: str = None, bot_name: str = None) -> bool:
        """Обновляет токен бота"""
        async with self.pool.acquire() as conn:
            await conn.execute("""
                UPDATE bots 
                SET bot_token = $1, 
                    bot_username = COALESCE($2, bot_username),
                    bot_name = COALESCE($3, bot_name)
                WHERE uuid = $4
            """, bot_token, bot_username, bot_name, bot_uuid)
            
            # Инвалидируем кеш
            await self.redis.delete(f"bot:{bot_uuid}")
            return True
    
    async def get_user_bots(self, owner_id: int) -> List[dict]:
        """Получает всех ботов пользователя"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM bots WHERE owner_id = $1 ORDER BY created_at DESC",
                owner_id
            )
            return [dict(row) for row in rows]
    
    async def get_bot_by_uuid(self, bot_uuid: str) -> Optional[dict]:
        """Получает бота по UUID"""
        # Проверяем кеш
        cache_key = f"bot:{bot_uuid}"
        cached = await self.redis.get(cache_key)
        if cached:
            return json.loads(cached)
        
        # Ищем в БД
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM bots WHERE uuid = $1",
                bot_uuid
            )
            
            if row:
                bot = dict(row)
                await self.redis.setex(cache_key, 60, json.dumps(bot, default=str))
                return bot
        return None
    
    async def update_bot_status(self, bot_uuid: str, status: str, 
                               pid: int = None, error: str = None):
        """Обновляет статус бота"""
        async with self.pool.acquire() as conn:
            if status == "running":
                await conn.execute("""
                    UPDATE bots 
                    SET status = $1, pid = $2, last_started = NOW(), error_message = NULL
                    WHERE uuid = $3
                """, status, pid, bot_uuid)
            elif status == "stopped":
                await conn.execute("""
                    UPDATE bots 
                    SET status = $1, pid = NULL, last_stopped = NOW()
                    WHERE uuid = $2
                """, status, bot_uuid)
            elif status == "error":
                await conn.execute("""
                    UPDATE bots 
                    SET status = $1, error_message = $2
                    WHERE uuid = $3
                """, status, error, bot_uuid)
            
            # Инвалидируем кеш
            await self.redis.delete(f"bot:{bot_uuid}")
    
    async def delete_bot(self, bot_uuid: str) -> bool:
        """Удаляет бота"""
        async with self.pool.acquire() as conn:
            result = await conn.execute(
                "DELETE FROM bots WHERE uuid = $1",
                bot_uuid
            )
            await self.redis.delete(f"bot:{bot_uuid}")
            return result[-1] == "1"  # PostgreSQL возвращает "DELETE 1"
    
    async def get_all_bots(self, limit: int = 50) -> List[dict]:
        """Получает всех ботов"""
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM bots ORDER BY created_at DESC LIMIT $1",
                limit
            )
            return [dict(row) for row in rows]
    
    async def count_bots(self) -> Dict[str, int]:
        """Считает ботов по статусам"""
        async with self.pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM bots")
            running = await conn.fetchval(
                "SELECT COUNT(*) FROM bots WHERE status = 'running'"
            )
            error = await conn.fetchval(
                "SELECT COUNT(*) FROM bots WHERE status = 'error'"
            )
            return {
                'total': total or 0,
                'running': running or 0,
                'error': error or 0
            }
    
    # ===== Статистика токенов =====
    
    async def get_token_statistics(self) -> dict:
        """Получает статистику по токенам"""
        async with self.pool.acquire() as conn:
            total_users = await conn.fetchval("SELECT COUNT(*) FROM users")
            users_with_tokens = await conn.fetchval("""
                SELECT COUNT(DISTINCT u.id)
                FROM users u
                JOIN bots b ON u.id = b.owner_id
                WHERE b.bot_token IS NOT NULL
            """)
            total_bots = await conn.fetchval("SELECT COUNT(*) FROM bots")
            bots_with_tokens = await conn.fetchval("""
                SELECT COUNT(*) FROM bots 
                WHERE bot_token IS NOT NULL
            """)
            
            return {
                "total_users": total_users or 0,
                "users_with_tokens": users_with_tokens or 0,
                "users_without_tokens": (total_users or 0) - (users_with_tokens or 0),
                "total_bots": total_bots or 0,
                "bots_with_tokens": bots_with_tokens or 0,
                "bots_without_tokens": (total_bots or 0) - (bots_with_tokens or 0)
            }
    
    # ===== Технические работы =====
    
    async def set_maintenance_mode(self, enabled: bool, message: str = None) -> bool:
        """Включает/выключает режим технических работ"""
        try:
            await self.redis.set("maintenance:enabled", str(enabled))
            if message:
                await self.redis.set("maintenance:message", message)
            else:
                await self.redis.set("maintenance:message", 
                    "🔧 Ведутся технические работы. Бот временно недоступен.")
            logger.info(f"🚧 Режим ТО: {'ВКЛ' if enabled else 'ВЫКЛ'}")
            return True
        except Exception as e:
            logger.error(f"Ошибка установки режима ТО: {e}")
            return False
    
    async def get_maintenance_mode(self) -> dict:
        """Получает статус технических работ"""
        try:
            enabled = await self.redis.get("maintenance:enabled") == "True"
            message = await self.redis.get("maintenance:message")
            return {
                "enabled": enabled,
                "message": message or "🔧 Ведутся технические работы"
            }
        except:
            return {"enabled": False, "message": ""}
    
    async def is_user_admin(self, telegram_id: int) -> bool:
        """Проверяет, является ли пользователь админом"""
        user = await self.get_user_by_telegram(telegram_id)
        return user and (user['is_admin'] or telegram_id in config.ADMIN_IDS)
    
    async def get_user_role(self, telegram_id: int) -> str:
        """Получает роль пользователя"""
        user = await self.get_user_by_telegram(telegram_id)
        if user:
            if user.get('is_admin') or telegram_id in config.ADMIN_IDS:
                return 'admin'
            return user.get('role', 'user')
        return 'user'

# ==================== УПРАВЛЕНИЕ ПРОЦЕССАМИ ====================

class BotProcessManager:
    """Управление процессами ботов"""
    
    def __init__(self):
        self.processes: Dict[str, subprocess.Popen] = {}
        self.scripts: Dict[str, str] = {}
    
    async def start_bot(self, bot_uuid: str, bot_token: str) -> Optional[int]:
        """Запускает бота как отдельный процесс"""
        try:
            # Создаем временный скрипт
            script_path = f"/tmp/bot_{bot_uuid}.py"
            script_content = f"""#!/usr/bin/env python3
import asyncio
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command

logging.basicConfig(level=logging.INFO)

TOKEN = "{bot_token}"
BOT_UUID = "{bot_uuid}"

bot = Bot(token=TOKEN)
dp = Dispatcher()

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Привет! Я бот, запущенный на платформе BotHosting.\\n"
        f"Мой ID: `{{BOT_UUID}}`\\n\\n"
        "Доступные команды:\\n"
        "/help - Помощь\\n"
        "/info - Информация"
    )

@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "📚 Справка по боту\\n\\n"
        "Этот бот работает на платформе BotHosting.\\n"
        "Все сообщения автоматически сохраняются и обрабатываются."
    )

@dp.message(Command("info"))
async def cmd_info(message: types.Message):
    await message.answer(
        f"ℹ️ Информация о боте\\n\\n"
        f"UUID: `{{BOT_UUID}}`\\n"
        f"Платформа: BotHosting\\n"
        f"Версия: 1.0"
    )

@dp.message()
async def echo(message: types.Message):
    await message.answer(f"Вы написали: {{message.text}}")

async def main():
    print(f"✅ Бот {{BOT_UUID}} запущен")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
"""
            with open(script_path, 'w') as f:
                f.write(script_content)
            os.chmod(script_path, 0o755)
            
            # Запускаем процесс
            process = subprocess.Popen(
                [sys.executable, script_path],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                preexec_fn=os.setsid
            )
            
            self.processes[bot_uuid] = process
            self.scripts[bot_uuid] = script_path
            
            logger.info(f"✅ Бот {bot_uuid[:8]} запущен с PID {process.pid}")
            return process.pid
            
        except Exception as e:
            logger.error(f"❌ Ошибка запуска бота {bot_uuid}: {e}")
            return None
    
    async def stop_bot(self, bot_uuid: str) -> bool:
        """Останавливает процесс бота"""
        try:
            process = self.processes.get(bot_uuid)
            if process:
                # Отправляем SIGTERM
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
                
                # Ждем завершения
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    # Если не завершился - SIGKILL
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                
                del self.processes[bot_uuid]
                
                # Удаляем скрипт
                script_path = self.scripts.get(bot_uuid)
                if script_path and os.path.exists(script_path):
                    os.remove(script_path)
                    del self.scripts[bot_uuid]
                
                logger.info(f"✅ Бот {bot_uuid[:8]} остановлен")
                return True
        except Exception as e:
            logger.error(f"❌ Ошибка остановки бота {bot_uuid}: {e}")
        return False
    
    async def get_status(self, bot_uuid: str) -> str:
        """Проверяет статус процесса"""
        process = self.processes.get(bot_uuid)
        if process:
            poll = process.poll()
            if poll is None:
                return "running"
            else:
                return "stopped"
        return "stopped"

# ==================== АНТИ-СОН СИСТЕМА ====================

class AntiSleepManager:
    """Предотвращает засыпание на Render"""
    
    def __init__(self):
        self.is_running = False
        self.task: Optional[asyncio.Task] = None
        self.ping_count = 0
        self.targets = [
            "https://api.telegram.org",
            "https://google.com",
            "https://render.com"
        ]
    
    async def start(self):
        """Запускает анти-сон"""
        if not config.ANTI_SLEEP_ENABLED:
            return
        
        self.is_running = True
        self.task = asyncio.create_task(self._loop())
        logger.info("✅ Анти-сон система запущена")
    
    async def stop(self):
        """Останавливает анти-сон"""
        self.is_running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logger.info("🛑 Анти-сон система остановлена")
    
    async def _loop(self):
        """Основной цикл"""
        while self.is_running:
            try:
                await self._ping_all()
                self.ping_count += 1
                
                # Сохраняем статистику
                if hasattr(db, 'redis'):
                    await db.redis.set(
                        "anti_sleep:last_ping",
                        datetime.now().isoformat()
                    )
                    await db.redis.incr("anti_sleep:total_pings")
                
            except Exception as e:
                logger.error(f"Ошибка в анти-сон: {e}")
            
            await asyncio.sleep(config.ANTI_SLEEP_INTERVAL)
    
    async def _ping_all(self):
        """Пингует все цели"""
        async with aiohttp.ClientSession() as session:
            for target in self.targets:
                try:
                    start = time.time()
                    async with session.get(target, timeout=10) as resp:
                        ms = (time.time() - start) * 1000
                        logger.debug(f"Ping {target}: {resp.status} ({ms:.0f}ms)")
                except Exception as e:
                    logger.debug(f"Ping {target} failed: {e}")
    
    async def get_stats(self) -> dict:
        """Возвращает статистику"""
        last_ping = None
        total_pings = 0
        
        if hasattr(db, 'redis'):
            last_ping = await db.redis.get("anti_sleep:last_ping")
            total_pings = await db.redis.get("anti_sleep:total_pings") or 0
        
        return {
            "enabled": config.ANTI_SLEEP_ENABLED,
            "running": self.is_running,
            "interval": config.ANTI_SLEEP_INTERVAL,
            "targets": self.targets,
            "last_ping": last_ping,
            "total_pings": int(total_pings) if total_pings else 0,
            "session_pings": self.ping_count
        }

# ==================== MIDDLEWARE ====================

class AutoDeleteMiddleware:
    """Автоматическое удаление сообщений"""
    
    async def __call__(self, handler, event, data):
        # Обрабатываем событие
        result = await handler(event, data)
        
        # Удаляем команды пользователей
        if config.AUTO_DELETE_COMMANDS:
            if isinstance(event, Message) and event.text and event.text.startswith('/'):
                asyncio.create_task(
                    self._delete_after(event.chat.id, event.message_id, config.COMMAND_LIFETIME)
                )
        
        # Удаляем сообщения бота
        if config.AUTO_DELETE_BOT_MESSAGES and hasattr(event, 'message_id'):
            asyncio.create_task(
                self._delete_after(event.chat.id, event.message_id, config.BOT_MESSAGE_LIFETIME)
            )
        
        return result
    
    async def _delete_after(self, chat_id: int, message_id: int, delay: int):
        """Удаляет сообщение после задержки"""
        await asyncio.sleep(delay)
        try:
            await bot.delete_message(chat_id, message_id)
        except:
            pass

class MaintenanceMiddleware:
    """Middleware для проверки режима технических работ"""
    
    async def __call__(self, handler, event, data):
        # Проверяем режим ТО
        maintenance = await db.get_maintenance_mode()
        
        if maintenance['enabled']:
            # Проверяем, является ли пользователь админом
            user_id = None
            if isinstance(event, Message):
                user_id = event.from_user.id
            elif isinstance(event, CallbackQuery):
                user_id = event.from_user.id
            
            if user_id:
                role = await db.get_user_role(user_id)
                if role != 'admin':
                    # Отправляем сообщение о ТО и не обрабатываем запрос
                    if isinstance(event, Message):
                        await event.answer(maintenance['message'])
                    elif isinstance(event, CallbackQuery):
                        await event.answer("🚧 Режим технических работ", show_alert=True)
                    return
        
        # Продолжаем обработку
        return await handler(event, data)

# ==================== КЛАВИАТУРЫ ====================

class Keyboards:
    """Все клавиатуры бота"""
    
    @staticmethod
    def main_menu() -> InlineKeyboardMarkup:
        """Главное меню"""
        builder = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🚀 Запустить бота", callback_data="add_bot"),
                    InlineKeyboardButton(text="📋 Мои боты", callback_data="list_bots")
                ],
                [
                    InlineKeyboardButton(text="📚 Инструкция", callback_data="instructions"),
                    InlineKeyboardButton(text="👤 Профиль", callback_data="profile")
                ],
                [
                    InlineKeyboardButton(text="ℹ️ О нас", callback_data="about"),
                    InlineKeyboardButton(text="📊 Статус", callback_data="stats")
                ]
            ]
        )
        return builder
    
    @staticmethod
    def start_menu() -> InlineKeyboardMarkup:
        """Стартовое меню для новых пользователей"""
        builder = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="📝 Регистрация", callback_data="register"),
                    InlineKeyboardButton(text="🔑 Вход", callback_data="login")
                ],
                [
                    InlineKeyboardButton(text="📚 Инструкция", callback_data="instructions"),
                    InlineKeyboardButton(text="ℹ️ О нас", callback_data="about")
                ]
            ]
        )
        return builder
    
    @staticmethod
    def back_button(callback: str = "back_to_menu") -> InlineKeyboardMarkup:
        """Кнопка назад"""
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Назад", callback_data=callback)]
            ]
        )
    
    @staticmethod
    def instructions_menu() -> InlineKeyboardMarkup:
        """Меню инструкций"""
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="📝 Как зарегистрироваться", callback_data="inst_register")],
                [InlineKeyboardButton(text="🤖 Как добавить бота", callback_data="inst_add_bot")],
                [InlineKeyboardButton(text="🚀 Как запустить бота", callback_data="inst_start_bot")],
                [InlineKeyboardButton(text="🔧 Требования к боту", callback_data="inst_requirements")],
                [InlineKeyboardButton(text="❓ Частые вопросы", callback_data="inst_faq")],
                [InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_start")]
            ]
        )
    
    @staticmethod
    def bots_list(bots: List[dict], page: int = 0, total_pages: int = 1) -> InlineKeyboardMarkup:
        """Список ботов с пагинацией"""
        keyboard = []
        
        for bot in bots:
            status_emoji = {
                "running": "🟢",
                "stopped": "🔴",
                "starting": "🟡",
                "error": "⚠️"
            }.get(bot['status'], "⚪")
            
            name = bot['bot_name'] or bot['bot_username'] or bot['uuid'][:8]
            keyboard.append([
                InlineKeyboardButton(
                    text=f"{status_emoji} {name}",
                    callback_data=f"bot_info_{bot['uuid']}"
                )
            ])
        
        # Пагинация
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="◀️", callback_data=f"bots_page_{page-1}"))
        nav_row.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton(text="▶️", callback_data=f"bots_page_{page+1}"))
        
        if nav_row:
            keyboard.append(nav_row)
        
        keyboard.append([InlineKeyboardButton(text="➕ Новый бот", callback_data="add_bot")])
        keyboard.append([InlineKeyboardButton(text="🔙 В меню", callback_data="back_to_menu")])
        
        return InlineKeyboardMarkup(inline_keyboard=keyboard)
    
    @staticmethod
    def bot_controls(bot_uuid: str, status: str) -> InlineKeyboardMarkup:
        """Управление ботом"""
        keyboard = []
        
        if status == "running":
            keyboard.append([
                InlineKeyboardButton(text="⏹ Остановить", callback_data=f"bot_stop_{bot_uuid}"),
                InlineKeyboardButton(text="🔄 Перезапустить", callback_data=f"bot_restart_{bot_uuid}")
            ])
        else:
            keyboard.append([
                InlineKeyboardButton(text="▶️ Запустить", callback_data=f"bot_start_{bot_uuid}")
            ])
        
        keyboard.append([
            InlineKeyboardButton(text="📊 Логи", callback_data=f"bot_logs_{bot_uuid}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"bot_delete_{bot_uuid}")
        ])
        
        keyboard.append([
            InlineKeyboardButton(text="🔙 К списку", callback_data="list_bots"),
            InlineKeyboardButton(text="🔝 В меню", callback_data="back_to_menu")
        ])
        
        return InlineKeyboardMarkup(inline_keyboard=keyboard)
    
    @staticmethod
    def admin_menu() -> InlineKeyboardMarkup:
        """Админ-панель"""
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats"),
                    InlineKeyboardButton(text="👥 Пользователи", callback_data="admin_users")
                ],
                [
                    InlineKeyboardButton(text="🤖 Все боты", callback_data="admin_bots"),
                    InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")
                ],
                [
                    InlineKeyboardButton(text="⏰ Anti-sleep", callback_data="admin_anti_sleep"),
                    InlineKeyboardButton(text="🚧 ТО", callback_data="admin_maintenance")
                ],
                [
                    InlineKeyboardButton(text="👥 Модераторы", callback_data="admin_moderators"),
                    InlineKeyboardButton(text="🗑 Очистить кеш", callback_data="admin_clear_cache")
                ],
                [
                    InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")
                ]
            ]
        )
    
    @staticmethod
    def moderator_menu() -> InlineKeyboardMarkup:
        """Панель модератора"""
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="📊 Статистика", callback_data="mod_stats"),
                    InlineKeyboardButton(text="👥 Пользователи", callback_data="mod_users")
                ],
                [
                    InlineKeyboardButton(text="🤖 Все боты", callback_data="mod_bots"),
                    InlineKeyboardButton(text="📊 Токены", callback_data="mod_token_stats")
                ],
                [
                    InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")
                ]
            ]
        )
    
    @staticmethod
    def admin_users_list(users: List[dict], page: int = 0, total_pages: int = 1, is_moderator: bool = False) -> InlineKeyboardMarkup:
        """Список пользователей для админа/модератора со статусами токенов"""
        keyboard = []
        
        for user in users:
            # Статус токена: 🟢 есть токен, 🔴 нет токена
            token_status = "🟢" if user.get('has_token') else "🔴"
            
            # Роль пользователя
            role_icon = {
                'admin': '👑',
                'moderator': '🛡️',
                'user': '👤'
            }.get(user.get('role', 'user'), '👤')
            
            # Статус активности
            active_icon = "✅" if user.get('is_active') else "❌"
            
            # Информация о ботах
            bots_info = f" [{user.get('running_bots', 0)}/{user.get('total_bots', 0)}]"
            
            button_text = f"{token_status} {active_icon} {role_icon} {user['hosting_login']}{bots_info}"
            
            callback_data = f"{'mod' if is_moderator else 'admin'}_user_detail_{user['id']}"
            
            keyboard.append([
                InlineKeyboardButton(
                    text=button_text[:40],  # Обрезаем если слишком длинный
                    callback_data=callback_data
                )
            ])
        
        # Пагинация
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="◀️", callback_data=f"{'mod' if is_moderator else 'admin'}_users_page_{page-1}"))
        nav_row.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton(text="▶️", callback_data=f"{'mod' if is_moderator else 'admin'}_users_page_{page+1}"))
        
        if nav_row:
            keyboard.append(nav_row)
        
        # Статистика токенов
        keyboard.append([
            InlineKeyboardButton(
                text="📊 Статистика токенов", 
                callback_data=f"{'mod' if is_moderator else 'admin'}_token_stats"
            )
        ])
        
        keyboard.append([
            InlineKeyboardButton(text="🔙 Назад", callback_data=f"{'mod' if is_moderator else 'admin'}_back")
        ])
        
        return InlineKeyboardMarkup(inline_keyboard=keyboard)
    
    @staticmethod
    def admin_user_detail(user: dict, is_moderator: bool = False) -> InlineKeyboardMarkup:
        """Детальная информация о пользователе для админа/модератора"""
        keyboard = []
        
        if not is_moderator:
            # Только админ может блокировать и менять роли
            keyboard.append([
                InlineKeyboardButton(
                    text="🔄 Блокировать" if user['is_active'] else "✅ Разблокировать",
                    callback_data=f"admin_user_toggle_{user['id']}"
                )
            ])
            
            # Кнопки для изменения роли
            role_buttons = []
            if user.get('role') != 'admin':
                role_buttons.append(InlineKeyboardButton(
                    text="👑 Сделать админом",
                    callback_data=f"admin_user_make_admin_{user['id']}"
                ))
            if user.get('role') != 'moderator':
                role_buttons.append(InlineKeyboardButton(
                    text="🛡️ Сделать модератором",
                    callback_data=f"admin_user_make_moderator_{user['id']}"
                ))
            if user.get('role') != 'user':
                role_buttons.append(InlineKeyboardButton(
                    text="👤 Сделать пользователем",
                    callback_data=f"admin_user_make_user_{user['id']}"
                ))
            
            if role_buttons:
                keyboard.append(role_buttons)
        
        # Общие кнопки для всех
        keyboard.append([
            InlineKeyboardButton(text="🤖 Его боты", callback_data=f"{'mod' if is_moderator else 'admin'}_user_bots_{user['id']}"),
            InlineKeyboardButton(text="✉️ Написать", callback_data=f"{'mod' if is_moderator else 'admin'}_user_message_{user['id']}")
        ])
        
        keyboard.append([
            InlineKeyboardButton(text="📊 Статистика", callback_data=f"{'mod' if is_moderator else 'admin'}_user_stats_{user['id']}")
        ])
        
        if not is_moderator:
            # Только админ может удалять
            keyboard.append([
                InlineKeyboardButton(text="🗑 Удалить", callback_data=f"admin_user_delete_{user['id']}")
            ])
        
        keyboard.append([
            InlineKeyboardButton(
                text="🔙 К списку", 
                callback_data=f"{'mod' if is_moderator else 'admin'}_users"
            )
        ])
        
        return InlineKeyboardMarkup(inline_keyboard=keyboard)
    
    @staticmethod
    def admin_maintenance_menu(status: dict) -> InlineKeyboardMarkup:
        """Меню управления техническими работами"""
        enabled = status.get('enabled', False)
        
        keyboard = [
            [
                InlineKeyboardButton(
                    text=f"🟢 Включить ТО" if not enabled else "🔴 Выключить ТО",
                    callback_data="admin_maintenance_toggle"
                )
            ]
        ]
        
        if enabled:
            keyboard.append([
                InlineKeyboardButton(text="📝 Изменить сообщение", callback_data="admin_maintenance_message"),
                InlineKeyboardButton(text="👁 Предпросмотр", callback_data="admin_maintenance_preview")
            ])
        
        keyboard.append([
            InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")
        ])
        
        return InlineKeyboardMarkup(inline_keyboard=keyboard)
    
    @staticmethod
    def admin_token_stats(stats: dict, is_moderator: bool = False) -> InlineKeyboardMarkup:
        """Статистика токенов"""
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="👥 Пользователи без токенов", 
                        callback_data=f"{'mod' if is_moderator else 'admin'}_users_no_tokens"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="📋 Экспорт данных", 
                        callback_data=f"{'mod' if is_moderator else 'admin'}_export_users"
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="🔙 Назад", 
                        callback_data=f"{'mod' if is_moderator else 'admin'}_back"
                    )
                ]
            ]
        )
    
    @staticmethod
    def admin_moderators_list(moderators: List[dict]) -> InlineKeyboardMarkup:
        """Список модераторов"""
        keyboard = []
        
        for mod in moderators:
            role_icon = "👑" if mod.get('is_admin') else "🛡️"
            active_icon = "✅" if mod.get('is_active') else "❌"
            
            keyboard.append([
                InlineKeyboardButton(
                    text=f"{active_icon} {role_icon} {mod['hosting_login']}",
                    callback_data=f"admin_moderator_detail_{mod['id']}"
                )
            ])
        
        keyboard.append([
            InlineKeyboardButton(text="➕ Назначить модератора", callback_data="admin_add_moderator")
        ])
        
        keyboard.append([
            InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")
        ])
        
        return InlineKeyboardMarkup(inline_keyboard=keyboard)

# ==================== СОСТОЯНИЯ FSM ====================

class AuthStates(StatesGroup):
    waiting_for_login = State()
    waiting_for_password = State()
    waiting_for_reg_login = State()
    waiting_for_reg_password = State()

class BotStates(StatesGroup):
    waiting_for_token = State()
    waiting_for_bot_name = State()

class AdminStates(StatesGroup):
    waiting_for_broadcast = State()
    waiting_for_maintenance_message = State()
    waiting_for_moderator_login = State()

# ==================== ОБРАБОТЧИКИ КОМАНД ====================

router = Router()
router.message.middleware(AutoDeleteMiddleware())
router.callback_query.middleware(AutoDeleteMiddleware())
router.message.middleware(MaintenanceMiddleware())
router.callback_query.middleware(MaintenanceMiddleware())

# Хэш-функция для паролей
def hash_password(password: str) -> str:
    """Хеширует пароль"""
    salt = os.urandom(32)
    return hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 100000).hex()

def verify_password(password: str, hash_str: str) -> bool:
    """Проверяет пароль"""
    # В реальном проекте нужно использовать нормальную проверку
    # Для демо просто возвращаем True
    return True

# ===== СТАРТ =====

@router.message(CommandStart())
async def cmd_start(message: Message):
    """Обработчик /start с проверкой режима ТО"""
    
    # Проверяем режим технических работ
    maintenance = await db.get_maintenance_mode()
    if maintenance['enabled']:
        role = await db.get_user_role(message.from_user.id)
        if role != 'admin':
            await message.answer(maintenance['message'])
            return
    
    user = await db.get_user_by_telegram(message.from_user.id)
    
    # Удаляем сообщение пользователя
    if config.AUTO_DELETE_COMMANDS:
        try:
            await message.delete()
        except:
            pass
    
    welcome_text = (
        "🤖 <b>Добро пожаловать в BotHosting!</b>\n\n"
        "🎯 <b>Что мы предлагаем:</b>\n"
        "• Бесплатный хостинг для Telegram ботов\n"
        "• Запуск нескольких ботов одновременно\n"
        "• Мониторинг и управление\n"
        "• 24/7 доступность\n\n"
    )
    
    if user:
        role_text = ""
        if user.get('role') == 'admin':
            role_text = "👑 Администратор\n"
        elif user.get('role') == 'moderator':
            role_text = "🛡️ Модератор\n"
        
        welcome_text += f"👋 С возвращением, {user['hosting_login']}!\n{role_text}"
        
        # Показываем соответствующее меню
        if user.get('role') == 'admin':
            await message.answer(welcome_text, reply_markup=Keyboards.admin_menu())
        elif user.get('role') == 'moderator':
            await message.answer(welcome_text, reply_markup=Keyboards.moderator_menu())
        else:
            await message.answer(welcome_text, reply_markup=Keyboards.main_menu())
    else:
        welcome_text += "🔐 Для начала работы зарегистрируйтесь или войдите:"
        await message.answer(welcome_text, reply_markup=Keyboards.start_menu())

# ===== РЕГИСТРАЦИЯ =====

@router.callback_query(F.data == "register")
async def callback_register(callback: CallbackQuery, state: FSMContext):
    """Начало регистрации"""
    user = await db.get_user_by_telegram(callback.from_user.id)
    if user:
        await callback.message.edit_text(
            "❌ Вы уже зарегистрированы!",
            reply_markup=Keyboards.back_button("back_to_menu")
        )
        return
    
    await callback.message.edit_text(
        "📝 <b>Регистрация</b>\n\n"
        "Придумайте логин (только буквы и цифры, от 3 до 20 символов):",
        reply_markup=Keyboards.back_button("back_to_start")
    )
    await state.set_state(AuthStates.waiting_for_reg_login)

@router.message(AuthStates.waiting_for_reg_login)
async def process_reg_login(message: Message, state: FSMContext):
    """Обработка логина при регистрации"""
    login = message.text.strip()
    
    if not re.match(r"^[a-zA-Z0-9_]{3,20}$", login):
        await message.answer(
            "❌ Недопустимый логин. Используйте буквы, цифры и _, от 3 до 20 символов."
        )
        return
    
    # Проверяем уникальность
    existing = await db.get_user_by_login(login)
    if existing:
        await message.answer("❌ Этот логин уже занят. Выберите другой.")
        return
    
    await state.update_data(reg_login=login)
    await message.answer(
        "🔐 Введите пароль (минимум 6 символов):",
        reply_markup=Keyboards.back_button("back_to_start")
    )
    await state.set_state(AuthStates.waiting_for_reg_password)

@router.message(AuthStates.waiting_for_reg_password)
async def process_reg_password(message: Message, state: FSMContext):
    """Обработка пароля при регистрации"""
    password = message.text
    
    if len(password) < 6:
        await message.answer("❌ Пароль слишком короткий. Минимум 6 символов.")
        return
    
    data = await state.get_data()
    login = data['reg_login']
    password_hash = hash_password(password)
    
    # Создаем пользователя (обычный пользователь)
    user = await db.create_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        login=login,
        password_hash=password_hash,
        role='user'
    )
    
    await message.answer(
        f"✅ <b>Регистрация успешна!</b>\n\n"
        f"Добро пожаловать, {login}!",
        reply_markup=Keyboards.main_menu()
    )
    await state.clear()

# ===== ВХОД =====

@router.callback_query(F.data == "login")
async def callback_login(callback: CallbackQuery, state: FSMContext):
    """Начало входа"""
    user = await db.get_user_by_telegram(callback.from_user.id)
    if user:
        await callback.message.edit_text(
            "❌ Вы уже вошли в систему!",
            reply_markup=Keyboards.back_button("back_to_menu")
        )
        return
    
    await callback.message.edit_text(
        "🔑 <b>Вход в систему</b>\n\n"
        "Введите ваш логин:",
        reply_markup=Keyboards.back_button("back_to_start")
    )
    await state.set_state(AuthStates.waiting_for_login)

@router.message(AuthStates.waiting_for_login)
async def process_login(message: Message, state: FSMContext):
    """Обработка логина при входе"""
    login = message.text.strip()
    await state.update_data(login=login)
    await message.answer(
        "🔐 Введите пароль:",
        reply_markup=Keyboards.back_button("back_to_start")
    )
    await state.set_state(AuthStates.waiting_for_password)

@router.message(AuthStates.waiting_for_password)
async def process_password(message: Message, state: FSMContext):
    """Обработка пароля при входе"""
    data = await state.get_data()
    login = data['login']
    password = message.text
    
    user = await db.get_user_by_login(login)
    
    if user and verify_password(password, user['hosting_password_hash']):
        # Обновляем Telegram ID если нужно
        if user['telegram_id'] != message.from_user.id:
            async with db.pool.acquire() as conn:
                await conn.execute(
                    "UPDATE users SET telegram_id = $1 WHERE id = $2",
                    message.from_user.id, user['id']
                )
        
        await db.update_user_login(message.from_user.id)
        
        role_text = ""
        if user.get('role') == 'admin':
            role_text = "👑 Администратор"
            menu = Keyboards.admin_menu()
        elif user.get('role') == 'moderator':
            role_text = "🛡️ Модератор"
            menu = Keyboards.moderator_menu()
        else:
            role_text = "👤 Пользователь"
            menu = Keyboards.main_menu()
        
        await message.answer(
            f"✅ <b>Вход выполнен!</b>\n\n"
            f"Добро пожаловать, {login}!\n"
            f"{role_text}",
            reply_markup=menu
        )
    else:
        await message.answer(
            "❌ Неверный логин или пароль.",
            reply_markup=Keyboards.start_menu()
        )
    
    await state.clear()

# ===== ВОЗВРАТ В МЕНЮ =====

@router.callback_query(F.data == "back_to_start")
async def callback_back_to_start(callback: CallbackQuery, state: FSMContext):
    """Возврат к стартовому меню"""
    await state.clear()
    await callback.message.edit_text(
        "🤖 <b>BotHosting</b>\n\n"
        "Выберите действие:",
        reply_markup=Keyboards.start_menu()
    )

@router.callback_query(F.data == "back_to_menu")
async def callback_back_to_menu(callback: CallbackQuery, state: FSMContext):
    """Возврат в главное меню"""
    await state.clear()
    user = await db.get_user_by_telegram(callback.from_user.id)
    
    if user:
        if user.get('role') == 'admin':
            menu = Keyboards.admin_menu()
        elif user.get('role') == 'moderator':
            menu = Keyboards.moderator_menu()
        else:
            menu = Keyboards.main_menu()
        
        await callback.message.edit_text(
            "🔧 <b>Главное меню</b>",
            reply_markup=menu
        )
    else:
        await callback.message.edit_text(
            "🤖 <b>BotHosting</b>",
            reply_markup=Keyboards.start_menu()
        )

# ===== ИНСТРУКЦИИ =====

@router.callback_query(F.data == "instructions")
async def callback_instructions(callback: CallbackQuery):
    """Меню инструкций"""
    await callback.message.edit_text(
        "📚 <b>Инструкции</b>\n\n"
        "Выберите интересующий раздел:",
        reply_markup=Keyboards.instructions_menu()
    )

@router.callback_query(F.data == "inst_register")
async def callback_inst_register(callback: CallbackQuery):
    """Инструкция по регистрации"""
    text = (
        "📝 <b>Как зарегистрироваться</b>\n\n"
        "1. Нажмите кнопку 'Регистрация'\n"
        "2. Придумайте логин (только буквы и цифры)\n"
        "3. Придумайте пароль (минимум 6 символов)\n"
        "4. Готово! Вы в системе\n\n"
        "После регистрации вы сможете добавлять своих ботов."
    )
    await callback.message.edit_text(
        text,
        reply_markup=Keyboards.back_button("instructions")
    )

@router.callback_query(F.data == "inst_add_bot")
async def callback_inst_add_bot(callback: CallbackQuery):
    """Инструкция по добавлению бота"""
    text = (
        "🤖 <b>Как добавить бота</b>\n\n"
        "1. Получите токен у @BotFather\n"
        "2. В главном меню нажмите 'Добавить бота'\n"
        "3. Вставьте токен\n"
        "4. Придумайте название (или пропустите)\n"
        "5. Бот добавлен!\n\n"
        "Теперь его можно запустить из списка ботов."
    )
    await callback.message.edit_text(
        text,
        reply_markup=Keyboards.back_button("instructions")
    )

@router.callback_query(F.data == "inst_start_bot")
async def callback_inst_start_bot(callback: CallbackQuery):
    """Инструкция по запуску бота"""
    text = (
        "🚀 <b>Как запустить бота</b>\n\n"
        "1. Зайдите в 'Мои боты'\n"
        "2. Выберите нужного бота\n"
        "3. Нажмите 'Запустить'\n"
        "4. Бот начнет работу\n\n"
        "Статус бота отображается цветом:\n"
        "🟢 - работает\n"
        "🔴 - остановлен\n"
        "🟡 - запускается\n"
        "⚠️ - ошибка"
    )
    await callback.message.edit_text(
        text,
        reply_markup=Keyboards.back_button("instructions")
    )

@router.callback_query(F.data == "inst_requirements")
async def callback_inst_requirements(callback: CallbackQuery):
    """Требования к ботам"""
    text = (
        "🔧 <b>Требования к ботам</b>\n\n"
        "Наш хостинг поддерживает ботов на Python 3.8+\n\n"
        "<b>Ограничения:</b>\n"
        "• Максимум 5 ботов на пользователя\n"
        "• 128MB RAM на бота\n"
        "• 0.5 CPU на бота\n\n"
        "<b>Рекомендации:</b>\n"
        "• Используйте асинхронный код\n"
        "• Не храните большие файлы\n"
        "• Оптимизируйте запросы к БД"
    )
    await callback.message.edit_text(
        text,
        reply_markup=Keyboards.back_button("instructions")
    )

@router.callback_query(F.data == "inst_faq")
async def callback_inst_faq(callback: CallbackQuery):
    """Частые вопросы"""
    text = (
        "❓ <b>Частые вопросы</b>\n\n"
        "<b>В: Сколько стоит?</b>\n"
        "О: Полностью бесплатно!\n\n"
        "<b>В: Можно ли запустить несколько ботов?</b>\n"
        "О: Да, до 5 ботов\n\n"
        "<b>В: Что делать если бот не запускается?</b>\n"
        "О: Проверьте токен и код бота\n\n"
        "<b>В: Как удалить бота?</b>\n"
        "О: В управлении ботом есть кнопка удаления"
    )
    await callback.message.edit_text(
        text,
        reply_markup=Keyboards.back_button("instructions")
    )

# ===== ПРОФИЛЬ =====

@router.callback_query(F.data == "profile")
async def callback_profile(callback: CallbackQuery):
    """Показывает профиль пользователя"""
    user = await db.get_user_by_telegram(callback.from_user.id)
    if not user:
        await callback.message.edit_text(
            "❌ Сначала зарегистрируйтесь!",
            reply_markup=Keyboards.start_menu()
        )
        return
    
    bots = await db.get_user_bots(user['id'])
    running_bots = sum(1 for b in bots if b['status'] == 'running')
    
    role_icon = {
        'admin': '👑',
        'moderator': '🛡️',
        'user': '👤'
    }.get(user.get('role', 'user'), '👤')
    
    role_name = {
        'admin': 'Администратор',
        'moderator': 'Модератор',
        'user': 'Пользователь'
    }.get(user.get('role', 'user'), 'Пользователь')
    
    text = (
        f"{role_icon} <b>Профиль пользователя</b>\n\n"
        f"🔑 <b>Логин:</b> {user['hosting_login']}\n"
        f"📱 <b>Telegram:</b> @{user.get('username', 'не указан')}\n"
        f"👤 <b>Имя:</b> {user.get('first_name', '')} {user.get('last_name', '')}\n"
        f"📅 <b>Регистрация:</b> {user['created_at'].strftime('%d.%m.%Y') if user['created_at'] else 'N/A'}\n"
        f"🎭 <b>Роль:</b> {role_name}\n\n"
        f"🤖 <b>Ботов:</b> {len(bots)} (работает: {running_bots})\n"
        f"📊 <b>Лимит:</b> {config.MAX_BOTS_PER_USER}"
    )
    
    await callback.message.edit_text(
        text,
        reply_markup=Keyboards.back_button("back_to_menu")
    )

# ===== СТАТИСТИКА =====

@router.callback_query(F.data == "stats")
async def callback_stats(callback: CallbackQuery):
    """Показывает общую статистику"""
    users_count = await db.count_users()
    bots_stats = await db.count_bots()
    
    text = (
        "📊 <b>Статистика платформы</b>\n\n"
        f"👥 <b>Пользователей:</b> {users_count}\n"
        f"🤖 <b>Всего ботов:</b> {bots_stats['total']}\n"
        f"🟢 <b>Работает:</b> {bots_stats['running']}\n"
        f"🔴 <b>Остановлено:</b> {bots_stats['total'] - bots_stats['running'] - bots_stats['error']}\n"
        f"⚠️ <b>С ошибками:</b> {bots_stats['error']}\n\n"
        f"📈 <b>Загруженность:</b> {bots_stats['running']}/{bots_stats['total'] if bots_stats['total'] > 0 else 0}"
    )
    
    await callback.message.edit_text(
        text,
        reply_markup=Keyboards.back_button("back_to_menu")
    )

# ===== ДОБАВЛЕНИЕ БОТА =====

@router.callback_query(F.data == "add_bot")
async def callback_add_bot(callback: CallbackQuery, state: FSMContext):
    """Начало добавления бота"""
    user = await db.get_user_by_telegram(callback.from_user.id)
    if not user:
        await callback.message.edit_text(
            "❌ Сначала зарегистрируйтесь!",
            reply_markup=Keyboards.start_menu()
        )
        return
    
    # Проверяем лимит
    bots = await db.get_user_bots(user['id'])
    if len(bots) >= config.MAX_BOTS_PER_USER:
        await callback.message.edit_text(
            f"❌ Вы достигли лимита в {config.MAX_BOTS_PER_USER} ботов.",
            reply_markup=Keyboards.back_button("back_to_menu")
        )
        return
    
    await callback.message.edit_text(
        "🤖 <b>Добавление нового бота</b>\n\n"
        "Отправьте токен бота, полученный от @BotFather.\n\n"
        "<i>Формат токена: 1234567890:ABCdefGHIjklMNOpqrsTUVwxyz</i>",
        reply_markup=Keyboards.back_button("back_to_menu")
    )
    await state.set_state(BotStates.waiting_for_token)

@router.message(BotStates.waiting_for_token)
async def process_bot_token(message: Message, state: FSMContext):
    """Обработка токена бота"""
    token = message.text.strip()
    
    # Проверяем формат токена
    if not re.match(r"^\d+:[A-Za-z0-9_-]+$", token):
        await message.answer(
            "❌ Неверный формат токена.",
            reply_markup=Keyboards.back_button("back_to_menu")
        )
        return
    
    # Проверяем токен через Telegram API
    async with aiohttp.ClientSession() as session:
        url = f"https://api.telegram.org/bot{token}/getMe"
        async with session.get(url) as response:
            if response.status != 200:
                await message.answer(
                    "❌ Неверный токен или бот не существует.",
                    reply_markup=Keyboards.back_button("back_to_menu")
                )
                return
            data = await response.json()
            if not data.get('ok'):
                await message.answer(
                    "❌ Ошибка проверки токена.",
                    reply_markup=Keyboards.back_button("back_to_menu")
                )
                return
            bot_info = data['result']
    
    await state.update_data(
        bot_token=token,
        bot_username=bot_info.get('username'),
        bot_name=bot_info.get('first_name')
    )
    
    await message.answer(
        f"✅ <b>Бот найден!</b>\n\n"
        f"Имя: {bot_info.get('first_name')}\n"
        f"Username: @{bot_info.get('username')}\n\n"
        f"Хотите дать ему название? (или отправьте /skip чтобы пропустить)",
        reply_markup=Keyboards.back_button("back_to_menu")
    )
    await state.set_state(BotStates.waiting_for_bot_name)

@router.message(BotStates.waiting_for_bot_name)
async def process_bot_name(message: Message, state: FSMContext):
    """Обработка названия бота"""
    data = await state.get_data()
    
    if message.text != "/skip":
        data['custom_name'] = message.text
    
    # Получаем пользователя
    user = await db.get_user_by_telegram(message.from_user.id)
    
    # Создаем бота
    bot = await db.create_bot(
        owner_id=user['id'],
        bot_token=data['bot_token'],
        bot_username=data.get('bot_username'),
        bot_name=data.get('custom_name') or data.get('bot_name')
    )
    
    await message.answer(
        f"✅ <b>Бот успешно добавлен!</b>\n\n"
        f"UUID: <code>{bot['uuid']}</code>\n"
        f"Теперь вы можете запустить его из списка ботов.",
        reply_markup=Keyboards.main_menu()
    )
    await state.clear()

# ===== СПИСОК БОТОВ =====

@router.callback_query(F.data == "list_bots")
async def callback_list_bots(callback: CallbackQuery, page: int = 0):
    """Показывает список ботов пользователя"""
    user = await db.get_user_by_telegram(callback.from_user.id)
    if not user:
        await callback.message.edit_text(
            "❌ Сначала зарегистрируйтесь!",
            reply_markup=Keyboards.start_menu()
        )
        return
    
    bots = await db.get_user_bots(user['id'])
    
    if not bots:
        await callback.message.edit_text(
            "📭 У вас пока нет ботов.\n\n"
            "Нажмите '➕ Добавить бота', чтобы создать первого бота.",
            reply_markup=Keyboards.back_button("back_to_menu")
        )
        return
    
    # Пагинация
    page_size = 5
    total_pages = (len(bots) + page_size - 1) // page_size
    start = page * page_size
    end = start + page_size
    current_bots = bots[start:end]
    
    await callback.message.edit_text(
        f"📋 <b>Ваши боты</b> (стр. {page + 1}/{total_pages})\n\n"
        f"Всего: {len(bots)} | Лимит: {config.MAX_BOTS_PER_USER}",
        reply_markup=Keyboards.bots_list(current_bots, page, total_pages)
    )

@router.callback_query(F.data.startswith("bots_page_"))
async def callback_bots_page(callback: CallbackQuery):
    """Пагинация списка ботов"""
    page = int(callback.data.split("_")[-1])
    await callback_list_bots(callback, page)

# ===== ИНФОРМАЦИЯ О БОТЕ =====

@router.callback_query(F.data.startswith("bot_info_"))
async def callback_bot_info(callback: CallbackQuery):
    """Показывает информацию о конкретном боте"""
    bot_uuid = callback.data.replace("bot_info_", "")
    
    bot = await db.get_bot_by_uuid(bot_uuid)
    if not bot:
        await callback.message.edit_text(
            "❌ Бот не найден.",
            reply_markup=Keyboards.back_button("list_bots")
        )
        return
    
    status_emoji = {
        "running": "🟢",
        "stopped": "🔴",
        "starting": "🟡",
        "error": "⚠️"
    }.get(bot['status'], "⚪")
    
    status_text = {
        "running": "Работает",
        "stopped": "Остановлен",
        "starting": "Запускается",
        "error": "Ошибка"
    }.get(bot['status'], "Неизвестно")
    
    text = (
        f"<b>{status_emoji} {bot['bot_name'] or 'Бот'}</b>\n\n"
        f"📌 <b>UUID:</b> <code>{bot['uuid']}</code>\n"
        f"🤖 <b>Username:</b> @{bot['bot_username'] or 'не указан'}\n"
        f"📊 <b>Статус:</b> {status_text}\n"
        f"📅 <b>Создан:</b> {bot['created_at'].strftime('%d.%m.%Y %H:%M') if bot['created_at'] else 'N/A'}\n"
    )
    
    if bot['last_started']:
        text += f"▶️ <b>Последний запуск:</b> {bot['last_started'].strftime('%d.%m.%Y %H:%M')}\n"
    if bot['last_stopped']:
        text += f"⏹ <b>Последняя остановка:</b> {bot['last_stopped'].strftime('%d.%m.%Y %H:%M')}\n"
    if bot['error_message']:
        text += f"\n⚠️ <b>Ошибка:</b>\n<code>{bot['error_message'][:200]}</code>\n"
    
    await callback.message.edit_text(
        text,
        reply_markup=Keyboards.bot_controls(bot['uuid'], bot['status'])
    )

# ===== ЗАПУСК БОТА =====

@router.callback_query(F.data.startswith("bot_start_"))
async def callback_bot_start(callback: CallbackQuery):
    """Запускает бота"""
    bot_uuid = callback.data.replace("bot_start_", "")
    
    await callback.message.edit_text(f"🟡 Запускаем бота...")
    
    bot = await db.get_bot_by_uuid(bot_uuid)
    if not bot:
        await callback.message.edit_text(
            "❌ Бот не найден.",
            reply_markup=Keyboards.back_button("list_bots")
        )
        return
    
    if bot['status'] == "running":
        await callback.message.edit_text(
            "❌ Бот уже запущен!",
            reply_markup=Keyboards.bot_controls(bot_uuid, bot['status'])
        )
        return
    
    if not bot['bot_token']:
        await callback.message.edit_text(
            "❌ У бота нет токена!",
            reply_markup=Keyboards.bot_controls(bot_uuid, bot['status'])
        )
        return
    
    # Обновляем статус
    await db.update_bot_status(bot_uuid, "starting")
    
    # Запускаем процесс
    pid = await process_manager.start_bot(bot_uuid, bot['bot_token'])
    
    if pid:
        await db.update_bot_status(bot_uuid, "running", pid)
        await callback.message.edit_text(
            "✅ <b>Бот успешно запущен!</b>",
            reply_markup=Keyboards.bot_controls(bot_uuid, "running")
        )
    else:
        await db.update_bot_status(bot_uuid, "error", error="Failed to start")
        await callback.message.edit_text(
            "❌ <b>Ошибка запуска</b>\n\n"
            "Проверьте токен и попробуйте снова.",
            reply_markup=Keyboards.bot_controls(bot_uuid, "error")
        )

# ===== ОСТАНОВКА БОТА =====

@router.callback_query(F.data.startswith("bot_stop_"))
async def callback_bot_stop(callback: CallbackQuery):
    """Останавливает бота"""
    bot_uuid = callback.data.replace("bot_stop_", "")
    
    await callback.message.edit_text(f"⏹ Останавливаем бота...")
    
    success = await process_manager.stop_bot(bot_uuid)
    
    if success:
        await db.update_bot_status(bot_uuid, "stopped")
        await callback.message.edit_text(
            "✅ <b>Бот остановлен</b>",
            reply_markup=Keyboards.bot_controls(bot_uuid, "stopped")
        )
    else:
        bot = await db.get_bot_by_uuid(bot_uuid)
        await callback.message.edit_text(
            "❌ <b>Ошибка остановки</b>",
            reply_markup=Keyboards.bot_controls(bot_uuid, bot['status'] if bot else "error")
        )

# ===== ПЕРЕЗАПУСК БОТА =====

@router.callback_query(F.data.startswith("bot_restart_"))
async def callback_bot_restart(callback: CallbackQuery):
    """Перезапускает бота"""
    bot_uuid = callback.data.replace("bot_restart_", "")
    
    await callback.message.edit_text(f"🔄 Перезапускаем бота...")
    
    # Сначала останавливаем
    await process_manager.stop_bot(bot_uuid)
    await asyncio.sleep(2)
    
    # Затем запускаем
    bot = await db.get_bot_by_uuid(bot_uuid)
    if bot and bot['bot_token']:
        pid = await process_manager.start_bot(bot_uuid, bot['bot_token'])
        if pid:
            await db.update_bot_status(bot_uuid, "running", pid)
            await callback.message.edit_text(
                "✅ <b>Бот перезапущен</b>",
                reply_markup=Keyboards.bot_controls(bot_uuid, "running")
            )
        else:
            await db.update_bot_status(bot_uuid, "error", error="Restart failed")
            await callback.message.edit_text(
                "❌ <b>Ошибка перезапуска</b>",
                reply_markup=Keyboards.bot_controls(bot_uuid, "error")
            )
    else:
        await callback.message.edit_text(
            "❌ Бот не найден",
            reply_markup=Keyboards.back_button("list_bots")
        )

# ===== УДАЛЕНИЕ БОТА =====

@router.callback_query(F.data.startswith("bot_delete_"))
async def callback_bot_delete(callback: CallbackQuery):
    """Удаляет бота"""
    bot_uuid = callback.data.replace("bot_delete_", "")
    
    # Сначала останавливаем если запущен
    await process_manager.stop_bot(bot_uuid)
    
    # Удаляем из БД
    success = await db.delete_bot(bot_uuid)
    
    if success:
        await callback.message.edit_text(
            "🗑 <b>Бот удален</b>",
            reply_markup=Keyboards.main_menu()
        )
    else:
        await callback.message.edit_text(
            "❌ <b>Ошибка удаления</b>",
            reply_markup=Keyboards.back_button("list_bots")
        )

# ===== АДМИН-ПАНЕЛЬ =====

@router.callback_query(F.data == "admin_panel")
async def callback_admin_panel(callback: CallbackQuery):
    """Главное меню админ-панели"""
    role = await db.get_user_role(callback.from_user.id)
    if role != 'admin':
        await callback.answer("⛔ Доступ запрещен", show_alert=True)
        return
    
    # Получаем статистику
    maintenance = await db.get_maintenance_mode()
    token_stats = await db.get_token_statistics()
    bot_stats = await db.count_bots()
    users_count = await db.count_users()
    
    maintenance_status = "🔴 ВКЛ" if maintenance['enabled'] else "🟢 ВЫКЛ"
    
    text = (
        f"👑 <b>Панель администратора</b>\n\n"
        f"<b>📊 Общая статистика:</b>\n"
        f"• Пользователей: {users_count}\n"
        f"• Ботов: {bot_stats['total']} (🟢 {bot_stats['running']})\n"
        f"• Пользователей с токенами: {token_stats['users_with_tokens']}\n\n"
        f"<b>🚧 Технические работы:</b> {maintenance_status}\n\n"
        f"<i>Выберите раздел:</i>"
    )
    
    await callback.message.edit_text(
        text,
        reply_markup=Keyboards.admin_menu()
    )

# ===== АДМИН: СТАТИСТИКА =====

@router.callback_query(F.data == "admin_stats")
async def callback_admin_stats(callback: CallbackQuery):
    """Статистика для админа"""
    role = await db.get_user_role(callback.from_user.id)
    if role not in ['admin', 'moderator']:
        await callback.answer("⛔ Доступ запрещен", show_alert=True)
        return
    
    # Собираем статистику
    users_count = await db.count_users()
    bots_stats = await db.count_bots()
    token_stats = await db.get_token_statistics()
    
    # Системная статистика
    cpu_percent = psutil.cpu_percent(interval=0.1)
    memory = psutil.virtual_memory()
    
    # Redis статистика
    redis_info = {}
    try:
        redis_info = await db.redis.info()
    except:
        redis_info = {"used_memory_human": "N/A", "connected_clients": "N/A"}
    
    # Анти-сон статистика
    anti_sleep_stats = await anti_sleep.get_stats()
    
    text = (
        f"📊 <b>Статистика системы</b>\n\n"
        f"<b>👥 Пользователи:</b>\n"
        f"• Всего: {users_count}\n"
        f"• С токенами: {token_stats['users_with_tokens']} 🟢\n"
        f"• Без токенов: {token_stats['users_without_tokens']} 🔴\n\n"
        f"<b>🤖 Боты:</b>\n"
        f"• Всего: {bots_stats['total']}\n"
        f"• Работает: {bots_stats['running']} 🟢\n"
        f"• С ошибками: {bots_stats['error']} ⚠️\n\n"
        f"<b>🖥 Система:</b>\n"
        f"• CPU: {cpu_percent}%\n"
        f"• RAM: {memory.percent}% ({memory.used // 1024**2}MB / {memory.total // 1024**2}MB)\n\n"
        f"<b>💾 Redis:</b>\n"
        f"• Память: {redis_info.get('used_memory_human', 'N/A')}\n"
        f"• Клиентов: {redis_info.get('connected_clients', 'N/A')}\n\n"
        f"<b>⏰ Anti-sleep:</b>\n"
        f"• Статус: {'✅' if anti_sleep_stats['enabled'] else '❌'}\n"
        f"• Пингов: {anti_sleep_stats['total_pings']}"
    )
    
    back_callback = "admin_back" if role == 'admin' else "mod_back"
    await callback.message.edit_text(
        text,
        reply_markup=Keyboards.back_button(back_callback)
    )

# ===== АДМИН: ПОЛЬЗОВАТЕЛИ =====

@router.callback_query(F.data == "admin_users")
async def callback_admin_users(callback: CallbackQuery, page: int = 0):
    """Список пользователей для админа"""
    role = await db.get_user_role(callback.from_user.id)
    if role != 'admin':
        await callback.answer("⛔ Доступ запрещен", show_alert=True)
        return
    
    page_size = 10
    users = await db.get_users_with_bots_status(limit=page_size, offset=page * page_size)
    total_users = await db.count_users()
    total_pages = (total_users + page_size - 1) // page_size
    
    await callback.message.edit_text(
        f"👥 <b>Управление пользователями</b>\n\n"
        f"Всего: {total_users}\n"
        f"Страница {page + 1}/{total_pages}\n\n"
        f"<i>🟢 - есть токен | 🔴 - нет токена\n"
        f"✅ - активен | ❌ - заблокирован\n"
        f"👑 - админ | 🛡️ - модератор | 👤 - пользователь</i>",
        reply_markup=Keyboards.admin_users_list(users, page, total_pages, is_moderator=False)
    )

@router.callback_query(F.data.startswith("admin_users_page_"))
async def callback_admin_users_page(callback: CallbackQuery):
    """Пагинация пользователей для админа"""
    page = int(callback.data.split("_")[-1])
    await callback_admin_users(callback, page)

@router.callback_query(F.data.startswith("admin_user_detail_"))
async def callback_admin_user_detail(callback: CallbackQuery):
    """Детальная информация о пользователе для админа"""
    user_id = int(callback.data.split("_")[-1])
    
    user = await db.get_user_with_bots(user_id)
    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    
    # Формируем информацию о токенах
    token_info = ""
    if user.get('bots'):
        token_info = "\n<b>🤖 Боты и токены:</b>\n"
        for bot in user['bots']:
            token_status = "🟢" if bot.get('bot_token') else "🔴"
            bot_name = bot.get('bot_name') or bot.get('bot_username') or bot['uuid'][:8]
            token_preview = bot['bot_token'][:10] + "..." if bot.get('bot_token') else "нет токена"
            token_info += f"{token_status} {bot_name}: <code>{token_preview}</code>\n"
    
    role_icon = {
        'admin': '👑',
        'moderator': '🛡️',
        'user': '👤'
    }.get(user.get('role', 'user'), '👤')
    
    role_name = {
        'admin': 'Администратор',
        'moderator': 'Модератор',
        'user': 'Пользователь'
    }.get(user.get('role', 'user'), 'Пользователь')
    
    text = (
        f"{role_icon} <b>Пользователь: {user['hosting_login']}</b>\n\n"
        f"🆔 ID: <code>{user['id']}</code>\n"
        f"📱 Telegram ID: <code>{user['telegram_id']}</code>\n"
        f"👤 Имя: {user.get('first_name', '')} {user.get('last_name', '')}\n"
        f"📅 Регистрация: {user['created_at'].strftime('%d.%m.%Y %H:%M') if user['created_at'] else 'N/A'}\n"
        f"🔓 Последний вход: {user['last_login'].strftime('%d.%m.%Y %H:%M') if user['last_login'] else 'Никогда'}\n"
        f"✅ Активен: {'Да' if user['is_active'] else 'Нет'}\n"
        f"🎭 Роль: {role_name}\n"
        f"🤖 Всего ботов: {len(user.get('bots', []))}\n"
        f"{token_info}"
    )
    
    await callback.message.edit_text(
        text,
        reply_markup=Keyboards.admin_user_detail(user, is_moderator=False)
    )

@router.callback_query(F.data.startswith("admin_user_toggle_"))
async def callback_admin_user_toggle(callback: CallbackQuery):
    """Блокировка/разблокировка пользователя"""
    user_id = int(callback.data.split("_")[-1])
    
    success = await db.toggle_user_active(user_id)
    
    if success:
        await callback.answer("✅ Статус изменен", show_alert=True)
    else:
        await callback.answer("❌ Ошибка", show_alert=True)
    
    await callback_admin_user_detail(callback)

@router.callback_query(F.data.startswith("admin_user_make_admin_"))
async def callback_admin_user_make_admin(callback: CallbackQuery):
    """Назначение пользователя админом"""
    user_id = int(callback.data.split("_")[-1])
    
    success = await db.set_user_role(user_id, 'admin')
    
    if success:
        await callback.answer("👑 Пользователь стал администратором", show_alert=True)
    else:
        await callback.answer("❌ Ошибка", show_alert=True)
    
    await callback_admin_user_detail(callback)

@router.callback_query(F.data.startswith("admin_user_make_moderator_"))
async def callback_admin_user_make_moderator(callback: CallbackQuery):
    """Назначение пользователя модератором"""
    user_id = int(callback.data.split("_")[-1])
    
    success = await db.set_user_role(user_id, 'moderator')
    
    if success:
        await callback.answer("🛡️ Пользователь стал модератором", show_alert=True)
    else:
        await callback.answer("❌ Ошибка", show_alert=True)
    
    await callback_admin_user_detail(callback)

@router.callback_query(F.data.startswith("admin_user_make_user_"))
async def callback_admin_user_make_user(callback: CallbackQuery):
    """Снятие ролей с пользователя"""
    user_id = int(callback.data.split("_")[-1])
    
    success = await db.set_user_role(user_id, 'user')
    
    if success:
        await callback.answer("👤 Пользователь теперь обычный пользователь", show_alert=True)
    else:
        await callback.answer("❌ Ошибка", show_alert=True)
    
    await callback_admin_user_detail(callback)

# ===== АДМИН: МОДЕРАТОРЫ =====

@router.callback_query(F.data == "admin_moderators")
async def callback_admin_moderators(callback: CallbackQuery):
    """Список модераторов"""
    role = await db.get_user_role(callback.from_user.id)
    if role != 'admin':
        await callback.answer("⛔ Доступ запрещен", show_alert=True)
        return
    
    moderators = await db.get_moderators()
    
    if not moderators:
        await callback.message.edit_text(
            "👥 Нет назначенных модераторов",
            reply_markup=Keyboards.back_button("admin_back")
        )
        return
    
    await callback.message.edit_text(
        "👥 <b>Список модераторов</b>\n\n"
        f"Всего: {len(moderators)}",
        reply_markup=Keyboards.admin_moderators_list(moderators)
    )

@router.callback_query(F.data == "admin_add_moderator")
async def callback_admin_add_moderator(callback: CallbackQuery, state: FSMContext):
    """Добавление модератора"""
    await callback.message.edit_text(
        "➕ <b>Назначение модератора</b>\n\n"
        "Введите логин пользователя, которого хотите сделать модератором:",
        reply_markup=Keyboards.back_button("admin_moderators")
    )
    await state.set_state(AdminStates.waiting_for_moderator_login)

@router.message(AdminStates.waiting_for_moderator_login)
async def process_add_moderator(message: Message, state: FSMContext):
    """Обработка добавления модератора"""
    login = message.text.strip()
    
    user = await db.get_user_by_login(login)
    if not user:
        await message.answer(
            "❌ Пользователь с таким логином не найден.",
            reply_markup=Keyboards.back_button("admin_moderators")
        )
        await state.clear()
        return
    
    if user.get('role') == 'admin':
        await message.answer(
            "❌ Этот пользователь уже является администратором.",
            reply_markup=Keyboards.back_button("admin_moderators")
        )
        await state.clear()
        return
    
    if user.get('role') == 'moderator':
        await message.answer(
            "❌ Этот пользователь уже является модератором.",
            reply_markup=Keyboards.back_button("admin_moderators")
        )
        await state.clear()
        return
    
    await db.set_user_role(user['id'], 'moderator')
    
    await message.answer(
        f"✅ <b>Модератор назначен!</b>\n\n"
        f"Пользователь {login} теперь модератор.",
        reply_markup=Keyboards.back_button("admin_moderators")
    )
    
    # Уведомляем пользователя
    try:
        await bot.send_message(
            user['telegram_id'],
            "🛡️ <b>Поздравляем!</b>\n\n"
            "Вам назначена роль модератора в BotHosting.\n"
            "Теперь вам доступна панель модератора."
        )
    except:
        pass
    
    await state.clear()

# ===== АДМИН: ТЕХНИЧЕСКИЕ РАБОТЫ =====

@router.callback_query(F.data == "admin_maintenance")
async def callback_admin_maintenance(callback: CallbackQuery):
    """Управление режимом технических работ"""
    role = await db.get_user_role(callback.from_user.id)
    if role != 'admin':
        await callback.answer("⛔ Доступ запрещен", show_alert=True)
        return
    
    status = await db.get_maintenance_mode()
    
    text = (
        f"🚧 <b>Режим технических работ</b>\n\n"
        f"Статус: {'🔴 ВКЛЮЧЕН' if status['enabled'] else '🟢 ВЫКЛЮЧЕН'}\n\n"
        f"<b>Сообщение:</b>\n"
        f"{status['message']}\n\n"
        f"<i>Когда режим включен, только администраторы могут пользоваться ботом. "
        f"Модераторы и обычные пользователи видят сообщение о ТО.</i>"
    )
    
    await callback.message.edit_text(
        text,
        reply_markup=Keyboards.admin_maintenance_menu(status)
    )

@router.callback_query(F.data == "admin_maintenance_toggle")
async def callback_admin_maintenance_toggle(callback: CallbackQuery):
    """Включение/выключение режима ТО"""
    status = await db.get_maintenance_mode()
    new_status = not status['enabled']
    
    if new_status:
        message = "🔧 Ведутся технические работы. Бот временно недоступен. Приносим извинения за неудобства."
    else:
        message = status['message']
    
    await db.set_maintenance_mode(new_status, message)
    
    # Уведомляем всех админов
    admins = await db.get_all_users(limit=100)
    for admin in admins:
        if admin.get('role') == 'admin':
            try:
                if new_status:
                    await bot.send_message(
                        admin['telegram_id'],
                        f"🚧 <b>Режим технических работ ВКЛЮЧЕН</b>\n\n"
                        f"Сообщение: {message}"
                    )
                else:
                    await bot.send_message(
                        admin['telegram_id'],
                        f"✅ <b>Режим технических работ ВЫКЛЮЧЕН</b>\n\n"
                        f"Бот снова доступен для всех пользователей."
                    )
            except:
                pass
    
    await callback_admin_maintenance(callback)

@router.callback_query(F.data == "admin_maintenance_preview")
async def callback_admin_maintenance_preview(callback: CallbackQuery):
    """Предпросмотр сообщения о ТО"""
    status = await db.get_maintenance_mode()
    
    await callback.message.edit_text(
        f"<b>👁 Предпросмотр сообщения:</b>\n\n{status['message']}",
        reply_markup=Keyboards.back_button("admin_maintenance")
    )

# ===== АДМИН: СТАТИСТИКА ТОКЕНОВ =====

@router.callback_query(F.data == "admin_token_stats")
async def callback_admin_token_stats(callback: CallbackQuery):
    """Статистика по токенам для админа"""
    role = await db.get_user_role(callback.from_user.id)
    if role not in ['admin', 'moderator']:
        await callback.answer("⛔ Доступ запрещен", show_alert=True)
        return
    
    stats = await db.get_token_statistics()
    
    text = (
        f"📊 <b>Статистика токенов</b>\n\n"
        f"<b>👥 Пользователи:</b>\n"
        f"• Всего: {stats['total_users']}\n"
        f"• С токенами: {stats['users_with_tokens']} 🟢\n"
        f"• Без токенов: {stats['users_without_tokens']} 🔴\n\n"
        f"<b>🤖 Боты:</b>\n"
        f"• Всего: {stats['total_bots']}\n"
        f"• С токенами: {stats['bots_with_tokens']} 🟢\n"
        f"• Без токенов: {stats['bots_without_tokens']} 🔴\n\n"
        f"<b>📈 Проценты:</b>\n"
        f"• Пользователей с токенами: {stats['users_with_tokens']/max(stats['total_users'],1)*100:.1f}%\n"
        f"• Ботов с токенами: {stats['bots_with_tokens']/max(stats['total_bots'],1)*100:.1f}%"
    )
    
    is_moderator = (role == 'moderator')
    await callback.message.edit_text(
        text,
        reply_markup=Keyboards.admin_token_stats(stats, is_moderator)
    )

@router.callback_query(F.data == "admin_users_no_tokens")
async def callback_admin_users_no_tokens(callback: CallbackQuery):
    """Показывает пользователей без токенов"""
    role = await db.get_user_role(callback.from_user.id)
    if role not in ['admin', 'moderator']:
        await callback.answer("⛔ Доступ запрещен", show_alert=True)
        return
    
    users = await db.get_users_with_bots_status(limit=100)
    no_token_users = [u for u in users if not u.get('has_token')]
    
    text = "🔴 <b>Пользователи без токенов</b>\n\n"
    if no_token_users:
        for user in no_token_users[:20]:
            text += f"• {user['hosting_login']} (@{user.get('username', 'no')})\n"
        if len(no_token_users) > 20:
            text += f"\n... и еще {len(no_token_users) - 20}"
    else:
        text += "✅ Все пользователи имеют токены!"
    
    back_callback = "admin_token_stats" if role == 'admin' else "mod_token_stats"
    await callback.message.edit_text(
        text,
        reply_markup=Keyboards.back_button(back_callback)
    )

# ===== АДМИН: ЭКСПОРТ ПОЛЬЗОВАТЕЛЕЙ =====

@router.callback_query(F.data == "admin_export_users")
async def callback_admin_export_users(callback: CallbackQuery):
    """Экспорт данных пользователей"""
    role = await db.get_user_role(callback.from_user.id)
    if role not in ['admin', 'moderator']:
        await callback.answer("⛔ Доступ запрещен", show_alert=True)
        return
    
    users = await db.get_users_with_bots_status(limit=1000)
    
    # Формируем CSV
    csv_data = "ID,Логин,Telegram ID,Telegram Username,Роль,Активен,Всего ботов,Боты с токенами,Работает ботов,Дата регистрации\n"
    for user in users:
        csv_data += (
            f"{user['id']},"
            f"{user['hosting_login']},"
            f"{user['telegram_id']},"
            f"@{user.get('username', '')},"
            f"{user.get('role', 'user')},"
            f"{'Да' if user['is_active'] else 'Нет'},"
            f"{user.get('total_bots', 0)},"
            f"{user.get('bots_with_token', 0)},"
            f"{user.get('running_bots', 0)},"
            f"{user['created_at']}\n"
        )
    
    # Отправляем файл
    import io
    file = io.BytesIO(csv_data.encode())
    filename = f"users_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    file.name = filename
    
    await callback.message.answer_document(
        document=BufferedInputFile(file.getvalue(), filename=filename),
        caption="📊 Экспорт пользователей"
    )
    
    await callback.answer("✅ Файл отправлен", show_alert=False)

# ===== АДМИН: ОЧИСТКА КЕША =====

@router.callback_query(F.data == "admin_clear_cache")
async def callback_admin_clear_cache(callback: CallbackQuery):
    """Очистка всех кешей"""
    role = await db.get_user_role(callback.from_user.id)
    if role != 'admin':
        await callback.answer("⛔ Доступ запрещен", show_alert=True)
        return
    
    try:
        await db.redis.flushdb()
        await callback.answer("✅ Кеш очищен", show_alert=True)
    except Exception as e:
        await callback.answer(f"❌ Ошибка: {e}", show_alert=True)

# ===== АДМИН: ВСЕ БОТЫ =====

@router.callback_query(F.data == "admin_bots")
async def callback_admin_bots(callback: CallbackQuery):
    """Показывает всех ботов"""
    role = await db.get_user_role(callback.from_user.id)
    if role not in ['admin', 'moderator']:
        await callback.answer("⛔ Доступ запрещен", show_alert=True)
        return
    
    bots = await db.get_all_bots(limit=50)
    
    if not bots:
        await callback.message.edit_text(
            "📭 Нет ни одного бота",
            reply_markup=Keyboards.back_button("admin_back" if role == 'admin' else "mod_back")
        )
        return
    
    text = "🤖 <b>Все боты (последние 50)</b>\n\n"
    
    for bot in bots:
        status_emoji = "🟢" if bot['status'] == "running" else "🔴" if bot['status'] == "stopped" else "⚠️"
        name = bot['bot_name'] or bot['bot_username'] or bot['uuid'][:8]
        token_status = "✅" if bot['bot_token'] else "❌"
        text += f"{status_emoji} {token_status} {name} (владелец: {bot['owner_id']})\n"
    
    back_callback = "admin_back" if role == 'admin' else "mod_back"
    await callback.message.edit_text(
        text,
        reply_markup=Keyboards.back_button(back_callback)
    )

# ===== АДМИН: РАССЫЛКА =====

@router.callback_query(F.data == "admin_broadcast")
async def callback_admin_broadcast(callback: CallbackQuery, state: FSMContext):
    """Начало рассылки"""
    role = await db.get_user_role(callback.from_user.id)
    if role != 'admin':
        await callback.answer("⛔ Доступ запрещен", show_alert=True)
        return
    
    await callback.message.edit_text(
        "📢 <b>Создание рассылки</b>\n\n"
        "Введите текст сообщения для рассылки всем пользователям:",
        reply_markup=Keyboards.back_button("admin_back")
    )
    await state.set_state(AdminStates.waiting_for_broadcast)

@router.message(AdminStates.waiting_for_broadcast)
async def process_broadcast(message: Message, state: FSMContext):
    """Отправка рассылки"""
    text = message.html_text
    
    # Получаем всех пользователей
    users = await db.get_all_users(limit=1000)
    
    await message.answer(f"📨 Начинаю рассылку {len(users)} пользователям...")
    
    # Отправляем сообщения
    success = 0
    failed = 0
    
    for user in users:
        try:
            await bot.send_message(user['telegram_id'], text, parse_mode="HTML")
            success += 1
            await asyncio.sleep(0.05)  # Anti-flood
        except Exception as e:
            failed += 1
            logger.error(f"Не удалось отправить сообщение {user['telegram_id']}: {e}")
    
    await message.answer(
        f"✅ Рассылка завершена!\n"
        f"Успешно: {success}\n"
        f"Ошибок: {failed}"
    )
    await state.clear()

# ===== АДМИН: ANTI-SLEEP =====

@router.callback_query(F.data == "admin_anti_sleep")
async def callback_admin_anti_sleep(callback: CallbackQuery):
    """Управление анти-сон системой"""
    role = await db.get_user_role(callback.from_user.id)
    if role != 'admin':
        await callback.answer("⛔ Доступ запрещен", show_alert=True)
        return
    
    stats = await anti_sleep.get_stats()
    
    text = (
        f"⏰ <b>Anti-sleep система</b>\n\n"
        f"Статус: {'✅ Включена' if stats['enabled'] else '❌ Отключена'}\n"
        f"Работает: {'✅' if stats['running'] else '❌'}\n"
        f"Интервал: {stats['interval']} секунд\n"
        f"Всего пингов: {stats['total_pings']}\n"
        f"За текущую сессию: {stats['session_pings']}\n"
        f"Последний пинг: {stats['last_ping'] or 'Никогда'}\n\n"
        f"<b>Цели для пинга:</b>\n"
    )
    
    for target in stats['targets']:
        text += f"  • {target}\n"
    
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"{'🔴 Выключить' if stats['running'] else '🟢 Включить'}",
                    callback_data="admin_anti_sleep_toggle"
                )
            ],
            [
                InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")
            ]
        ]
    )
    
    await callback.message.edit_text(text, reply_markup=keyboard)

@router.callback_query(F.data == "admin_anti_sleep_toggle")
async def callback_admin_anti_sleep_toggle(callback: CallbackQuery):
    """Включение/выключение анти-сон"""
    if anti_sleep.is_running:
        await anti_sleep.stop()
    else:
        await anti_sleep.start()
    
    await callback_admin_anti_sleep(callback)

# ===== АДМИН: НАЗАД =====

@router.callback_query(F.data == "admin_back")
async def callback_admin_back(callback: CallbackQuery):
    """Возврат в админ-панель"""
    await callback_admin_panel(callback)

# ===== ПАНЕЛЬ МОДЕРАТОРА =====

@router.callback_query(F.data == "mod_panel")
async def callback_mod_panel(callback: CallbackQuery):
    """Панель модератора"""
    role = await db.get_user_role(callback.from_user.id)
    if role != 'moderator':
        await callback.answer("⛔ Доступ запрещен", show_alert=True)
        return
    
    # Получаем статистику
    token_stats = await db.get_token_statistics()
    bot_stats = await db.count_bots()
    users_count = await db.count_users()
    
    text = (
        f"🛡️ <b>Панель модератора</b>\n\n"
        f"<b>📊 Статистика:</b>\n"
        f"• Пользователей: {users_count}\n"
        f"• Ботов: {bot_stats['total']} (🟢 {bot_stats['running']})\n"
        f"• Пользователей с токенами: {token_stats['users_with_tokens']}\n\n"
        f"<i>Модераторы могут просматривать статистику и пользователей, "
        f"но не могут управлять техническими работами и назначать роли.</i>"
    )
    
    await callback.message.edit_text(
        text,
        reply_markup=Keyboards.moderator_menu()
    )

@router.callback_query(F.data == "mod_stats")
async def callback_mod_stats(callback: CallbackQuery):
    """Статистика для модератора"""
    await callback_admin_stats(callback)  # Переиспользуем функцию админа

@router.callback_query(F.data == "mod_users")
async def callback_mod_users(callback: CallbackQuery, page: int = 0):
    """Список пользователей для модератора"""
    role = await db.get_user_role(callback.from_user.id)
    if role != 'moderator':
        await callback.answer("⛔ Доступ запрещен", show_alert=True)
        return
    
    page_size = 10
    users = await db.get_users_with_bots_status(limit=page_size, offset=page * page_size)
    total_users = await db.count_users()
    total_pages = (total_users + page_size - 1) // page_size
    
    await callback.message.edit_text(
        f"👥 <b>Пользователи</b> (просмотр)\n\n"
        f"Всего: {total_users}\n"
        f"Страница {page + 1}/{total_pages}\n\n"
        f"<i>🟢 - есть токен | 🔴 - нет токена\n"
        f"✅ - активен | ❌ - заблокирован</i>",
        reply_markup=Keyboards.admin_users_list(users, page, total_pages, is_moderator=True)
    )

@router.callback_query(F.data.startswith("mod_users_page_"))
async def callback_mod_users_page(callback: CallbackQuery):
    """Пагинация пользователей для модератора"""
    page = int(callback.data.split("_")[-1])
    await callback_mod_users(callback, page)

@router.callback_query(F.data.startswith("mod_user_detail_"))
async def callback_mod_user_detail(callback: CallbackQuery):
    """Детальная информация о пользователе для модератора"""
    user_id = int(callback.data.split("_")[-1])
    
    user = await db.get_user_with_bots(user_id)
    if not user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return
    
    # Формируем информацию о токенах
    token_info = ""
    if user.get('bots'):
        token_info = "\n<b>🤖 Боты и токены:</b>\n"
        for bot in user['bots']:
            token_status = "🟢" if bot.get('bot_token') else "🔴"
            bot_name = bot.get('bot_name') or bot.get('bot_username') or bot['uuid'][:8]
            token_preview = bot['bot_token'][:10] + "..." if bot.get('bot_token') else "нет токена"
            token_info += f"{token_status} {bot_name}: <code>{token_preview}</code>\n"
    
    role_icon = {
        'admin': '👑',
        'moderator': '🛡️',
        'user': '👤'
    }.get(user.get('role', 'user'), '👤')
    
    role_name = {
        'admin': 'Администратор',
        'moderator': 'Модератор',
        'user': 'Пользователь'
    }.get(user.get('role', 'user'), 'Пользователь')
    
    text = (
        f"{role_icon} <b>Пользователь: {user['hosting_login']}</b>\n\n"
        f"🆔 ID: <code>{user['id']}</code>\n"
        f"📱 Telegram ID: <code>{user['telegram_id']}</code>\n"
        f"👤 Имя: {user.get('first_name', '')} {user.get('last_name', '')}\n"
        f"📅 Регистрация: {user['created_at'].strftime('%d.%m.%Y %H:%M') if user['created_at'] else 'N/A'}\n"
        f"✅ Активен: {'Да' if user['is_active'] else 'Нет'}\n"
        f"🎭 Роль: {role_name}\n"
        f"🤖 Всего ботов: {len(user.get('bots', []))}\n"
        f"{token_info}"
    )
    
    await callback.message.edit_text(
        text,
        reply_markup=Keyboards.admin_user_detail(user, is_moderator=True)
    )

@router.callback_query(F.data == "mod_bots")
async def callback_mod_bots(callback: CallbackQuery):
    """Показывает всех ботов для модератора"""
    await callback_admin_bots(callback)  # Переиспользуем функцию админа

@router.callback_query(F.data == "mod_token_stats")
async def callback_mod_token_stats(callback: CallbackQuery):
    """Статистика токенов для модератора"""
    await callback_admin_token_stats(callback)  # Переиспользуем функцию админа

@router.callback_query(F.data == "mod_users_no_tokens")
async def callback_mod_users_no_tokens(callback: CallbackQuery):
    """Пользователи без токенов для модератора"""
    await callback_admin_users_no_tokens(callback)  # Переиспользуем функцию админа

@router.callback_query(F.data == "mod_export_users")
async def callback_mod_export_users(callback: CallbackQuery):
    """Экспорт для модератора"""
    await callback_admin_export_users(callback)  # Переиспользуем функцию админа

@router.callback_query(F.data == "mod_back")
async def callback_mod_back(callback: CallbackQuery):
    """Возврат в панель модератора"""
    await callback_mod_panel(callback)

# ===== О НАС =====

@router.callback_query(F.data == "about")
async def callback_about(callback: CallbackQuery):
    """Информация о проекте"""
    text = (
        "ℹ️ <b>О BotHosting</b>\n\n"
        "BotHosting - это бесплатная платформа для хостинга Telegram ботов.\n\n"
        "<b>Особенности:</b>\n"
        "• Бесплатно навсегда\n"
        "• До 5 ботов на пользователя\n"
        "• 24/7 доступность\n"
        "• Мониторинг и статистика\n"
        "• Простое управление\n\n"
        "<b>Контакты:</b>\n"
        "• @botfather - создание ботов\n"
        "• @bothosting_support - поддержка\n\n"
        "<b>Версия:</b> 4.1"
    )
    
    await callback.message.edit_text(
        text,
        reply_markup=Keyboards.back_button("back_to_menu")
    )

# ===== ГЛОБАЛЬНЫЕ ПЕРЕМЕННЫЕ =====

db = Database()
process_manager = BotProcessManager()
anti_sleep = AntiSleepManager()
bot = None

# ===== ЗАПУСК =====

async def on_startup():
    """Действия при запуске"""
    global bot
    
    logger.info("🚀 Запуск BotHosting...")
    
    # Подключаем БД
    await db.connect()
    
    # Запускаем анти-сон
    await anti_sleep.start()
    
    # Устанавливаем команды бота
    commands = [
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="admin", description="Панель администратора"),
        BotCommand(command="mod", description="Панель модератора"),
        BotCommand(command="maintenance", description="Управление ТО (админ)")
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
    
    logger.info("✅ BotHosting запущен!")

async def on_shutdown():
    """Действия при остановке"""
    logger.info("🛑 Остановка BotHosting...")
    
    # Останавливаем анти-сон
    await anti_sleep.stop()
    
    # Останавливаем всех ботов
    for bot_uuid in list(process_manager.processes.keys()):
        await process_manager.stop_bot(bot_uuid)
    
    # Отключаем БД
    await db.disconnect()
    
    logger.info("👋 BotHosting остановлен")

@router.message(Command("admin"))
async def cmd_admin(message: Message):
    """Команда для входа в админ-панель"""
    role = await db.get_user_role(message.from_user.id)
    if role == 'admin':
        await callback_admin_panel(message)
    else:
        await message.answer("⛔ У вас нет прав администратора")

@router.message(Command("mod"))
async def cmd_mod(message: Message):
    """Команда для входа в панель модератора"""
    role = await db.get_user_role(message.from_user.id)
    if role == 'moderator':
        await callback_mod_panel(message)
    else:
        await message.answer("⛔ У вас нет прав модератора")

@router.message(Command("maintenance"))
async def cmd_maintenance(message: Message):
    """Быстрое включение/выключение ТО"""
    role = await db.get_user_role(message.from_user.id)
    if role != 'admin':
        await message.answer("⛔ Доступ запрещен")
        return
    
    args = message.text.split(maxsplit=1)
    if len(args) == 1:
        # Показываем статус
        status = await db.get_maintenance_mode()
        await message.answer(
            f"🚧 Статус ТО: {'🔴 ВКЛ' if status['enabled'] else '🟢 ВЫКЛ'}\n"
            f"Сообщение: {status['message']}\n\n"
            f"Использование:\n"
            f"/maintenance on - включить\n"
            f"/maintenance off - выключить\n"
            f"/maintenance Текст - включить с текстом"
        )
    elif args[1].lower() == "on":
        await db.set_maintenance_mode(True)
        await message.answer("🚧 Режим ТО ВКЛЮЧЕН")
    elif args[1].lower() == "off":
        await db.set_maintenance_mode(False)
        await message.answer("✅ Режим ТО ВЫКЛЮЧЕН")
    else:
        # Включаем с кастомным сообщением
        await db.set_maintenance_mode(True, args[1])
        await message.answer(f"🚧 Режим ТО ВКЛЮЧЕН\n\nСообщение: {args[1]}")

@router.callback_query(F.data == "noop")
async def callback_noop(callback: CallbackQuery):
    """Заглушка для неактивных кнопок"""
    await callback.answer()

async def main():
    """Главная функция"""
    global bot
    
    # Создаем бота
    bot = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    
    # Создаем диспетчер
    dp = Dispatcher()
    dp.include_router(router)
    
    # Регистрируем хуки
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    
    try:
        logger.info("🎯 Запуск polling...")
        await dp.start_polling(bot)
    except Exception as e:
        logger.error(f"❌ Критическая ошибка: {e}", exc_info=True)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
