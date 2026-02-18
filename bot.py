#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram Bot Hosting Platform
Версия: 5.0 (с PostgreSQL на Render)
"""

import os
import asyncio
import logging
import uuid
import re
import hashlib
from datetime import datetime
from typing import Optional, Dict, List, Any

import aiohttp
import asyncpg
from aiogram import Bot, Dispatcher, Router, F, types
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, 
    InlineKeyboardButton, BotCommand, BotCommandScopeDefault
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

# ==================== КОНФИГУРАЦИЯ ====================

# Токен бота (ваш)
BOT_TOKEN = "8270979575:AAGK9BnLpi-wfFTnvziUMl1vj89YRAFbIjg"

# ID администратора (ваш)
ADMIN_IDS = [8443743937]

# База данных (ваша ссылка)
DATABASE_URL = "postgresql://hosting_user:syippHobXZYzfj2gxnJx0kAbb4WiD6af@dpg-d6aujh8boq4c73dldlv0-a.oregon-postgres.render.com/hosting_db_6qz5"

# Порт для Render
PORT = int(os.getenv("PORT", 10000))

# ==================== ЛОГИРОВАНИЕ ====================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ==================== БАЗА ДАННЫХ ====================

class Database:
    def __init__(self):
        self.pool = None
    
    async def connect(self):
        """Подключение к PostgreSQL"""
        try:
            self.pool = await asyncpg.create_pool(
                DATABASE_URL,
                min_size=1,
                max_size=10,
                command_timeout=60
            )
            
            # Создаем таблицы
            await self.init_tables()
            logger.info("✅ База данных PostgreSQL подключена")
            return True
            
        except Exception as e:
            logger.error(f"❌ Ошибка подключения к БД: {e}")
            self.pool = None
            return False
    
    async def init_tables(self):
        """Создание таблиц"""
        if not self.pool:
            return
            
        async with self.pool.acquire() as conn:
            # Таблица пользователей
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id SERIAL PRIMARY KEY,
                    telegram_id BIGINT UNIQUE NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    login TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    is_active BOOLEAN DEFAULT true,
                    is_admin BOOLEAN DEFAULT false,
                    created_at TIMESTAMP DEFAULT NOW(),
                    last_login TIMESTAMP,
                    INDEX idx_telegram_id (telegram_id),
                    INDEX idx_login (login)
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
                    created_at TIMESTAMP DEFAULT NOW(),
                    last_started TIMESTAMP,
                    last_stopped TIMESTAMP,
                    INDEX idx_owner_id (owner_id),
                    INDEX idx_status (status)
                )
            """)
            
            logger.info("✅ Таблицы созданы/проверены")
    
    # ===== ПОЛЬЗОВАТЕЛИ =====
    
    async def create_user(self, telegram_id: int, username: str, first_name: str, 
                         last_name: str, login: str, password_hash: str) -> Optional[dict]:
        """Создает нового пользователя"""
        if not self.pool:
            logger.error("❌ Нет подключения к БД")
            return None
            
        try:
            async with self.pool.acquire() as conn:
                # Проверяем, существует ли пользователь
                existing = await conn.fetchval(
                    "SELECT id FROM users WHERE telegram_id = $1",
                    telegram_id
                )
                if existing:
                    logger.warning(f"⚠️ Пользователь {telegram_id} уже существует")
                    return await self.get_user_by_telegram(telegram_id)
                
                # Проверяем логин
                existing_login = await conn.fetchval(
                    "SELECT id FROM users WHERE login = $1",
                    login
                )
                if existing_login:
                    logger.warning(f"⚠️ Логин {login} уже занят")
                    return None
                
                # Создаем пользователя
                row = await conn.fetchrow("""
                    INSERT INTO users (
                        telegram_id, username, first_name, last_name, 
                        login, password_hash, created_at, last_login
                    ) VALUES ($1, $2, $3, $4, $5, $6, NOW(), NOW())
                    RETURNING id, telegram_id, login, username, first_name, last_name, created_at, is_admin
                """, telegram_id, username, first_name, last_name, login, password_hash)
                
                user = dict(row)
                logger.info(f"✅ Создан пользователь: {login} (ID: {user['id']})")
                return user
                
        except Exception as e:
            logger.error(f"❌ Ошибка создания пользователя: {e}")
            return None
    
    async def get_user_by_telegram(self, telegram_id: int) -> Optional[dict]:
        """Получает пользователя по Telegram ID"""
        if not self.pool:
            return None
            
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM users WHERE telegram_id = $1",
                    telegram_id
                )
                return dict(row) if row else None
        except Exception as e:
            logger.error(f"❌ Ошибка получения пользователя: {e}")
            return None
    
    async def get_user_by_login(self, login: str) -> Optional[dict]:
        """Получает пользователя по логину"""
        if not self.pool:
            return None
            
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM users WHERE login = $1",
                    login
                )
                return dict(row) if row else None
        except Exception as e:
            logger.error(f"❌ Ошибка получения пользователя: {e}")
            return None
    
    async def update_last_login(self, telegram_id: int):
        """Обновляет время последнего входа"""
        if not self.pool:
            return
            
        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    "UPDATE users SET last_login = NOW() WHERE telegram_id = $1",
                    telegram_id
                )
        except Exception as e:
            logger.error(f"❌ Ошибка обновления времени входа: {e}")
    
    async def get_all_users(self) -> List[dict]:
        """Получает всех пользователей"""
        if not self.pool:
            return []
            
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT 
                        u.*,
                        COUNT(b.id) as bots_count
                    FROM users u
                    LEFT JOIN bots b ON u.id = b.owner_id
                    GROUP BY u.id
                    ORDER BY u.created_at DESC
                """)
                return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"❌ Ошибка получения пользователей: {e}")
            return []
    
    # ===== БОТЫ =====
    
    async def add_bot(self, telegram_id: int, bot_token: str, 
                     bot_username: str = None, bot_name: str = None) -> Optional[dict]:
        """Добавляет бота пользователю"""
        if not self.pool:
            return None
            
        try:
            user = await self.get_user_by_telegram(telegram_id)
            if not user:
                logger.error(f"❌ Пользователь {telegram_id} не найден")
                return None
            
            bot_uuid = str(uuid.uuid4())
            
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow("""
                    INSERT INTO bots (uuid, owner_id, bot_token, bot_username, bot_name, status, created_at)
                    VALUES ($1, $2, $3, $4, $5, 'stopped', NOW())
                    RETURNING uuid, bot_token, bot_username, bot_name, status, created_at
                """, bot_uuid, user['id'], bot_token, bot_username, bot_name)
                
                bot = dict(row)
                logger.info(f"✅ Добавлен бот {bot_uuid[:8]} для пользователя {user['login']}")
                return bot
                
        except Exception as e:
            logger.error(f"❌ Ошибка добавления бота: {e}")
            return None
    
    async def get_user_bots(self, telegram_id: int) -> List[dict]:
        """Получает всех ботов пользователя"""
        if not self.pool:
            return []
            
        try:
            user = await self.get_user_by_telegram(telegram_id)
            if not user:
                return []
            
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT * FROM bots WHERE owner_id = $1 ORDER BY created_at DESC",
                    user['id']
                )
                return [dict(row) for row in rows]
        except Exception as e:
            logger.error(f"❌ Ошибка получения ботов: {e}")
            return []
    
    async def update_bot_status(self, bot_uuid: str, status: str):
        """Обновляет статус бота"""
        if not self.pool:
            return
            
        try:
            async with self.pool.acquire() as conn:
                if status == "running":
                    await conn.execute(
                        "UPDATE bots SET status = $1, last_started = NOW() WHERE uuid = $2",
                        status, bot_uuid
                    )
                else:
                    await conn.execute(
                        "UPDATE bots SET status = $1, last_stopped = NOW() WHERE uuid = $2",
                        status, bot_uuid
                    )
        except Exception as e:
            logger.error(f"❌ Ошибка обновления статуса: {e}")

# ==================== ХЭШИРОВАНИЕ ====================

def hash_password(password: str) -> str:
    """Простое хэширование SHA256"""
    return hashlib.sha256(password.encode()).hexdigest()

def verify_password(password: str, hash_str: str) -> bool:
    """Проверка пароля"""
    return hash_password(password) == hash_str

# ==================== КЛАВИАТУРЫ ====================

def main_menu() -> InlineKeyboardMarkup:
    """Главное меню"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить бота", callback_data="add_bot")],
        [InlineKeyboardButton(text="📋 Мои боты", callback_data="list_bots")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")],
        [InlineKeyboardButton(text="ℹ️ О нас", callback_data="info")]
    ])

def start_menu() -> InlineKeyboardMarkup:
    """Стартовое меню"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Регистрация", callback_data="register")],
        [InlineKeyboardButton(text="🔑 Вход", callback_data="login")],
        [InlineKeyboardButton(text="ℹ️ О нас", callback_data="info")]
    ])

def back_button(callback: str = "back") -> InlineKeyboardMarkup:
    """Кнопка назад"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data=callback)]
    ])

def bots_keyboard(bots: List[dict]) -> InlineKeyboardMarkup:
    """Клавиатура со списком ботов"""
    keyboard = []
    for bot in bots:
        status = "🟢" if bot['status'] == "running" else "🔴"
        name = bot['bot_name'] or bot['bot_username'] or bot['uuid'][:8]
        keyboard.append([
            InlineKeyboardButton(
                text=f"{status} {name}",
                callback_data=f"bot_{bot['uuid']}"
            )
        ])
    keyboard.append([InlineKeyboardButton(text="➕ Новый бот", callback_data="add_bot")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

# ==================== СОСТОЯНИЯ FSM ====================

class AuthStates(StatesGroup):
    waiting_for_login = State()
    waiting_for_password = State()
    waiting_for_reg_login = State()
    waiting_for_reg_password = State()

class BotStates(StatesGroup):
    waiting_for_token = State()

# ==================== ИНИЦИАЛИЗАЦИЯ ====================

bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher(storage=MemoryStorage())
router = Router()
db = Database()

# ==================== HEALTH CHECK ====================

from aiohttp import web

async def health_check(request):
    """Health check endpoint для Render"""
    return web.json_response({
        "status": "ok",
        "time": datetime.now().isoformat(),
        "db": "connected" if db.pool else "disconnected"
    })

async def start_web_server():
    """Запуск веб-сервера для health checks"""
    app = web.Application()
    app.router.add_get("/health", health_check)
    app.router.add_get("/", health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"✅ Health check server started on port {PORT}")

# ==================== ОБРАБОТЧИКИ ====================

@router.message(CommandStart())
async def cmd_start(message: Message):
    """Команда /start"""
    user = await db.get_user_by_telegram(message.from_user.id)
    
    if user:
        await message.answer(
            f"👋 С возвращением, {user['login']}!",
            reply_markup=main_menu()
        )
    else:
        await message.answer(
            "🤖 Добро пожаловать в BotHosting!\n\n"
            "Выберите действие:",
            reply_markup=start_menu()
        )

@router.callback_query(F.data == "register")
async def callback_register(callback: CallbackQuery, state: FSMContext):
    """Начало регистрации"""
    user = await db.get_user_by_telegram(callback.from_user.id)
    if user:
        await callback.message.edit_text(
            "❌ Вы уже зарегистрированы!",
            reply_markup=back_button("back")
        )
        return
    
    await callback.message.edit_text(
        "📝 Придумайте логин (только буквы и цифры, от 3 до 20 символов):",
        reply_markup=back_button("back")
    )
    await state.set_state(AuthStates.waiting_for_reg_login)

@router.message(AuthStates.waiting_for_reg_login)
async def process_reg_login(message: Message, state: FSMContext):
    """Обработка логина при регистрации"""
    login = message.text.strip()
    
    if not re.match(r"^[a-zA-Z0-9_]{3,20}$", login):
        await message.answer("❌ Недопустимый логин. Используйте буквы, цифры и _, от 3 до 20 символов.")
        return
    
    # Проверяем уникальность логина
    existing = await db.get_user_by_login(login)
    if existing:
        await message.answer("❌ Этот логин уже занят. Выберите другой.")
        return
    
    await state.update_data(reg_login=login)
    await message.answer(
        "🔐 Введите пароль (минимум 6 символов):",
        reply_markup=back_button("back")
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
    
    # Создаем пользователя
    user = await db.create_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username or "",
        first_name=message.from_user.first_name or "",
        last_name=message.from_user.last_name or "",
        login=login,
        password_hash=password_hash
    )
    
    if user:
        await message.answer(
            f"✅ <b>Регистрация успешна!</b>\n\n"
            f"Добро пожаловать, {login}!",
            reply_markup=main_menu()
        )
    else:
        await message.answer(
            "❌ Ошибка регистрации. Попробуйте позже.",
            reply_markup=start_menu()
        )
    
    await state.clear()

@router.callback_query(F.data == "login")
async def callback_login(callback: CallbackQuery, state: FSMContext):
    """Начало входа"""
    user = await db.get_user_by_telegram(callback.from_user.id)
    if user:
        await callback.message.edit_text(
            "❌ Вы уже вошли в систему!",
            reply_markup=back_button("back")
        )
        return
    
    await callback.message.edit_text(
        "🔑 Введите ваш логин:",
        reply_markup=back_button("back")
    )
    await state.set_state(AuthStates.waiting_for_login)

@router.message(AuthStates.waiting_for_login)
async def process_login(message: Message, state: FSMContext):
    """Обработка логина при входе"""
    login = message.text.strip()
    await state.update_data(login=login)
    await message.answer(
        "🔐 Введите пароль:",
        reply_markup=back_button("back")
    )
    await state.set_state(AuthStates.waiting_for_password)

@router.message(AuthStates.waiting_for_password)
async def process_login_password(message: Message, state: FSMContext):
    """Обработка пароля при входе"""
    data = await state.get_data()
    login = data['login']
    password = message.text
    
    user = await db.get_user_by_login(login)
    
    if user and verify_password(password, user['password_hash']):
        # Обновляем Telegram ID если нужно
        if user['telegram_id'] != message.from_user.id:
            async with db.pool.acquire() as conn:
                await conn.execute(
                    "UPDATE users SET telegram_id = $1 WHERE id = $2",
                    message.from_user.id, user['id']
                )
        
        await db.update_last_login(message.from_user.id)
        
        await message.answer(
            f"✅ <b>Вход выполнен!</b>\n\n"
            f"Добро пожаловать, {login}!",
            reply_markup=main_menu()
        )
    else:
        await message.answer(
            "❌ Неверный логин или пароль.",
            reply_markup=start_menu()
        )
    
    await state.clear()

@router.callback_query(F.data == "add_bot")
async def callback_add_bot(callback: CallbackQuery, state: FSMContext):
    """Добавление бота"""
    user = await db.get_user_by_telegram(callback.from_user.id)
    if not user:
        await callback.message.edit_text(
            "❌ Сначала зарегистрируйтесь!",
            reply_markup=start_menu()
        )
        return
    
    await callback.message.edit_text(
        "🤖 Отправьте токен бота от @BotFather:",
        reply_markup=back_button("back")
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
            reply_markup=back_button("back")
        )
        return
    
    # Проверяем токен через Telegram API
    async with aiohttp.ClientSession() as session:
        url = f"https://api.telegram.org/bot{token}/getMe"
        async with session.get(url) as response:
            if response.status != 200:
                await message.answer(
                    "❌ Неверный токен или бот не существует.",
                    reply_markup=back_button("back")
                )
                return
            data = await response.json()
            if not data.get('ok'):
                await message.answer(
                    "❌ Ошибка проверки токена.",
                    reply_markup=back_button("back")
                )
                return
            bot_info = data['result']
    
    # Добавляем бота
    bot_result = await db.add_bot(
        telegram_id=message.from_user.id,
        bot_token=token,
        bot_username=bot_info.get('username'),
        bot_name=bot_info.get('first_name')
    )
    
    if bot_result:
        await message.answer(
            f"✅ <b>Бот успешно добавлен!</b>\n\n"
            f"Имя: {bot_info.get('first_name')}\n"
            f"Username: @{bot_info.get('username')}\n"
            f"UUID: {bot_result['uuid'][:8]}",
            reply_markup=main_menu()
        )
    else:
        await message.answer(
            "❌ Ошибка при добавлении бота.",
            reply_markup=main_menu()
        )
    
    await state.clear()

@router.callback_query(F.data == "list_bots")
async def callback_list_bots(callback: CallbackQuery):
    """Список ботов пользователя"""
    user = await db.get_user_by_telegram(callback.from_user.id)
    if not user:
        await callback.message.edit_text(
            "❌ Сначала зарегистрируйтесь!",
            reply_markup=start_menu()
        )
        return
    
    bots = await db.get_user_bots(callback.from_user.id)
    
    if not bots:
        await callback.message.edit_text(
            "📭 У вас пока нет ботов.\n\n"
            "Нажмите '➕ Добавить бота' чтобы создать первого бота.",
            reply_markup=back_button("back")
        )
        return
    
    await callback.message.edit_text(
        f"📋 <b>Ваши боты</b>\n\n"
        f"Всего: {len(bots)}",
        reply_markup=bots_keyboard(bots)
    )

@router.callback_query(F.data == "profile")
async def callback_profile(callback: CallbackQuery):
    """Профиль пользователя"""
    user = await db.get_user_by_telegram(callback.from_user.id)
    if not user:
        await callback.message.edit_text(
            "❌ Сначала зарегистрируйтесь!",
            reply_markup=start_menu()
        )
        return
    
    bots = await db.get_user_bots(callback.from_user.id)
    
    await callback.message.edit_text(
        f"👤 <b>Профиль</b>\n\n"
        f"🔑 Логин: {user['login']}\n"
        f"🆔 Telegram ID: <code>{user['telegram_id']}</code>\n"
        f"📅 Дата регистрации: {user['created_at'].strftime('%d.%m.%Y %H:%M') if user['created_at'] else 'N/A'}\n"
        f"🤖 Всего ботов: {len(bots)}\n"
        f"👑 Админ: {'Да' if user.get('is_admin') or user['telegram_id'] in ADMIN_IDS else 'Нет'}",
        reply_markup=back_button("back")
    )

@router.callback_query(F.data == "info")
async def callback_info(callback: CallbackQuery):
    """Информация о проекте"""
    await callback.message.edit_text(
        "ℹ️ <b>BotHosting</b>\n\n"
        "Платформа для хостинга Telegram ботов\n\n"
        "🔹 Бесплатно\n"
        "🔹 До 5 ботов\n"
        "🔹 PostgreSQL база данных\n"
        "🔹 24/7 доступность\n\n"
        f"Версия: 5.0\n"
        f"Статус БД: {'✅' if db.pool else '❌'}",
        reply_markup=back_button("back")
    )

@router.callback_query(F.data == "back")
async def callback_back(callback: CallbackQuery, state: FSMContext):
    """Возврат в главное меню"""
    await state.clear()
    user = await db.get_user_by_telegram(callback.from_user.id)
    
    if user:
        await callback.message.edit_text(
            "🔧 Главное меню",
            reply_markup=main_menu()
        )
    else:
        await callback.message.edit_text(
            "🤖 BotHosting",
            reply_markup=start_menu()
        )

@router.callback_query(F.data.startswith("bot_"))
async def callback_bot_info(callback: CallbackQuery):
    """Информация о конкретном боте"""
    bot_uuid = callback.data.replace("bot_", "")
    
    # Здесь можно добавить информацию о конкретном боте
    await callback.message.edit_text(
        f"🤖 Информация о боте\n\n"
        f"UUID: {bot_uuid}\n\n"
        f"Функция в разработке",
        reply_markup=back_button("list_bots")
    )

# ==================== АДМИН-КОМАНДЫ ====================

@router.message(Command("users"))
async def cmd_users(message: Message):
    """Показать всех пользователей (только админ)"""
    if message.from_user.id not in ADMIN_IDS:
        await message.answer("⛔ Доступ запрещен")
        return
    
    users = await db.get_all_users()
    
    text = "📊 <b>Все пользователи:</b>\n\n"
    for user in users:
        text += f"• <b>{user['login']}</b> (@{user['username'] or 'none'})\n"
        text += f"  ID: {user['id']}, TG: {user['telegram_id']}\n"
        text += f"  Ботов: {user.get('bots_count', 0)}\n"
        text += f"  Создан: {user['created_at'].strftime('%d.%m.%Y')}\n\n"
    
    # Разбиваем на части если слишком длинное
    if len(text) > 4000:
        for i in range(0, len(text), 4000):
            await message.answer(text[i:i+4000])
    else:
        await message.answer(text)

@router.message(Command("stats"))
async def cmd_stats(message: Message):
    """Статистика системы"""
    users = await db.get_all_users()
    total_users = len(users)
    total_bots = sum(u.get('bots_count', 0) for u in users)
    
    text = (
        f"📊 <b>Статистика BotHosting</b>\n\n"
        f"👥 Пользователей: {total_users}\n"
        f"🤖 Всего ботов: {total_bots}\n"
        f"💾 База данных: {'✅' if db.pool else '❌'}\n"
        f"🕐 Время: {datetime.now().strftime('%d.%m.%Y %H:%M:%S')}"
    )
    
    await message.answer(text)

# ==================== ЗАПУСК ====================

async def on_startup():
    """Действия при запуске"""
    # Подключаемся к базе данных
    await db.connect()
    
    # Запускаем health check сервер
    await start_web_server()
    
    # Устанавливаем команды бота
    commands = [
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="users", description="Список пользователей (админ)"),
        BotCommand(command="stats", description="Статистика системы")
    ]
    await bot.set_my_commands(commands, scope=BotCommandScopeDefault())
    
    logger.info("✅ Бот запущен!")

async def on_shutdown():
    """Действия при остановке"""
    if db.pool:
        await db.pool.close()
    logger.info("👋 Бот остановлен")

async def main():
    """Главная функция"""
    dp.include_router(router)
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    asyncio.run(main())
