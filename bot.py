#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
Telegram Bot Hosting Platform
Упрощенная версия для Render
"""

import asyncio
import logging
import os
import sys
import json
import uuid
import re
import hashlib
from datetime import datetime
from typing import Optional, Dict, List, Any

import aiohttp
import asyncpg
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardMarkup, 
    InlineKeyboardButton
)
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from config import config

# ==================== ЛОГИРОВАНИЕ ====================

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
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
                config.DATABASE_URL,
                min_size=1,
                max_size=5
            )
            await self.init_tables()
            logger.info("✅ База данных подключена")
        except Exception as e:
            logger.error(f"❌ Ошибка подключения к БД: {e}")
            raise
    
    async def init_tables(self):
        """Создание таблиц"""
        async with self.pool.acquire() as conn:
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
                    created_at TIMESTAMP DEFAULT NOW(),
                    last_login TIMESTAMP
                )
            """)
            
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
                    last_stopped TIMESTAMP
                )
            """)
    
    # === Пользователи ===
    
    async def get_user_by_telegram(self, telegram_id: int) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM users WHERE telegram_id = $1",
                telegram_id
            )
            return dict(row) if row else None
    
    async def get_user_by_login(self, login: str) -> Optional[dict]:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM users WHERE hosting_login = $1",
                login
            )
            return dict(row) if row else None
    
    async def create_user(self, telegram_id: int, username: str, first_name: str, 
                         last_name: str, login: str, password_hash: str) -> dict:
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO users (telegram_id, username, first_name, last_name, 
                                  hosting_login, hosting_password_hash, last_login)
                VALUES ($1, $2, $3, $4, $5, $6, NOW())
                RETURNING *
            """, telegram_id, username, first_name, last_name, login, password_hash)
            return dict(row)
    
    # === Боты ===
    
    async def create_bot(self, owner_id: int, bot_token: str = None, 
                        bot_username: str = None, bot_name: str = None) -> dict:
        bot_uuid = str(uuid.uuid4())
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("""
                INSERT INTO bots (uuid, owner_id, bot_token, bot_username, bot_name)
                VALUES ($1, $2, $3, $4, $5)
                RETURNING *
            """, bot_uuid, owner_id, bot_token, bot_username, bot_name)
            return dict(row)
    
    async def get_user_bots(self, owner_id: int) -> List[dict]:
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM bots WHERE owner_id = $1 ORDER BY created_at DESC",
                owner_id
            )
            return [dict(row) for row in rows]

# ==================== СОСТОЯНИЯ FSM ====================

class AuthStates(StatesGroup):
    waiting_for_reg_login = State()
    waiting_for_reg_password = State()

class BotStates(StatesGroup):
    waiting_for_token = State()

# ==================== КЛАВИАТУРЫ ====================

def main_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить бота", callback_data="add_bot")],
        [InlineKeyboardButton(text="📋 Мои боты", callback_data="list_bots")],
        [InlineKeyboardButton(text="👤 Профиль", callback_data="profile")]
    ])

def start_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📝 Регистрация", callback_data="register")],
        [InlineKeyboardButton(text="🔑 Вход", callback_data="login")]
    ])

def back_button(callback: str = "back_to_menu") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data=callback)]
    ])

def bots_list(bots: List[dict]) -> InlineKeyboardMarkup:
    keyboard = []
    for bot in bots:
        status = "🟢" if bot['status'] == "running" else "🔴"
        name = bot['bot_name'] or bot['bot_username'] or bot['uuid'][:8]
        keyboard.append([InlineKeyboardButton(
            text=f"{status} {name}",
            callback_data=f"bot_{bot['uuid']}"
        )])
    keyboard.append([InlineKeyboardButton(text="➕ Новый бот", callback_data="add_bot")])
    keyboard.append([InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_menu")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

# ==================== ХЭШИРОВАНИЕ ====================

def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode()).hexdigest()

# ==================== ОБРАБОТЧИКИ ====================

router = Router()
db = Database()
bot_instance = None

@router.message(CommandStart())
async def cmd_start(message: Message):
    user = await db.get_user_by_telegram(message.from_user.id)
    if user:
        await message.answer(
            f"👋 С возвращением, {user['hosting_login']}!",
            reply_markup=main_menu()
        )
    else:
        await message.answer(
            "🤖 Добро пожаловать в BotHosting!\n\nЗарегистрируйтесь или войдите:",
            reply_markup=start_menu()
        )

# Регистрация
@router.callback_query(F.data == "register")
async def callback_register(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "📝 Придумайте логин (только буквы и цифры):",
        reply_markup=back_button("back_to_start")
    )
    await state.set_state(AuthStates.waiting_for_reg_login)

@router.message(AuthStates.waiting_for_reg_login)
async def process_reg_login(message: Message, state: FSMContext):
    login = message.text.strip()
    if not re.match(r"^[a-zA-Z0-9_]{3,20}$", login):
        await message.answer("❌ Недопустимый логин")
        return
    
    existing = await db.get_user_by_login(login)
    if existing:
        await message.answer("❌ Логин занят")
        return
    
    await state.update_data(reg_login=login)
    await message.answer("🔐 Введите пароль (мин. 6 символов):")
    await state.set_state(AuthStates.waiting_for_reg_password)

@router.message(AuthStates.waiting_for_reg_password)
async def process_reg_password(message: Message, state: FSMContext):
    password = message.text
    if len(password) < 6:
        await message.answer("❌ Пароль слишком короткий")
        return
    
    data = await state.get_data()
    user = await db.create_user(
        telegram_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        login=data['reg_login'],
        password_hash=hash_password(password)
    )
    
    await message.answer(
        f"✅ Регистрация успешна, {data['reg_login']}!",
        reply_markup=main_menu()
    )
    await state.clear()

# Вход
@router.callback_query(F.data == "login")
async def callback_login(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "🔑 Введите логин:",
        reply_markup=back_button("back_to_start")
    )
    await state.set_state(AuthStates.waiting_for_reg_login)

@router.message(AuthStates.waiting_for_reg_login)
async def process_login(message: Message, state: FSMContext):
    login = message.text.strip()
    await state.update_data(login=login)
    await message.answer("🔐 Введите пароль:")
    await state.set_state(AuthStates.waiting_for_reg_password)

@router.message(AuthStates.waiting_for_reg_password)
async def process_login_password(message: Message, state: FSMContext):
    data = await state.get_data()
    user = await db.get_user_by_login(data['login'])
    
    if user and user['hosting_password_hash'] == hash_password(message.text):
        if user['telegram_id'] != message.from_user.id:
            async with db.pool.acquire() as conn:
                await conn.execute(
                    "UPDATE users SET telegram_id = $1 WHERE id = $2",
                    message.from_user.id, user['id']
                )
        
        await message.answer(
            f"✅ Вход выполнен, {user['hosting_login']}!",
            reply_markup=main_menu()
        )
    else:
        await message.answer("❌ Неверный логин или пароль", reply_markup=start_menu())
    
    await state.clear()

# Добавление бота
@router.callback_query(F.data == "add_bot")
async def callback_add_bot(callback: CallbackQuery, state: FSMContext):
    user = await db.get_user_by_telegram(callback.from_user.id)
    if not user:
        await callback.message.edit_text("❌ Сначала зарегистрируйтесь!", reply_markup=start_menu())
        return
    
    bots = await db.get_user_bots(user['id'])
    if len(bots) >= config.MAX_BOTS_PER_USER:
        await callback.message.edit_text(f"❌ Лимит {config.MAX_BOTS_PER_USER} ботов", reply_markup=back_button())
        return
    
    await callback.message.edit_text(
        "🤖 Отправьте токен бота от @BotFather:",
        reply_markup=back_button()
    )
    await state.set_state(BotStates.waiting_for_token)

@router.message(BotStates.waiting_for_token)
async def process_bot_token(message: Message, state: FSMContext):
    token = message.text.strip()
    if not re.match(r"^\d+:[A-Za-z0-9_-]+$", token):
        await message.answer("❌ Неверный формат токена")
        return
    
    user = await db.get_user_by_telegram(message.from_user.id)
    bot = await db.create_bot(user['id'], token)
    
    await message.answer(
        f"✅ Бот добавлен!\nUUID: {bot['uuid'][:8]}",
        reply_markup=main_menu()
    )
    await state.clear()

# Список ботов
@router.callback_query(F.data == "list_bots")
async def callback_list_bots(callback: CallbackQuery):
    user = await db.get_user_by_telegram(callback.from_user.id)
    if not user:
        await callback.message.edit_text("❌ Сначала зарегистрируйтесь!", reply_markup=start_menu())
        return
    
    bots = await db.get_user_bots(user['id'])
    if not bots:
        await callback.message.edit_text(
            "📭 У вас нет ботов",
            reply_markup=back_button()
        )
        return
    
    await callback.message.edit_text(
        f"📋 Ваши боты ({len(bots)}):",
        reply_markup=bots_list(bots)
    )

# Профиль
@router.callback_query(F.data == "profile")
async def callback_profile(callback: CallbackQuery):
    user = await db.get_user_by_telegram(callback.from_user.id)
    if not user:
        await callback.message.edit_text("❌ Сначала зарегистрируйтесь!", reply_markup=start_menu())
        return
    
    bots = await db.get_user_bots(user['id'])
    
    await callback.message.edit_text(
        f"👤 Профиль\n\n"
        f"Логин: {user['hosting_login']}\n"
        f"Ботов: {len(bots)}/{config.MAX_BOTS_PER_USER}\n"
        f"Дата: {user['created_at'].strftime('%d.%m.%Y')}",
        reply_markup=back_button()
    )

# Навигация
@router.callback_query(F.data == "back_to_start")
async def callback_back_to_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "🤖 BotHosting",
        reply_markup=start_menu()
    )

@router.callback_query(F.data == "back_to_menu")
async def callback_back_to_menu(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        "🔧 Главное меню",
        reply_markup=main_menu()
    )

# ==================== ЗАПУСК ====================

async def on_startup():
    await db.connect()
    logger.info("✅ Бот запущен!")

async def on_shutdown():
    if db.pool:
        await db.pool.close()
    logger.info("👋 Бот остановлен")

async def main():
    global bot_instance
    
    # Используем MemoryStorage вместо Redis
    storage = MemoryStorage()
    
    bot_instance = Bot(
        token=config.BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML)
    )
    
    dp = Dispatcher(storage=storage)
    dp.include_router(router)
    
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    
    try:
        await dp.start_polling(bot_instance)
    finally:
        await bot_instance.session.close()

if __name__ == "__main__":
    asyncio.run(main())
