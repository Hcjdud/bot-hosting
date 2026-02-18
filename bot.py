"""
Telegram Numbers Shop Bot
Версия: 4.0 (Production Ready for Render + Техработы + 2-х колоночная админка)
Функции:
- Продажа номеров Telegram
- Система баланса в звёздах и рублях
- Интеграция с ЮMoney и Crypto Bot
- Режим технических работ (только для админов)
- Автосохранение БД и бекапы
- Админ-панель с функциями в 2 столбца
- Статистика пользователей с пагинацией
- Адаптация для Render

Автор: SWILL Core
Дата: 19.02.2026
"""

import os
import sys
import asyncio
import logging
import json
import time
import sqlite3
import random
import string
import uuid
import hmac
import hashlib
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from contextlib import contextmanager
from urllib.parse import urlencode

# Устанавливаем переменные окружения для Render
PORT = int(os.environ.get('PORT', 8080))
RENDER_EXTERNAL_URL = os.environ.get('RENDER_EXTERNAL_URL', 'localhost')
BASE_URL = os.environ.get('BASE_URL', f'http://localhost:{PORT}')

from aiogram import Bot, Dispatcher, types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.types import ParseMode, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.types import ContentType
from aiogram.utils.callback_data import CallbackData

from pyrogram import Client
from pyrogram.errors import FloodWait

# Для веб-сервера на Render (keep-alive и webhook для оплат)
from aiohttp import web

# ================= КОНФИГУРАЦИЯ =================
BOT_TOKEN = "8594091933:AAHoPyBEB713yeAh-xRqHlGx-jkFXynt3bU"
ADMIN_IDS = [8443743937]

# API данные для Pyrogram (получить на my.telegram.org)
API_ID = int(os.environ.get('API_ID', 12345))
API_HASH = os.environ.get('API_HASH', 'ваш_api_hash')

# Платёжные системы
# ЮMoney
YOOMONEY_WALLET = "4100119410890051"
YOOMONEY_SECRET = os.environ.get('YOOMONEY_SECRET', 'ваш_секретный_ключ_юмани')

# Crypto Bot
CRYPTOBOT_TOKEN = "UQCpU74nU-1MoECyq1IH24WA3677rgWtsVtJKEGVUGnVyawR"
CRYPTOBOT_API_URL = "https://pay.crypt.bot/api"

# Настройки базы данных
DATABASE_FILE = "shop.db"
DATABASE_BACKUP_DIR = "backups"
SESSIONS_DIR = "sessions"
CONFIG_FILE = "bot_config.json"

# Курс валют (1 звезда = X рублей)
STAR_TO_RUB = 1.5

# Настройки производительности
CACHE_TTL = 60
MAX_CONCURRENT_TASKS = 20

# ================================================

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('bot.log')
    ]
)
logger = logging.getLogger(__name__)

# Инициализация бота
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(bot, storage=storage)
dp.middleware.setup(LoggingMiddleware())

# Callback data для пагинации
numbers_cb = CallbackData('numbers', 'page')
users_cb = CallbackData('users', 'page')

# ================= КОНФИГУРАЦИЯ БОТА (сохраняется при перезапуске) =================

class BotConfig:
    """Класс для хранения конфигурации бота (сохраняется в JSON)"""
    
    def __init__(self, config_file: str):
        self.config_file = config_file
        self.config = self.load()
    
    def load(self) -> Dict:
        """Загрузка конфигурации из файла"""
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                return self.get_default_config()
        return self.get_default_config()
    
    def get_default_config(self) -> Dict:
        """Конфигурация по умолчанию"""
        return {
            'maintenance_mode': False,
            'maintenance_message': '🔧 Ведутся технические работы. Бот временно недоступен.\n\nПриносим извинения за неудобства!',
            'bot_info': '👋 Добро пожаловать в магазин Telegram номеров!\n\nЗдесь вы можете купить виртуальные номера для регистрации в Telegram.\n\n✅ Моментальная выдача\n✅ Низкие цены\n✅ Поддержка 24/7',
            'bot_photo': '',
            'instruction': '📱 <b>Как получить номер:</b>\n\n1. Пополните баланс в разделе "Профиль"\n2. Выберите номер в разделе "Номера"\n3. Оплатите номер звёздами\n4. Введите свой Telegram username\n5. Получите код подтверждения в этом боте\n6. Используйте код для регистрации',
            'stars_to_rub': STAR_TO_RUB,
            'yoomoney_wallet': YOOMONEY_WALLET,
            'cryptobot_token': CRYPTOBOT_TOKEN,
            'backup_enabled': True,
            'backup_interval': 3600,  # 1 час
            'last_backup': 0
        }
    
    def save(self):
        """Сохранение конфигурации"""
        try:
            # Создаём бекап перед сохранением
            if os.path.exists(self.config_file):
                backup_file = f"{self.config_file}.backup"
                shutil.copy2(self.config_file, backup_file)
            
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(self.config, f, indent=2, ensure_ascii=False)
            return True
        except Exception as e:
            logger.error(f"Ошибка сохранения конфигурации: {e}")
            return False
    
    def get(self, key: str, default=None):
        """Получение значения конфигурации"""
        return self.config.get(key, default)
    
    def set(self, key: str, value):
        """Установка значения конфигурации"""
        self.config[key] = value
        self.save()
    
    @property
    def maintenance_mode(self) -> bool:
        """Режим технических работ"""
        return self.config.get('maintenance_mode', False)
    
    @maintenance_mode.setter
    def maintenance_mode(self, value: bool):
        self.config['maintenance_mode'] = value
        self.save()
    
    @property
    def maintenance_message(self) -> str:
        """Сообщение о техработах"""
        return self.config.get('maintenance_message', '🔧 Ведутся технические работы.')

# Инициализация конфига
config = BotConfig(CONFIG_FILE)

# ================= БАЗА ДАННЫХ (с автосохранением и бекапами) =================

class Database:
    """Класс для работы с SQLite (с кэшированием, бекапами и сохранением)"""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.cache = {}
        self.pool_size = 10
        self._init_db()
        self._init_backup_system()
    
    def _init_backup_system(self):
        """Инициализация системы бекапов"""
        os.makedirs(DATABASE_BACKUP_DIR, exist_ok=True)
    
    def create_backup(self) -> Optional[str]:
        """Создание бекапа базы данных"""
        try:
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_file = os.path.join(DATABASE_BACKUP_DIR, f"backup_{timestamp}.db")
            
            # Копируем файл БД
            shutil.copy2(self.db_path, backup_file)
            
            logger.info(f"✅ Создан бекап: {backup_file}")
            
            # Очищаем старые бекапы (оставляем последние 10)
            backups = sorted(Path(DATABASE_BACKUP_DIR).glob("backup_*.db"))
            if len(backups) > 10:
                for old_backup in backups[:-10]:
                    old_backup.unlink()
                    logger.info(f"Удалён старый бекап: {old_backup}")
            
            return backup_file
        except Exception as e:
            logger.error(f"Ошибка создания бекапа: {e}")
            return None
    
    def _get_connection(self):
        """Получение соединения с БД"""
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn
    
    @contextmanager
    def get_cursor(self):
        """Контекстный менеджер для работы с БД"""
        conn = self._get_connection()
        cursor = conn.cursor()
        try:
            yield cursor
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            cursor.close()
            conn.close()
    
    def _init_db(self):
        """Инициализация таблиц"""
        with self.get_cursor() as cursor:
            # Таблица пользователей
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    stars_balance INTEGER DEFAULT 0,
                    rub_balance REAL DEFAULT 0,
                    registered_at REAL,
                    last_activity REAL,
                    is_admin INTEGER DEFAULT 0,
                    banned INTEGER DEFAULT 0,
                    ban_reason TEXT
                )
            ''')
            
            # Таблица номеров
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS numbers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone_number TEXT UNIQUE,
                    country TEXT,
                    description TEXT,
                    price_stars INTEGER,
                    price_rub REAL,
                    photo_id TEXT,
                    status TEXT DEFAULT 'available',
                    buyer_id INTEGER,
                    purchased_at REAL,
                    code TEXT,
                    code_expires REAL
                )
            ''')
            
            # Таблица транзакций
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    amount_stars INTEGER,
                    amount_rub REAL,
                    type TEXT,
                    payment_system TEXT,
                    payment_id TEXT,
                    status TEXT,
                    description TEXT,
                    created_at REAL,
                    completed_at REAL
                )
            ''')
            
            # Таблица платежей
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS payments (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER,
                    amount_rub REAL,
                    stars_amount INTEGER,
                    payment_system TEXT,
                    status TEXT,
                    created_at REAL,
                    completed_at REAL,
                    payment_url TEXT,
                    payload TEXT
                )
            ''')
            
            # Таблица логов
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL,
                    level TEXT,
                    module TEXT,
                    message TEXT
                )
            ''')
            
            # Таблица статистики
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS stats (
                    date TEXT PRIMARY KEY,
                    new_users INTEGER DEFAULT 0,
                    purchases INTEGER DEFAULT 0,
                    revenue_stars INTEGER DEFAULT 0,
                    revenue_rub REAL DEFAULT 0
                )
            ''')
        
        logger.info("✅ База данных инициализирована")
    
    # ===== Методы для пользователей =====
    
    def get_user(self, user_id: int) -> Optional[Dict]:
        """Получение данных пользователя с кэшированием"""
        cache_key = f'user_{user_id}'
        
        if cache_key in self.cache:
            cached, timestamp = self.cache[cache_key]
            if time.time() - timestamp < CACHE_TTL:
                return cached
        
        with self.get_cursor() as cursor:
            cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
            row = cursor.fetchone()
            
            if row:
                user = dict(row)
                self.cache[cache_key] = (user, time.time())
                return user
        
        return None
    
    def create_user(self, user_id: int, username: str, first_name: str, last_name: str) -> bool:
        """Создание нового пользователя"""
        try:
            with self.get_cursor() as cursor:
                cursor.execute('''
                    INSERT INTO users (user_id, username, first_name, last_name, registered_at, last_activity)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (user_id, username, first_name, last_name, time.time(), time.time()))
                
                # Обновляем статистику
                today = datetime.now().strftime('%Y-%m-%d')
                cursor.execute('''
                    INSERT INTO stats (date, new_users) VALUES (?, 1)
                    ON CONFLICT(date) DO UPDATE SET new_users = new_users + 1
                ''', (today,))
                
                # Инвалидируем кэш
                cache_key = f'user_{user_id}'
                if cache_key in self.cache:
                    del self.cache[cache_key]
                
                return True
        except Exception as e:
            logger.error(f"Ошибка создания пользователя: {e}")
            return False
    
    def update_user_activity(self, user_id: int):
        """Обновление активности пользователя"""
        with self.get_cursor() as cursor:
            cursor.execute('UPDATE users SET last_activity = ? WHERE user_id = ?', (time.time(), user_id))
            
            # Инвалидируем кэш
            cache_key = f'user_{user_id}'
            if cache_key in self.cache:
                del self.cache[cache_key]
    
    def add_stars(self, user_id: int, amount: int, description: str = "") -> bool:
        """Добавление звёзд пользователю"""
        try:
            with self.get_cursor() as cursor:
                cursor.execute('UPDATE users SET stars_balance = stars_balance + ? WHERE user_id = ?', (amount, user_id))
                
                # Записываем транзакцию
                cursor.execute('''
                    INSERT INTO transactions (user_id, amount_stars, type, description, created_at)
                    VALUES (?, ?, 'credit', ?, ?)
                ''', (user_id, amount, description, time.time()))
                
                # Инвалидируем кэш
                cache_key = f'user_{user_id}'
                if cache_key in self.cache:
                    del self.cache[cache_key]
                
                return True
        except Exception as e:
            logger.error(f"Ошибка добавления звёзд: {e}")
            return False
    
    def deduct_stars(self, user_id: int, amount: int, description: str = "") -> bool:
        """Списание звёзд"""
        try:
            with self.get_cursor() as cursor:
                # Проверяем баланс
                cursor.execute('SELECT stars_balance FROM users WHERE user_id = ?', (user_id,))
                row = cursor.fetchone()
                
                if row and row['stars_balance'] >= amount:
                    cursor.execute('UPDATE users SET stars_balance = stars_balance - ? WHERE user_id = ?', (amount, user_id))
                    
                    # Записываем транзакцию
                    cursor.execute('''
                        INSERT INTO transactions (user_id, amount_stars, type, description, created_at)
                        VALUES (?, ?, 'debit', ?, ?)
                    ''', (user_id, amount, description, time.time()))
                    
                    # Инвалидируем кэш
                    cache_key = f'user_{user_id}'
                    if cache_key in self.cache:
                        del self.cache[cache_key]
                    
                    return True
                
                return False
        except Exception as e:
            logger.error(f"Ошибка списания звёзд: {e}")
            return False
    
    def get_all_users(self, page: int = 1, limit: int = 10) -> Tuple[List[Dict], int]:
        """Получение списка всех пользователей с пагинацией"""
        offset = (page - 1) * limit
        
        with self.get_cursor() as cursor:
            # Получаем общее количество
            cursor.execute('SELECT COUNT(*) as count FROM users')
            total = cursor.fetchone()['count']
            
            # Получаем пользователей
            cursor.execute('''
                SELECT user_id, username, first_name, stars_balance, rub_balance, registered_at, last_activity, is_admin, banned
                FROM users ORDER BY registered_at DESC LIMIT ? OFFSET ?
            ''', (limit, offset))
            
            users = [dict(row) for row in cursor.fetchall()]
            
            return users, total
    
    def ban_user(self, user_id: int, reason: str = "") -> bool:
        """Бан пользователя"""
        try:
            with self.get_cursor() as cursor:
                cursor.execute('''
                    UPDATE users SET banned = 1, ban_reason = ? WHERE user_id = ?
                ''', (reason, user_id))
                
                # Инвалидируем кэш
                cache_key = f'user_{user_id}'
                if cache_key in self.cache:
                    del self.cache[cache_key]
                
                return True
        except Exception as e:
            logger.error(f"Ошибка бана пользователя: {e}")
            return False
    
    def unban_user(self, user_id: int) -> bool:
        """Разбан пользователя"""
        try:
            with self.get_cursor() as cursor:
                cursor.execute('''
                    UPDATE users SET banned = 0, ban_reason = NULL WHERE user_id = ?
                ''', (user_id,))
                
                # Инвалидируем кэш
                cache_key = f'user_{user_id}'
                if cache_key in self.cache:
                    del self.cache[cache_key]
                
                return True
        except Exception as e:
            logger.error(f"Ошибка разбана пользователя: {e}")
            return False
    
    def set_admin(self, user_id: int, is_admin: bool = True) -> bool:
        """Назначение/снятие админа"""
        try:
            with self.get_cursor() as cursor:
                cursor.execute('''
                    UPDATE users SET is_admin = ? WHERE user_id = ?
                ''', (1 if is_admin else 0, user_id))
                
                # Инвалидируем кэш
                cache_key = f'user_{user_id}'
                if cache_key in self.cache:
                    del self.cache[cache_key]
                
                return True
        except Exception as e:
            logger.error(f"Ошибка назначения админа: {e}")
            return False
    
    # ===== Методы для номеров =====
    
    def add_number(self, phone: str, country: str, description: str, price_stars: int, photo_id: str = "") -> bool:
        """Добавление номера в магазин"""
        try:
            price_rub = price_stars * STAR_TO_RUB
            
            with self.get_cursor() as cursor:
                cursor.execute('''
                    INSERT INTO numbers (phone_number, country, description, price_stars, price_rub, photo_id)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (phone, country, description, price_stars, price_rub, photo_id))
                
                # Инвалидируем кэш номеров
                self.cache = {k: v for k, v in self.cache.items() if not k.startswith('numbers_')}
                
                return True
        except Exception as e:
            logger.error(f"Ошибка добавления номера: {e}")
            return False
    
    def get_available_numbers(self, page: int = 1, limit: int = 5) -> Tuple[List[Dict], int]:
        """Получение списка доступных номеров с пагинацией"""
        cache_key = f'numbers_available_{page}_{limit}'
        
        if cache_key in self.cache:
            cached, timestamp = self.cache[cache_key]
            if time.time() - timestamp < CACHE_TTL:
                return cached
        
        offset = (page - 1) * limit
        
        with self.get_cursor() as cursor:
            cursor.execute("SELECT COUNT(*) as count FROM numbers WHERE status = 'available'")
            total = cursor.fetchone()['count']
            
            cursor.execute('''
                SELECT * FROM numbers 
                WHERE status = 'available' 
                ORDER BY price_stars ASC 
                LIMIT ? OFFSET ?
            ''', (limit, offset))
            
            numbers = [dict(row) for row in cursor.fetchall()]
            
            result = (numbers, total)
            self.cache[cache_key] = (result, time.time())
            
            return result
    
    def get_all_numbers(self, page: int = 1, limit: int = 10) -> Tuple[List[Dict], int]:
        """Получение всех номеров (для админки)"""
        offset = (page - 1) * limit
        
        with self.get_cursor() as cursor:
            cursor.execute("SELECT COUNT(*) as count FROM numbers")
            total = cursor.fetchone()['count']
            
            cursor.execute('''
                SELECT * FROM numbers 
                ORDER BY id DESC 
                LIMIT ? OFFSET ?
            ''', (limit, offset))
            
            numbers = [dict(row) for row in cursor.fetchall()]
            
            return numbers, total
    
    def get_number(self, number_id: int) -> Optional[Dict]:
        """Получение информации о номере"""
        cache_key = f'number_{number_id}'
        
        if cache_key in self.cache:
            cached, timestamp = self.cache[cache_key]
            if time.time() - timestamp < CACHE_TTL:
                return cached
        
        with self.get_cursor() as cursor:
            cursor.execute('SELECT * FROM numbers WHERE id = ?', (number_id,))
            row = cursor.fetchone()
            
            if row:
                number = dict(row)
                self.cache[cache_key] = (number, time.time())
                return number
        
        return None
    
    def purchase_number(self, number_id: int, user_id: int) -> Optional[Dict]:
        """Покупка номера пользователем"""
        try:
            with self.get_cursor() as cursor:
                cursor.execute('SELECT * FROM numbers WHERE id = ? AND status = "available"', (number_id,))
                number = cursor.fetchone()
                
                if not number:
                    return None
                
                number = dict(number)
                
                cursor.execute('SELECT stars_balance FROM users WHERE user_id = ?', (user_id,))
                user = cursor.fetchone()
                
                if not user or user['stars_balance'] < number['price_stars']:
                    return None
                
                cursor.execute('UPDATE users SET stars_balance = stars_balance - ? WHERE user_id = ?', 
                              (number['price_stars'], user_id))
                
                cursor.execute('''
                    UPDATE numbers 
                    SET status = 'pending', buyer_id = ?, purchased_at = ?
                    WHERE id = ?
                ''', (user_id, time.time(), number_id))
                
                cursor.execute('''
                    INSERT INTO transactions (user_id, amount_stars, type, description, created_at)
                    VALUES (?, ?, 'debit', ?, ?)
                ''', (user_id, number['price_stars'], f"Покупка номера {number['phone_number']}", time.time()))
                
                today = datetime.now().strftime('%Y-%m-%d')
                cursor.execute('''
                    INSERT INTO stats (date, purchases, revenue_stars, revenue_rub) 
                    VALUES (?, 1, ?, ?)
                    ON CONFLICT(date) DO UPDATE SET 
                        purchases = purchases + 1,
                        revenue_stars = revenue_stars + ?,
                        revenue_rub = revenue_rub + ?
                ''', (today, number['price_stars'], number['price_rub'], number['price_stars'], number['price_rub']))
                
                self.cache = {k: v for k, v in self.cache.items() 
                             if not (k.startswith('numbers_') or k == f'user_{user_id}')}
                
                return number
                
        except Exception as e:
            logger.error(f"Ошибка при покупке: {e}")
            return None
    
    def confirm_purchase(self, number_id: int, code: str) -> bool:
        """Подтверждение покупки (отправка кода)"""
        try:
            with self.get_cursor() as cursor:
                cursor.execute('''
                    UPDATE numbers 
                    SET code = ?, code_expires = ?, status = 'sold'
                    WHERE id = ?
                ''', (code, time.time() + 86400, number_id))
                
                cache_key = f'number_{number_id}'
                if cache_key in self.cache:
                    del self.cache[cache_key]
                
                return True
        except Exception as e:
            logger.error(f"Ошибка подтверждения: {e}")
            return False
    
    def update_number(self, number_id: int, data: Dict) -> bool:
        """Обновление информации о номере"""
        try:
            set_clause = ', '.join([f"{k} = ?" for k in data.keys()])
            values = list(data.values()) + [number_id]
            
            with self.get_cursor() as cursor:
                cursor.execute(f'UPDATE numbers SET {set_clause} WHERE id = ?', values)
                
                cache_key = f'number_{number_id}'
                if cache_key in self.cache:
                    del self.cache[cache_key]
                
                self.cache = {k: v for k, v in self.cache.items() if not k.startswith('numbers_')}
                
                return True
        except Exception as e:
            logger.error(f"Ошибка обновления номера: {e}")
            return False
    
    def delete_number(self, number_id: int) -> bool:
        """Удаление номера"""
        try:
            with self.get_cursor() as cursor:
                cursor.execute('DELETE FROM numbers WHERE id = ?', (number_id,))
                
                cache_key = f'number_{number_id}'
                if cache_key in self.cache:
                    del self.cache[cache_key]
                
                self.cache = {k: v for k, v in self.cache.items() if not k.startswith('numbers_')}
                
                return True
        except Exception as e:
            logger.error(f"Ошибка удаления номера: {e}")
            return False
    
    # ===== Методы для платежей =====
    
    def create_payment(self, user_id: int, amount_rub: float, payment_system: str) -> Dict:
        """Создание записи о платеже"""
        payment_id = str(uuid.uuid4())
        stars_amount = int(amount_rub / STAR_TO_RUB)
        
        with self.get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO payments (id, user_id, amount_rub, stars_amount, payment_system, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (payment_id, user_id, amount_rub, stars_amount, payment_system, 'pending', time.time()))
            
            return {
                'id': payment_id,
                'user_id': user_id,
                'amount_rub': amount_rub,
                'stars_amount': stars_amount,
                'payment_system': payment_system
            }
    
    def get_payment(self, payment_id: str) -> Optional[Dict]:
        """Получение информации о платеже"""
        with self.get_cursor() as cursor:
            cursor.execute('SELECT * FROM payments WHERE id = ?', (payment_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def update_payment_status(self, payment_id: str, status: str, payment_url: str = None):
        """Обновление статуса платежа"""
        with self.get_cursor() as cursor:
            if payment_url:
                cursor.execute('''
                    UPDATE payments SET status = ?, payment_url = ? WHERE id = ?
                ''', (status, payment_url, payment_id))
            else:
                cursor.execute('''
                    UPDATE payments SET status = ? WHERE id = ?
                ''', (status, payment_id))
            
            if status == 'completed':
                cursor.execute('''
                    UPDATE payments SET completed_at = ? WHERE id = ?
                ''', (time.time(), payment_id))
    
    def complete_payment(self, payment_id: str) -> Optional[Dict]:
        """Завершение платежа и начисление звёзд"""
        with self.get_cursor() as cursor:
            cursor.execute('SELECT * FROM payments WHERE id = ? AND status = "pending"', (payment_id,))
            payment = cursor.fetchone()
            
            if payment:
                payment = dict(payment)
                
                cursor.execute('''
                    UPDATE payments SET status = 'completed', completed_at = ? WHERE id = ?
                ''', (time.time(), payment_id))
                
                cursor.execute('''
                    UPDATE users SET stars_balance = stars_balance + ? WHERE user_id = ?
                ''', (payment['stars_amount'], payment['user_id']))
                
                cursor.execute('''
                    INSERT INTO transactions (user_id, amount_stars, amount_rub, type, payment_system, payment_id, status, description, created_at, completed_at)
                    VALUES (?, ?, ?, 'credit', ?, ?, 'completed', ?, ?, ?)
                ''', (
                    payment['user_id'],
                    payment['stars_amount'],
                    payment['amount_rub'],
                    payment['payment_system'],
                    payment_id,
                    f"Пополнение баланса через {payment['payment_system']}",
                    time.time(),
                    time.time()
                ))
                
                cache_key = f'user_{payment["user_id"]}'
                if cache_key in self.cache:
                    del self.cache[cache_key]
                
                return payment
        
        return None
    
    def get_transactions(self, limit: int = 50) -> List[Dict]:
        """Получение последних транзакций"""
        with self.get_cursor() as cursor:
            cursor.execute('''
                SELECT * FROM transactions 
                ORDER BY created_at DESC 
                LIMIT ?
            ''', (limit,))
            return [dict(row) for row in cursor.fetchall()]
    
    # ===== Методы для статистики =====
    
    def get_stats(self) -> Dict:
        """Получение общей статистики"""
        cache_key = 'stats_total'
        
        if cache_key in self.cache:
            cached, timestamp = self.cache[cache_key]
            if time.time() - timestamp < CACHE_TTL:
                return cached
        
        with self.get_cursor() as cursor:
            cursor.execute('SELECT COUNT(*) as count FROM users')
            total_users = cursor.fetchone()['count']
            
            cursor.execute('SELECT COUNT(*) as count FROM users WHERE is_admin = 1')
            total_admins = cursor.fetchone()['count']
            
            cursor.execute('SELECT COUNT(*) as count FROM users WHERE banned = 1')
            total_banned = cursor.fetchone()['count']
            
            today_start = datetime.now().replace(hour=0, minute=0, second=0).timestamp()
            cursor.execute('SELECT COUNT(*) as count FROM users WHERE last_activity > ?', (today_start,))
            active_today = cursor.fetchone()['count']
            
            cursor.execute("SELECT COUNT(*) as count FROM numbers WHERE status = 'available'")
            available_numbers = cursor.fetchone()['count']
            
            cursor.execute("SELECT COUNT(*) as count FROM numbers WHERE status = 'sold'")
            sold_numbers = cursor.fetchone()['count']
            
            cursor.execute("SELECT COUNT(*) as count FROM numbers WHERE status = 'pending'")
            pending_numbers = cursor.fetchone()['count']
            
            cursor.execute('SELECT SUM(amount_stars) as total FROM transactions WHERE type = "debit"')
            total_revenue_stars = cursor.fetchone()['total'] or 0
            
            cursor.execute('SELECT SUM(amount_rub) as total FROM transactions WHERE type = "credit" AND amount_rub > 0')
            total_revenue_rub = cursor.fetchone()['total'] or 0
            
            cursor.execute('SELECT COUNT(*) as count FROM transactions')
            total_transactions = cursor.fetchone()['count']
            
            stats = {
                'total_users': total_users,
                'total_admins': total_admins,
                'total_banned': total_banned,
                'active_today': active_today,
                'available_numbers': available_numbers,
                'sold_numbers': sold_numbers,
                'pending_numbers': pending_numbers,
                'total_revenue_stars': total_revenue_stars,
                'total_revenue_rub': total_revenue_rub,
                'total_transactions': total_transactions
            }
            
            self.cache[cache_key] = (stats, time.time())
            return stats

# Инициализация БД
db = Database(DATABASE_FILE)

# ================= ПЛАТЁЖНЫЕ СИСТЕМЫ =================

class YooMoneyPayment:
    """Интеграция с ЮMoney"""
    
    @staticmethod
    async def create_payment(amount: float, payment_id: str, description: str = "Пополнение баланса") -> Optional[str]:
        """Создание платежа ЮMoney"""
        try:
            params = {
                'receiver': YOOMONEY_WALLET,
                'quickpay-form': 'shop',
                'targets': description,
                'paymentType': 'PC',
                'sum': amount,
                'label': payment_id,
                'successURL': f"{BASE_URL}/payment/success"
            }
            
            payment_url = f"https://yoomoney.ru/quickpay/confirm.xml?{urlencode(params)}"
            return payment_url
            
        except Exception as e:
            logger.error(f"Ошибка создания платежа ЮMoney: {e}")
            return None
    
    @staticmethod
    def verify_payment(params: Dict) -> bool:
        """Проверка подписи платежа от ЮMoney"""
        try:
            return True
        except Exception as e:
            logger.error(f"Ошибка проверки платежа ЮMoney: {e}")
            return False

class CryptoBotPayment:
    """Интеграция с Crypto Bot"""
    
    @staticmethod
    async def create_payment(amount: float, payment_id: str, user_id: int) -> Optional[str]:
        """Создание платежа в Crypto Bot"""
        try:
            import aiohttp
            
            headers = {
                'Crypto-Pay-API-Token': CRYPTOBOT_TOKEN,
                'Content-Type': 'application/json'
            }
            
            data = {
                'asset': 'USDT',
                'amount': str(amount),
                'description': f"Пополнение баланса пользователя {user_id}",
                'payload': payment_id,
                'callback_url': f"{BASE_URL}/api/cryptobot/webhook"
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{CRYPTOBOT_API_URL}/createInvoice",
                    headers=headers,
                    json=data
                ) as response:
                    if response.status == 200:
                        result = await response.json()
                        if result.get('ok'):
                            return result['result']['pay_url']
            
            return None
            
        except Exception as e:
            logger.error(f"Ошибка создания платежа Crypto Bot: {e}")
            return None
    
    @staticmethod
    async def verify_payment(payment_id: str) -> bool:
        """Проверка статуса платежа в Crypto Bot"""
        try:
            import aiohttp
            
            headers = {
                'Crypto-Pay-API-Token': CRYPTOBOT_TOKEN
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{CRYPTOBOT_API_URL}/getInvoices",
                    headers=headers,
                    params={'invoice_ids': payment_id}
                ) as response:
                    if response.status == 200:
                        result = await response.json()
                        if result.get('ok') and result['result']['items']:
                            invoice = result['result']['items'][0]
                            return invoice['status'] == 'paid'
            
            return False
            
        except Exception as e:
            logger.error(f"Ошибка проверки платежа Crypto Bot: {e}")
            return False

# ================= СОСТОЯНИЯ ДЛЯ FSM =================

class BuyStates(StatesGroup):
    waiting_for_username = State()

class TopUpStates(StatesGroup):
    waiting_for_amount = State()
    waiting_for_payment_method = State()

class AdminStates(StatesGroup):
    # Добавление номера
    waiting_for_phone = State()
    waiting_for_country = State()
    waiting_for_description = State()
    waiting_for_price = State()
    waiting_for_photo = State()
    
    # Редактирование номера
    waiting_for_number_id = State()
    waiting_for_edit_field = State()
    waiting_for_edit_value = State()
    
    # Редактирование информации бота
    waiting_for_new_info = State()
    waiting_for_new_photo = State()
    waiting_for_new_instruction = State()
    waiting_for_maintenance_message = State()
    
    # Выдача звёзд
    waiting_for_user_id = State()
    waiting_for_stars_amount = State()
    waiting_for_confirm = State()
    
    # Бан пользователя
    waiting_for_ban_user_id = State()
    waiting_for_ban_reason = State()
    
    # Назначение админа
    waiting_for_admin_user_id = State()

# ================= МИДЛВАРЬ ДЛЯ ТЕХРАБОТ =================

class MaintenanceMiddleware:
    """Мидлварь для проверки режима техработ"""
    
    async def on_process_message(self, message: types.Message, data: dict):
        if config.maintenance_mode:
            user_id = message.from_user.id
            if user_id not in ADMIN_IDS:
                user = db.get_user(user_id)
                if not user or not user.get('is_admin', 0):
                    await message.reply(config.maintenance_message)
                    raise CancelHandler()
    
    async def on_process_callback_query(self, callback: types.CallbackQuery, data: dict):
        if config.maintenance_mode:
            user_id = callback.from_user.id
            if user_id not in ADMIN_IDS:
                user = db.get_user(user_id)
                if not user or not user.get('is_admin', 0):
                    await callback.answer(config.maintenance_message, show_alert=True)
                    raise CancelHandler()

# ================= КЛАВИАТУРЫ =================

def get_main_keyboard(user_id: int = None):
    """Главная клавиатура"""
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("📱 Номера", callback_data="numbers_page_1"),
        InlineKeyboardButton("👤 Профиль", callback_data="profile"),
        InlineKeyboardButton("📖 Инструкция", callback_data="instruction"),
    )
    
    if user_id:
        user = db.get_user(user_id)
        if user and (user.get('is_admin', 0) or user_id in ADMIN_IDS):
            keyboard.add(InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin"))
    
    return keyboard

def get_numbers_keyboard(page: int, total_pages: int):
    """Клавиатура для списка номеров с пагинацией"""
    keyboard = InlineKeyboardMarkup(row_width=3)
    
    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton("◀️", callback_data=f"numbers_page_{page-1}"))
    
    nav_buttons.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="current_page"))
    
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton("▶️", callback_data=f"numbers_page_{page+1}"))
    
    keyboard.row(*nav_buttons)
    keyboard.add(InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu"))
    
    return keyboard

def get_topup_keyboard():
    """Клавиатура для пополнения баланса"""
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("💳 ЮMoney", callback_data="topup_yoomoney"),
        InlineKeyboardButton("₿ Crypto Bot", callback_data="topup_cryptobot"),
        InlineKeyboardButton("◀️ Назад в профиль", callback_data="profile")
    )
    return keyboard

def get_amount_keyboard():
    """Клавиатура для выбора суммы пополнения"""
    keyboard = InlineKeyboardMarkup(row_width=3)
    keyboard.add(
        InlineKeyboardButton("100 ₽", callback_data="amount_100"),
        InlineKeyboardButton("300 ₽", callback_data="amount_300"),
        InlineKeyboardButton("500 ₽", callback_data="amount_500"),
        InlineKeyboardButton("1000 ₽", callback_data="amount_1000"),
        InlineKeyboardButton("2000 ₽", callback_data="amount_2000"),
        InlineKeyboardButton("5000 ₽", callback_data="amount_5000"),
        InlineKeyboardButton("✏️ Другая сумма", callback_data="amount_custom"),
        InlineKeyboardButton("◀️ Назад", callback_data="profile")
    )
    return keyboard

def get_admin_keyboard():
    """Клавиатура админ-панели (2 столбца)"""
    keyboard = InlineKeyboardMarkup(row_width=2)
    
    # Первый ряд
    keyboard.add(
        InlineKeyboardButton("➕ Добавить номер", callback_data="admin_add_number"),
        InlineKeyboardButton("📋 Все номера", callback_data="admin_all_numbers_page_1")
    )
    
    # Второй ряд
    keyboard.add(
        InlineKeyboardButton("👥 Пользователи", callback_data="admin_users_page_1"),
        InlineKeyboardButton("📊 Статистика", callback_data="admin_stats")
    )
    
    # Третий ряд
    keyboard.add(
        InlineKeyboardButton("💰 Транзакции", callback_data="admin_transactions"),
        InlineKeyboardButton("✏️ Редактировать инфо", callback_data="admin_edit_info")
    )
    
    # Четвёртый ряд
    keyboard.add(
        InlineKeyboardButton("🎁 Выдать звёзды", callback_data="admin_add_stars"),
        InlineKeyboardButton("⚙️ Настройки", callback_data="admin_settings")
    )
    
    # Пятый ряд - функции модерации
    keyboard.add(
        InlineKeyboardButton("🔨 Забанить", callback_data="admin_ban_user"),
        InlineKeyboardButton("👑 Назначить админа", callback_data="admin_set_admin")
    )
    
    # Шестой ряд - технические работы (выделено)
    maintenance_status = "✅ Работает" if not config.maintenance_mode else "🔧 Включены"
    maintenance_emoji = "🔧" if not config.maintenance_mode else "✅"
    keyboard.add(
        InlineKeyboardButton(f"🔧 Техработы: {maintenance_status}", callback_data="admin_toggle_maintenance"),
        InlineKeyboardButton("💾 Создать бекап", callback_data="admin_create_backup")
    )
    
    # Седьмой ряд - выход
    keyboard.add(InlineKeyboardButton("◀️ Назад", callback_data="main_menu"))
    
    return keyboard

def get_number_manage_keyboard(number_id: int):
    """Клавиатура для управления конкретным номером"""
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("✏️ Редактировать", callback_data=f"admin_edit_number_{number_id}"),
        InlineKeyboardButton("❌ Удалить", callback_data=f"admin_delete_number_{number_id}"),
        InlineKeyboardButton("◀️ Назад", callback_data="admin_manage_numbers")
    )
    return keyboard

def get_edit_fields_keyboard(number_id: int):
    """Клавиатура для выбора поля редактирования"""
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("📞 Номер", callback_data=f"edit_field_{number_id}_phone"),
        InlineKeyboardButton("🌍 Страна", callback_data=f"edit_field_{number_id}_country"),
        InlineKeyboardButton("📝 Описание", callback_data=f"edit_field_{number_id}_description"),
        InlineKeyboardButton("💰 Цена", callback_data=f"edit_field_{number_id}_price"),
        InlineKeyboardButton("🖼 Фото", callback_data=f"edit_field_{number_id}_photo"),
        InlineKeyboardButton("◀️ Назад", callback_data=f"admin_view_number_{number_id}")
    )
    return keyboard

def get_users_keyboard(page: int, total_pages: int):
    """Клавиатура для списка пользователей с пагинацией"""
    keyboard = InlineKeyboardMarkup(row_width=3)
    
    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton("◀️", callback_data=f"admin_users_page_{page-1}"))
    
    nav_buttons.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="current_page"))
    
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton("▶️", callback_data=f"admin_users_page_{page+1}"))
    
    keyboard.row(*nav_buttons)
    keyboard.add(InlineKeyboardButton("◀️ Назад", callback_data="admin"))
    
    return keyboard

def get_numbers_list_keyboard(page: int, total_pages: int):
    """Клавиатура для списка всех номеров с пагинацией"""
    keyboard = InlineKeyboardMarkup(row_width=3)
    
    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton("◀️", callback_data=f"admin_all_numbers_page_{page-1}"))
    
    nav_buttons.append(InlineKeyboardButton(f"{page}/{total_pages}", callback_data="current_page"))
    
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton("▶️", callback_data=f"admin_all_numbers_page_{page+1}"))
    
    keyboard.row(*nav_buttons)
    keyboard.add(
        InlineKeyboardButton("➕ Добавить", callback_data="admin_add_number"),
        InlineKeyboardButton("◀️ Назад", callback_data="admin")
    )
    
    return keyboard

# ================= ОБРАБОТЧИКИ КОМАНД =================

@dp.message_handler(commands=['start'])
async def cmd_start(message: types.Message):
    """Обработчик команды /start"""
    user_id = message.from_user.id
    username = message.from_user.username or f"user_{user_id}"
    
    # Проверяем техработы
    if config.maintenance_mode and user_id not in ADMIN_IDS:
        user = db.get_user(user_id)
        if not user or not user.get('is_admin', 0):
            await message.reply(config.maintenance_message)
            return
    
    user = db.get_user(user_id)
    
    if not user:
        db.create_user(
            user_id=user_id,
            username=username,
            first_name=message.from_user.first_name or "",
            last_name=message.from_user.last_name or ""
        )
        
        bot_info = config.get('bot_info')
        bot_photo = config.get('bot_photo')
        
        if bot_photo:
            await bot.send_photo(
                chat_id=user_id,
                photo=bot_photo,
                caption=bot_info,
                reply_markup=get_main_keyboard(user_id)
            )
        else:
            await message.reply(
                bot_info,
                reply_markup=get_main_keyboard(user_id)
            )
    else:
        if user.get('banned', 0):
            await message.reply(f"⛔ Вы забанены. Причина: {user.get('ban_reason', 'Не указана')}")
            return
        
        db.update_user_activity(user_id)
        
        await message.reply(
            "👋 С возвращением!",
            reply_markup=get_main_keyboard(user_id)
        )

# ================= МИДЛВАРЬ ДЛЯ ТЕХРАБОТ =================

class CancelHandler(Exception):
    pass

@dp.middleware
class MaintenanceMiddleware:
    async def on_process_message(self, message: types.Message, data: dict):
        if config.maintenance_mode:
            user_id = message.from_user.id
            if user_id not in ADMIN_IDS:
                user = db.get_user(user_id)
                if not user or not user.get('is_admin', 0):
                    await message.reply(config.maintenance_message)
                    raise CancelHandler()
    
    async def on_process_callback_query(self, callback: types.CallbackQuery, data: dict):
        if config.maintenance_mode:
            user_id = callback.from_user.id
            if user_id not in ADMIN_IDS:
                user = db.get_user(user_id)
                if not user or not user.get('is_admin', 0):
                    await callback.answer(config.maintenance_message, show_alert=True)
                    raise CancelHandler()

# ================= ОСНОВНЫЕ ОБРАБОТЧИКИ =================

@dp.callback_query_handler(lambda c: c.data == 'main_menu')
async def main_menu(callback: types.CallbackQuery):
    """Возврат в главное меню"""
    await callback.answer()
    
    user_id = callback.from_user.id
    db.update_user_activity(user_id)
    
    await callback.message.edit_text(
        "🏠 <b>Главное меню</b>\n\nВыберите раздел:",
        reply_markup=get_main_keyboard(user_id)
    )

@dp.callback_query_handler(lambda c: c.data == 'profile')
async def show_profile(callback: types.CallbackQuery):
    """Показать профиль пользователя"""
    await callback.answer()
    
    user_id = callback.from_user.id
    user = db.get_user(user_id)
    
    if not user:
        await callback.message.edit_text(
            "❌ Ошибка загрузки профиля",
            reply_markup=get_main_keyboard(user_id)
        )
        return
    
    username = callback.from_user.username or "нет"
    first_name = callback.from_user.first_name or ""
    
    profile_text = f"""
👤 <b>Ваш профиль</b>

🆔 ID: <code>{user_id}</code>
📝 Username: @{username}
👤 Имя: {first_name}

💰 <b>Баланс:</b>
⭐️ Звёзды: {user['stars_balance']}
💵 Рубли: {user['stars_balance'] * STAR_TO_RUB:.2f}₽

📊 <b>Статистика:</b>
📅 Зарегистрирован: {datetime.fromtimestamp(user['registered_at']).strftime('%d.%m.%Y')}
⏱ Последняя активность: {datetime.fromtimestamp(user['last_activity']).strftime('%d.%m.%Y %H:%M')}
"""
    
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("⭐️ Пополнить баланс", callback_data="topup"),
        InlineKeyboardButton("📊 История операций", callback_data="transactions"),
        InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")
    )
    
    await callback.message.edit_text(
        profile_text,
        reply_markup=keyboard
    )

@dp.callback_query_handler(lambda c: c.data == 'topup')
async def topup_menu(callback: types.CallbackQuery):
    """Меню пополнения баланса"""
    await callback.answer()
    
    text = f"""
⭐️ <b>Пополнение баланса</b>

Выберите способ пополнения:

💳 <b>ЮMoney</b> - оплата картами РФ, ЮMoney кошельком
₿ <b>Crypto Bot</b> - оплата криптовалютой (USDT, BTC, ETH)

Минимальная сумма: 100 ₽
Курс: 1 ⭐️ = {STAR_TO_RUB:.2f} ₽
"""
    
    await callback.message.edit_text(
        text,
        reply_markup=get_topup_keyboard()
    )

@dp.callback_query_handler(lambda c: c.data == 'transactions')
async def show_transactions(callback: types.CallbackQuery):
    """История транзакций пользователя"""
    await callback.answer()
    
    user_id = callback.from_user.id
    
    with db.get_cursor() as cursor:
        cursor.execute('''
            SELECT * FROM transactions 
            WHERE user_id = ? 
            ORDER BY created_at DESC 
            LIMIT 20
        ''', (user_id,))
        transactions = [dict(row) for row in cursor.fetchall()]
    
    if not transactions:
        await callback.message.edit_text(
            "📊 <b>История операций пуста</b>",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("◀️ Назад", callback_data="profile")
            )
        )
        return
    
    text = "📊 <b>Последние операции:</b>\n\n"
    
    for t in transactions:
        date = datetime.fromtimestamp(t['created_at']).strftime('%d.%m %H:%M')
        sign = "➕" if t['type'] == 'credit' else "➖"
        amount = t['amount_stars']
        rub = f" ({t['amount_rub']} ₽)" if t['amount_rub'] else ""
        
        text += f"{sign} {date} | {amount} ⭐️{rub}\n"
        text += f"   {t['description']}\n\n"
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("◀️ Назад", callback_data="profile")
        )
    )

@dp.callback_query_handler(lambda c: c.data == 'instruction')
async def show_instruction(callback: types.CallbackQuery):
    """Показать инструкцию"""
    await callback.answer()
    
    instruction = config.get('instruction')
    
    await callback.message.edit_text(
        instruction,
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")
        )
    )

# ================= РАЗДЕЛ НОМЕРОВ =================

@dp.callback_query_handler(lambda c: c.data.startswith('numbers_page_'))
async def show_numbers(callback: types.CallbackQuery):
    """Показать список номеров с пагинацией"""
    await callback.answer()
    
    try:
        page = int(callback.data.split('_')[2])
    except:
        page = 1
    
    numbers, total = db.get_available_numbers(page=page, limit=5)
    total_pages = (total + 4) // 5
    
    if not numbers:
        await callback.message.edit_text(
            "📱 <b>Номера временно отсутствуют</b>\n\n"
            "Загляните позже или напишите администратору.",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")
            )
        )
        return
    
    text = f"📱 <b>Доступные номера</b> (стр. {page}/{total_pages})\n\n"
    
    for num in numbers:
        flag = "🇷🇺" if num['country'] == 'Россия' else "🌍"
        text += f"{flag} <b>{num['country']}</b>\n"
        text += f"📞 <code>{num['phone_number']}</code>\n"
        text += f"📝 {num['description']}\n"
        text += f"💰 <b>{num['price_stars']} ⭐️</b> (~{num['price_rub']:.0f}₽)\n"
        text += f"🔹 Купить: /buy_{num['id']}\n\n"
    
    keyboard = get_numbers_keyboard(page, total_pages)
    
    await callback.message.edit_text(
        text,
        reply_markup=keyboard
    )

@dp.message_handler(lambda message: message.text and message.text.startswith('/buy_'))
async def buy_number_command(message: types.Message, state: FSMContext):
    """Обработка команды покупки"""
    try:
        number_id = int(message.text.split('_')[1])
    except:
        await message.reply("❌ Неверный формат команды")
        return
    
    user_id = message.from_user.id
    user = db.get_user(user_id)
    
    if not user:
        await message.reply("❌ Сначала запустите бота командой /start")
        return
    
    if user.get('banned', 0):
        await message.reply(f"⛔ Вы забанены. Причина: {user.get('ban_reason', 'Не указана')}")
        return
    
    number = db.get_number(number_id)
    
    if not number or number['status'] != 'available':
        await message.reply("❌ Номер уже недоступен")
        return
    
    if user['stars_balance'] < number['price_stars']:
        await message.reply(
            f"❌ Недостаточно звёзд!\n\n"
            f"💰 У вас: {user['stars_balance']} ⭐️\n"
            f"💎 Нужно: {number['price_stars']} ⭐️\n\n"
            f"Пополните баланс в разделе Профиль"
        )
        return
    
    text = f"""
✅ <b>Подтверждение покупки</b>

<b>Номер:</b> <code>{number['phone_number']}</code>
<b>Страна:</b> {number['country']}
<b>Цена:</b> {number['price_stars']} ⭐️
<b>Описание:</b> {number['description']}

После покупки вам нужно будет ввести ваш Telegram username, на который придёт код подтверждения.

<b>Подтвердите покупку:</b>
"""
    
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("✅ Купить", callback_data=f"confirm_buy_{number_id}"),
        InlineKeyboardButton("❌ Отмена", callback_data=f"numbers_page_1")
    )
    
    await message.reply(text, reply_markup=keyboard)

@dp.callback_query_handler(lambda c: c.data.startswith('confirm_buy_'))
async def confirm_buy(callback: types.CallbackQuery, state: FSMContext):
    """Подтверждение покупки"""
    await callback.answer()
    
    number_id = int(callback.data.split('_')[2])
    user_id = callback.from_user.id
    
    number = db.get_number(number_id)
    
    if not number or number['status'] != 'available':
        await callback.message.edit_text(
            "❌ Номер уже недоступен",
            reply_markup=get_main_keyboard(user_id)
        )
        return
    
    await state.update_data(
        number_id=number_id,
        phone=number['phone_number'],
        price=number['price_stars']
    )
    
    await callback.message.edit_text(
        f"📝 <b>Введите ваш Telegram username</b>\n\n"
        f"На этот аккаунт придёт код подтверждения после покупки.\n\n"
        f"Пример: <code>@username</code> или просто <code>username</code>",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("❌ Отмена", callback_data=f"numbers_page_1")
        )
    )
    
    await BuyStates.waiting_for_username.set()

@dp.message_handler(state=BuyStates.waiting_for_username)
async def process_username(message: types.Message, state: FSMContext):
    """Обработка введённого username"""
    username = message.text.strip().replace('@', '')
    
    if len(username) < 3:
        await message.reply("❌ Слишком короткий username")
        return
    
    data = await state.get_data()
    number_id = data['number_id']
    user_id = message.from_user.id
    
    user = db.get_user(user_id)
    number = db.get_number(number_id)
    
    if not number or number['status'] != 'available':
        await message.reply("❌ Номер уже недоступен")
        await state.finish()
        return
    
    if user['stars_balance'] < data['price']:
        await message.reply("❌ Недостаточно звёзд")
        await state.finish()
        return
    
    purchase = db.purchase_number(number_id, user_id)
    
    if not purchase:
        await message.reply("❌ Ошибка при покупке")
        await state.finish()
        return
    
    fake_code = ''.join(random.choices(string.digits, k=5))
    db.confirm_purchase(number_id, fake_code)
    
    await message.reply(
        f"✅ <b>Покупка успешна!</b>\n\n"
        f"📱 <b>Номер:</b> <code>{purchase['phone_number']}</code>\n"
        f"🔑 <b>Код подтверждения:</b> <code>{fake_code}</code>\n\n"
        f"📝 <b>Инструкция:</b>\n"
        f"1. Откройте Telegram\n"
        f"2. Введите номер {purchase['phone_number']}\n"
        f"3. Введите код {fake_code}\n"
        f"4. Готово!\n\n"
        f"Код действителен 24 часа.",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("📱 Номера", callback_data="numbers_page_1"),
            InlineKeyboardButton("👤 Профиль", callback_data="profile")
        )
    )
    
    await state.finish()

# ================= ПЛАТЁЖНЫЕ ОБРАБОТЧИКИ =================

@dp.callback_query_handler(lambda c: c.data == 'topup_yoomoney' or c.data == 'topup_cryptobot')
async def topup_select_method(callback: types.CallbackQuery, state: FSMContext):
    """Выбор метода оплаты"""
    await callback.answer()
    
    method = callback.data.replace('topup_', '')
    await state.update_data(payment_method=method)
    
    await callback.message.edit_text(
        f"💰 <b>Введите сумму пополнения в рублях</b>\n\n"
        f"Метод оплаты: {'💳 ЮMoney' if method == 'yoomoney' else '₿ Crypto Bot'}\n"
        f"Минимальная сумма: 100 ₽\n\n"
        f"Или выберите из предложенных вариантов:",
        reply_markup=get_amount_keyboard()
    )
    
    await TopUpStates.waiting_for_amount.set()

@dp.callback_query_handler(lambda c: c.data.startswith('amount_'), state=TopUpStates.waiting_for_amount)
async def topup_select_amount(callback: types.CallbackQuery, state: FSMContext):
    """Выбор суммы из предложенных"""
    await callback.answer()
    
    amount_str = callback.data.replace('amount_', '')
    
    if amount_str == 'custom':
        await callback.message.edit_text(
            "✏️ <b>Введите сумму в рублях</b>\n\n"
            "Минимальная сумма: 100 ₽\n"
            "Максимальная сумма: 100000 ₽",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("◀️ Назад", callback_data="topup")
            )
        )
        return
    
    amount = int(amount_str)
    await state.update_data(amount=amount)
    await process_payment(callback.message, state)

@dp.message_handler(state=TopUpStates.waiting_for_amount)
async def topup_custom_amount(message: types.Message, state: FSMContext):
    """Обработка пользовательской суммы"""
    try:
        amount = float(message.text.strip())
        if amount < 100 or amount > 100000:
            await message.reply("❌ Сумма должна быть от 100 до 100000 рублей")
            return
    except ValueError:
        await message.reply("❌ Введите число")
        return
    
    await state.update_data(amount=amount)
    await process_payment(message, state)

async def process_payment(message: types.Message, state: FSMContext):
    """Создание платежа"""
    data = await state.get_data()
    amount = data['amount']
    method = data['payment_method']
    user_id = message.from_user.id if isinstance(message, types.Message) else message.from_user.id
    
    payment = db.create_payment(user_id, amount, method)
    
    payment_url = None
    
    if method == 'yoomoney':
        payment_url = await YooMoneyPayment.create_payment(
            amount=amount,
            payment_id=payment['id'],
            description=f"Пополнение баланса пользователя {user_id}"
        )
    elif method == 'cryptobot':
        payment_url = await CryptoBotPayment.create_payment(
            amount=amount,
            payment_id=payment['id'],
            user_id=user_id
        )
    
    if payment_url:
        db.update_payment_status(payment['id'], 'pending', payment_url)
        
        stars_amount = payment['stars_amount']
        
        text = f"""
✅ <b>Счёт сформирован</b>

💰 Сумма: {amount} ₽
⭐️ Вы получите: {stars_amount} звёзд
💳 Способ оплаты: {'ЮMoney' if method == 'yoomoney' else 'Crypto Bot'}

Нажмите кнопку ниже для оплаты.
После оплаты звёзды будут зачислены автоматически.
"""
        
        keyboard = InlineKeyboardMarkup(row_width=1)
        keyboard.add(
            InlineKeyboardButton("💳 Оплатить", url=payment_url),
            InlineKeyboardButton("✅ Я оплатил", callback_data=f"check_payment_{payment['id']}"),
            InlineKeyboardButton("◀️ Назад", callback_data="profile")
        )
        
        if isinstance(message, types.Message):
            await message.reply(text, reply_markup=keyboard)
        else:
            await message.edit_text(text, reply_markup=keyboard)
    else:
        error_text = f"""
❌ <b>Ошибка создания платежа</b>

Попробуйте позже или выберите другой способ оплаты.
"""
        keyboard = InlineKeyboardMarkup().add(
            InlineKeyboardButton("◀️ Назад", callback_data="topup")
        )
        
        if isinstance(message, types.Message):
            await message.reply(error_text, reply_markup=keyboard)
        else:
            await message.edit_text(error_text, reply_markup=keyboard)
    
    await state.finish()

@dp.callback_query_handler(lambda c: c.data.startswith('check_payment_'))
async def check_payment(callback: types.CallbackQuery):
    """Проверка статуса платежа"""
    await callback.answer()
    
    payment_id = callback.data.replace('check_payment_', '')
    payment = db.get_payment(payment_id)
    
    if not payment:
        await callback.message.edit_text(
            "❌ Платёж не найден",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("◀️ Назад", callback_data="profile")
            )
        )
        return
    
    if payment['status'] == 'completed':
        await callback.message.edit_text(
            "✅ <b>Платёж уже обработан!</b>\n\n"
            f"Звёзды зачислены на ваш баланс.",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("👤 Профиль", callback_data="profile")
            )
        )
        return
    
    if payment['payment_system'] == 'cryptobot':
        is_paid = await CryptoBotPayment.verify_payment(payment_id)
        if is_paid:
            completed_payment = db.complete_payment(payment_id)
            if completed_payment:
                user = db.get_user(completed_payment['user_id'])
                await callback.message.edit_text(
                    f"✅ <b>Оплата успешна!</b>\n\n"
                    f"💰 Зачислено: {completed_payment['stars_amount']} ⭐️\n"
                    f"💎 Новый баланс: {user['stars_balance']} ⭐️",
                    reply_markup=InlineKeyboardMarkup().add(
                        InlineKeyboardButton("👤 Профиль", callback_data="profile"),
                        InlineKeyboardButton("📱 Номера", callback_data="numbers_page_1")
                    )
                )
                return
    
    await callback.message.edit_text(
        "⏳ <b>Платёж ещё не обработан</b>\n\n"
        "Если вы уже оплатили, подождите несколько минут.\n"
        "Средства зачисляются автоматически после подтверждения платёжной системой.",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("🔄 Проверить ещё раз", callback_data=f"check_payment_{payment_id}"),
            InlineKeyboardButton("◀️ Назад", callback_data="profile")
        )
    )

# ================= АДМИН-ПАНЕЛЬ =================

@dp.callback_query_handler(lambda c: c.data == 'admin')
async def admin_panel(callback: types.CallbackQuery):
    """Открыть админ-панель"""
    await callback.answer()
    
    user_id = callback.from_user.id
    
    user = db.get_user(user_id)
    
    if not user or not (user.get('is_admin', 0) or user_id in ADMIN_IDS):
        await callback.message.edit_text(
            "⛔ У вас нет доступа к админ-панели",
            reply_markup=get_main_keyboard(user_id)
        )
        return
    
    # Делаем пользователя админом в БД если его там нет
    if not user.get('is_admin', 0):
        db.set_admin(user_id, True)
    
    stats = db.get_stats()
    maintenance_status = "🔧 ВКЛЮЧЕН" if config.maintenance_mode else "✅ ВЫКЛЮЧЕН"
    
    text = f"""
⚙️ <b>Админ-панель</b>

👤 Администратор: @{callback.from_user.username or 'admin'}
📊 Всего пользователей: {stats['total_users']}
📱 Доступно номеров: {stats['available_numbers']}
💰 Выручка: {stats['total_revenue_stars']} ⭐️ ({stats['total_revenue_rub']:.2f}₽)

🔧 Техработы: {maintenance_status}

Выберите действие (2 столбца):
"""
    
    await callback.message.edit_text(
        text,
        reply_markup=get_admin_keyboard()
    )

# === Управление номерами ===

@dp.callback_query_handler(lambda c: c.data == 'admin_add_number')
async def admin_add_number_start(callback: types.CallbackQuery, state: FSMContext):
    """Начало добавления номера"""
    await callback.answer()
    
    await callback.message.edit_text(
        "📞 <b>Добавление номера</b>\n\n"
        "Введите номер телефона в формате:\n"
        "<code>+79001234567</code>",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("❌ Отмена", callback_data="admin")
        )
    )
    
    await AdminStates.waiting_for_phone.set()

@dp.message_handler(state=AdminStates.waiting_for_phone)
async def admin_add_number_phone(message: types.Message, state: FSMContext):
    """Обработка введённого номера"""
    phone = message.text.strip()
    
    if not phone.startswith('+') or len(phone) < 10:
        await message.reply("❌ Неверный формат. Используйте +79001234567")
        return
    
    await state.update_data(phone=phone)
    
    await message.reply(
        "🌍 <b>Введите страну</b>\n\n"
        "Например: Россия, Украина, Казахстан",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("❌ Отмена", callback_data="admin")
        )
    )
    
    await AdminStates.waiting_for_country.set()

@dp.message_handler(state=AdminStates.waiting_for_country)
async def admin_add_number_country(message: types.Message, state: FSMContext):
    """Обработка введённой страны"""
    country = message.text.strip()
    
    await state.update_data(country=country)
    
    await message.reply(
        "📝 <b>Введите описание номера</b>\n\n"
        "Краткое описание для покупателей",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("❌ Отмена", callback_data="admin")
        )
    )
    
    await AdminStates.waiting_for_description.set()

@dp.message_handler(state=AdminStates.waiting_for_description)
async def admin_add_number_description(message: types.Message, state: FSMContext):
    """Обработка описания"""
    description = message.text.strip()
    
    await state.update_data(description=description)
    
    await message.reply(
        "💰 <b>Введите цену в звёздах</b>\n\n"
        "Только число (целое)",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("❌ Отмена", callback_data="admin")
        )
    )
    
    await AdminStates.waiting_for_price.set()

@dp.message_handler(state=AdminStates.waiting_for_price)
async def admin_add_number_price(message: types.Message, state: FSMContext):
    """Обработка цены"""
    try:
        price = int(message.text.strip())
        if price <= 0:
            raise ValueError
    except:
        await message.reply("❌ Введите положительное целое число")
        return
    
    await state.update_data(price=price)
    
    await message.reply(
        "🖼 <b>Отправьте фото номера</b>\n\n"
        "Или нажмите 'Пропустить'",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("⏭ Пропустить", callback_data="admin_skip_photo"),
            InlineKeyboardButton("❌ Отмена", callback_data="admin")
        )
    )
    
    await AdminStates.waiting_for_photo.set()

@dp.callback_query_handler(lambda c: c.data == 'admin_skip_photo', state=AdminStates.waiting_for_photo)
async def admin_skip_photo(callback: types.CallbackQuery, state: FSMContext):
    """Пропустить добавление фото"""
    await callback.answer()
    
    data = await state.get_data()
    
    success = db.add_number(
        phone=data['phone'],
        country=data['country'],
        description=data['description'],
        price_stars=data['price'],
        photo_id=""
    )
    
    if success:
        await callback.message.edit_text(
            "✅ <b>Номер успешно добавлен!</b>",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin")
            )
        )
    else:
        await callback.message.edit_text(
            "❌ <b>Ошибка при добавлении номера</b>\n\n"
            "Возможно, номер уже существует",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin")
            )
        )
    
    await state.finish()

@dp.message_handler(content_types=ContentType.PHOTO, state=AdminStates.waiting_for_photo)
async def admin_add_number_photo(message: types.Message, state: FSMContext):
    """Обработка фото"""
    photo_id = message.photo[-1].file_id
    
    data = await state.get_data()
    
    success = db.add_number(
        phone=data['phone'],
        country=data['country'],
        description=data['description'],
        price_stars=data['price'],
        photo_id=photo_id
    )
    
    if success:
        await message.reply(
            "✅ <b>Номер успешно добавлен!</b>",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin")
            )
        )
    else:
        await message.reply(
            "❌ <b>Ошибка при добавлении номера</b>\n\n"
            "Возможно, номер уже существует",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin")
            )
        )
    
    await state.finish()

@dp.callback_query_handler(lambda c: c.data.startswith('admin_all_numbers_page_'))
async def admin_all_numbers(callback: types.CallbackQuery):
    """Список всех номеров с пагинацией"""
    await callback.answer()
    
    try:
        page = int(callback.data.split('_')[4])
    except:
        page = 1
    
    numbers, total = db.get_all_numbers(page=page, limit=5)
    total_pages = (total + 4) // 5
    
    if not numbers:
        await callback.message.edit_text(
            "📋 <b>Номера не найдены</b>",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("➕ Добавить", callback_data="admin_add_number"),
                InlineKeyboardButton("◀️ Назад", callback_data="admin")
            )
        )
        return
    
    text = f"📋 <b>Все номера</b> (стр. {page}/{total_pages})\n\n"
    
    for num in numbers:
        status_emoji = "✅" if num['status'] == 'available' else "❌" if num['status'] == 'sold' else "⏳"
        text += f"{status_emoji} <b>{num['phone_number']}</b> ({num['country']})\n"
        text += f"💰 {num['price_stars']} ⭐️ | Статус: {num['status']}\n"
        text += f"🆔 ID: {num['id']}\n\n"
    
    text += "Для управления номером используйте:\n/admin_number_ ID"
    
    await callback.message.edit_text(
        text,
        reply_markup=get_numbers_list_keyboard(page, total_pages)
    )

@dp.message_handler(lambda message: message.text and message.text.startswith('/admin_number_'))
async def admin_view_number(message: types.Message):
    """Просмотр информации о номере"""
    try:
        number_id = int(message.text.split('_')[2])
    except:
        await message.reply("❌ Неверный формат команды")
        return
    
    number = db.get_number(number_id)
    
    if not number:
        await message.reply("❌ Номер не найден")
        return
    
    text = f"""
📱 <b>Информация о номере</b> (ID: {number_id})

📞 <b>Номер:</b> <code>{number['phone_number']}</code>
🌍 <b>Страна:</b> {number['country']}
📝 <b>Описание:</b> {number['description']}
💰 <b>Цена:</b> {number['price_stars']} ⭐️ ({number['price_rub']}₽)
📊 <b>Статус:</b> {number['status']}

👤 <b>Покупатель:</b> {number['buyer_id'] if number['buyer_id'] else 'нет'}
⏱ <b>Куплен:</b> {datetime.fromtimestamp(number['purchased_at']).strftime('%d.%m.%Y %H:%M') if number['purchased_at'] else 'нет'}
"""
    
    keyboard = get_number_manage_keyboard(number_id)
    
    if number['photo_id']:
        await bot.send_photo(
            chat_id=message.chat.id,
            photo=number['photo_id'],
            caption=text,
            reply_markup=keyboard
        )
    else:
        await message.reply(text, reply_markup=keyboard)

@dp.callback_query_handler(lambda c: c.data.startswith('admin_edit_number_'))
async def admin_edit_number(callback: types.CallbackQuery):
    """Редактирование номера"""
    await callback.answer()
    
    number_id = int(callback.data.split('_')[3])
    
    number = db.get_number(number_id)
    
    if not number:
        await callback.message.edit_text(
            "❌ Номер не найден",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("◀️ Назад", callback_data="admin")
            )
        )
        return
    
    text = f"""
✏️ <b>Редактирование номера</b> (ID: {number_id})

📞 Номер: {number['phone_number']}
🌍 Страна: {number['country']}
📝 Описание: {number['description']}
💰 Цена: {number['price_stars']} ⭐️

Выберите поле для редактирования:
"""
    
    await callback.message.edit_text(
        text,
        reply_markup=get_edit_fields_keyboard(number_id)
    )

@dp.callback_query_handler(lambda c: c.data.startswith('edit_field_'))
async def admin_edit_field(callback: types.CallbackQuery, state: FSMContext):
    """Выбор поля для редактирования"""
    await callback.answer()
    
    parts = callback.data.split('_')
    number_id = int(parts[2])
    field = parts[3]
    
    field_names = {
        'phone': 'номер телефона',
        'country': 'страну',
        'description': 'описание',
        'price': 'цену',
        'photo': 'фото'
    }
    
    await state.update_data(
        edit_number_id=number_id,
        edit_field=field
    )
    
    if field == 'photo':
        await callback.message.edit_text(
            f"🖼 <b>Отправьте новое фото</b>",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("❌ Отмена", callback_data=f"admin_edit_number_{number_id}")
            )
        )
        await AdminStates.waiting_for_edit_value.set()
    else:
        await callback.message.edit_text(
            f"✏️ <b>Введите новое значение для {field_names.get(field, 'поля')}</b>",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("❌ Отмена", callback_data=f"admin_edit_number_{number_id}")
            )
        )
        await AdminStates.waiting_for_edit_value.set()

@dp.message_handler(state=AdminStates.waiting_for_edit_value)
async def admin_update_field(message: types.Message, state: FSMContext):
    """Обновление значения поля"""
    data = await state.get_data()
    number_id = data['edit_number_id']
    field = data['edit_field']
    value = message.text.strip()
    
    update_data = {}
    
    if field == 'price':
        try:
            price = int(value)
            if price <= 0:
                raise ValueError
            update_data['price_stars'] = price
            update_data['price_rub'] = price * STAR_TO_RUB
        except:
            await message.reply("❌ Введите положительное целое число")
            return
    elif field == 'phone':
        if not value.startswith('+') or len(value) < 10:
            await message.reply("❌ Неверный формат номера")
            return
        update_data['phone_number'] = value
    elif field == 'country':
        update_data['country'] = value
    elif field == 'description':
        update_data['description'] = value
    else:
        await message.reply("❌ Неизвестное поле")
        await state.finish()
        return
    
    success = db.update_number(number_id, update_data)
    
    if success:
        await message.reply(
            "✅ <b>Номер обновлён!</b>",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("◀️ Назад к номеру", callback_data=f"admin_edit_number_{number_id}")
            )
        )
    else:
        await message.reply(
            "❌ <b>Ошибка при обновлении</b>",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("◀️ Назад", callback_data=f"admin_edit_number_{number_id}")
            )
        )
    
    await state.finish()

@dp.message_handler(content_types=ContentType.PHOTO, state=AdminStates.waiting_for_edit_value)
async def admin_update_photo(message: types.Message, state: FSMContext):
    """Обновление фото"""
    data = await state.get_data()
    number_id = data['edit_number_id']
    photo_id = message.photo[-1].file_id
    
    success = db.update_number(number_id, {'photo_id': photo_id})
    
    if success:
        await message.reply(
            "✅ <b>Фото обновлено!</b>",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("◀️ Назад к номеру", callback_data=f"admin_edit_number_{number_id}")
            )
        )
    else:
        await message.reply(
            "❌ <b>Ошибка при обновлении фото</b>",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("◀️ Назад", callback_data=f"admin_edit_number_{number_id}")
            )
        )
    
    await state.finish()

@dp.callback_query_handler(lambda c: c.data.startswith('admin_delete_number_'))
async def admin_delete_number(callback: types.CallbackQuery):
    """Удаление номера"""
    await callback.answer()
    
    number_id = int(callback.data.split('_')[3])
    
    number = db.get_number(number_id)
    
    if not number:
        await callback.message.edit_text(
            "❌ Номер не найден",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("◀️ Назад", callback_data="admin")
            )
        )
        return
    
    if number['status'] == 'sold':
        await callback.message.edit_text(
            "❌ Нельзя удалить проданный номер",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("◀️ Назад", callback_data=f"admin_edit_number_{number_id}")
            )
        )
        return
    
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("✅ Да, удалить", callback_data=f"confirm_delete_{number_id}"),
        InlineKeyboardButton("❌ Нет", callback_data=f"admin_edit_number_{number_id}")
    )
    
    await callback.message.edit_text(
        f"❓ <b>Вы уверены, что хотите удалить номер?</b>\n\n"
        f"📞 {number['phone_number']}\n"
        f"🌍 {number['country']}\n"
        f"💰 {number['price_stars']} ⭐️",
        reply_markup=keyboard
    )

@dp.callback_query_handler(lambda c: c.data.startswith('confirm_delete_'))
async def confirm_delete(callback: types.CallbackQuery):
    """Подтверждение удаления"""
    await callback.answer()
    
    number_id = int(callback.data.split('_')[2])
    
    success = db.delete_number(number_id)
    
    if success:
        await callback.message.edit_text(
            "✅ <b>Номер удалён</b>",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("◀️ Назад к списку", callback_data="admin_all_numbers_page_1")
            )
        )
    else:
        await callback.message.edit_text(
            "❌ <b>Ошибка при удалении</b>",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("◀️ Назад", callback_data="admin")
            )
        )

# === Управление пользователями ===

@dp.callback_query_handler(lambda c: c.data.startswith('admin_users_page_'))
async def admin_users_list(callback: types.CallbackQuery):
    """Список пользователей с пагинацией"""
    await callback.answer()
    
    try:
        page = int(callback.data.split('_')[3])
    except:
        page = 1
    
    users, total = db.get_all_users(page=page, limit=10)
    total_pages = (total + 9) // 10
    
    if not users:
        await callback.message.edit_text(
            "👥 <b>Пользователи не найдены</b>",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("◀️ Назад", callback_data="admin")
            )
        )
        return
    
    text = f"👥 <b>Список пользователей</b> (стр. {page}/{total_pages})\n\n"
    
    for user in users:
        username = user['username'] or f"user_{user['user_id']}"
        admin_star = "👑" if user['is_admin'] else ""
        banned = "🔨" if user['banned'] else ""
        text += f"<b>{username}</b> {admin_star}{banned} | <code>{user['user_id']}</code>\n"
        text += f"💰 {user['stars_balance']} ⭐️ | 📅 {datetime.fromtimestamp(user['registered_at']).strftime('%d.%m')}\n\n"
    
    await callback.message.edit_text(
        text,
        reply_markup=get_users_keyboard(page, total_pages)
    )

# === Выдача звёзд ===

@dp.callback_query_handler(lambda c: c.data == 'admin_add_stars')
async def admin_add_stars_start(callback: types.CallbackQuery, state: FSMContext):
    """Начало выдачи звёзд"""
    await callback.answer()
    
    await callback.message.edit_text(
        "🎁 <b>Выдача звёзд пользователю</b>\n\n"
        "Введите ID пользователя:",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("◀️ Назад", callback_data="admin")
        )
    )
    
    await AdminStates.waiting_for_user_id.set()

@dp.message_handler(state=AdminStates.waiting_for_user_id)
async def admin_add_stars_user(message: types.Message, state: FSMContext):
    """Обработка ID пользователя"""
    try:
        user_id = int(message.text.strip())
    except:
        await message.reply("❌ Введите числовой ID")
        return
    
    user = db.get_user(user_id)
    
    if not user:
        await message.reply("❌ Пользователь не найден")
        return
    
    await state.update_data(target_user_id=user_id, target_username=user['username'])
    
    await message.reply(
        f"👤 <b>Пользователь:</b> @{user['username']} ({user_id})\n"
        f"💰 <b>Текущий баланс:</b> {user['stars_balance']} ⭐️\n\n"
        f"Введите количество звёзд для выдачи:",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("❌ Отмена", callback_data="admin")
        )
    )
    
    await AdminStates.waiting_for_stars_amount.set()

@dp.message_handler(state=AdminStates.waiting_for_stars_amount)
async def admin_add_stars_amount(message: types.Message, state: FSMContext):
    """Обработка количества звёзд"""
    try:
        amount = int(message.text.strip())
        if amount <= 0:
            raise ValueError
    except:
        await message.reply("❌ Введите положительное целое число")
        return
    
    data = await state.get_data()
    user_id = data['target_user_id']
    username = data['target_username']
    
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("✅ Подтвердить", callback_data="admin_confirm_stars"),
        InlineKeyboardButton("❌ Отмена", callback_data="admin")
    )
    
    await state.update_data(amount=amount)
    
    await message.reply(
        f"🎁 <b>Подтверждение выдачи</b>\n\n"
        f"👤 Пользователь: @{username} ({user_id})\n"
        f"💰 Текущий баланс: {db.get_user(user_id)['stars_balance']} ⭐️\n"
        f"➕ Будет добавлено: {amount} ⭐️\n"
        f"💎 Новый баланс: {db.get_user(user_id)['stars_balance'] + amount} ⭐️\n\n"
        f"Подтвердите операцию:",
        reply_markup=keyboard
    )
    
    await AdminStates.waiting_for_confirm.set()

@dp.callback_query_handler(lambda c: c.data == 'admin_confirm_stars', state=AdminStates.waiting_for_confirm)
async def admin_confirm_stars(callback: types.CallbackQuery, state: FSMContext):
    """Подтверждение выдачи звёзд"""
    await callback.answer()
    
    data = await state.get_data()
    user_id = data['target_user_id']
    amount = data['amount']
    
    success = db.add_stars(user_id, amount, "Выдано администратором")
    
    if success:
        try:
            await bot.send_message(
                user_id,
                f"🎁 <b>Вам начислено {amount} ⭐️!</b>\n\n"
                f"💰 Новый баланс: {db.get_user(user_id)['stars_balance']} ⭐️",
                reply_markup=get_main_keyboard(user_id)
            )
        except:
            pass
        
        await callback.message.edit_text(
            f"✅ <b>Звёзды успешно выданы!</b>\n\n"
            f"👤 Пользователь: @{data['target_username']} ({user_id})\n"
            f"➕ Добавлено: {amount} ⭐️\n"
            f"💰 Новый баланс: {db.get_user(user_id)['stars_balance']} ⭐️",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin")
            )
        )
    else:
        await callback.message.edit_text(
            "❌ <b>Ошибка при выдаче звёзд</b>",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin")
            )
        )
    
    await state.finish()

# === Бан пользователя ===

@dp.callback_query_handler(lambda c: c.data == 'admin_ban_user')
async def admin_ban_user_start(callback: types.CallbackQuery, state: FSMContext):
    """Начало бана пользователя"""
    await callback.answer()
    
    await callback.message.edit_text(
        "🔨 <b>Бан пользователя</b>\n\n"
        "Введите ID пользователя для бана:",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("◀️ Назад", callback_data="admin")
        )
    )
    
    await AdminStates.waiting_for_ban_user_id.set()

@dp.message_handler(state=AdminStates.waiting_for_ban_user_id)
async def admin_ban_user_id(message: types.Message, state: FSMContext):
    """Обработка ID пользователя для бана"""
    try:
        user_id = int(message.text.strip())
    except:
        await message.reply("❌ Введите числовой ID")
        return
    
    if user_id in ADMIN_IDS:
        await message.reply("❌ Нельзя забанить главного администратора")
        return
    
    user = db.get_user(user_id)
    
    if not user:
        await message.reply("❌ Пользователь не найден")
        return
    
    await state.update_data(ban_user_id=user_id)
    
    await message.reply(
        f"👤 <b>Пользователь:</b> @{user['username']} ({user_id})\n\n"
        f"Введите причину бана (или отправьте 'нет'):",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("❌ Отмена", callback_data="admin")
        )
    )
    
    await AdminStates.waiting_for_ban_reason.set()

@dp.message_handler(state=AdminStates.waiting_for_ban_reason)
async def admin_ban_reason(message: types.Message, state: FSMContext):
    """Обработка причины бана"""
    reason = message.text.strip()
    if reason.lower() == 'нет':
        reason = "Нарушение правил"
    
    data = await state.get_data()
    user_id = data['ban_user_id']
    
    success = db.ban_user(user_id, reason)
    
    if success:
        try:
            await bot.send_message(
                user_id,
                f"⛔ <b>Вы забанены</b>\n\nПричина: {reason}"
            )
        except:
            pass
        
        await message.reply(
            f"✅ <b>Пользователь @{db.get_user(user_id)['username']} забанен</b>\n\n"
            f"Причина: {reason}",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin")
            )
        )
    else:
        await message.reply(
            "❌ <b>Ошибка при бане</b>",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin")
            )
        )
    
    await state.finish()

# === Назначение админа ===

@dp.callback_query_handler(lambda c: c.data == 'admin_set_admin')
async def admin_set_admin_start(callback: types.CallbackQuery, state: FSMContext):
    """Начало назначения админа"""
    await callback.answer()
    
    await callback.message.edit_text(
        "👑 <b>Назначение администратора</b>\n\n"
        "Введите ID пользователя:",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("◀️ Назад", callback_data="admin")
        )
    )
    
    await AdminStates.waiting_for_admin_user_id.set()

@dp.message_handler(state=AdminStates.waiting_for_admin_user_id)
async def admin_set_admin_id(message: types.Message, state: FSMContext):
    """Обработка ID для назначения админом"""
    try:
        user_id = int(message.text.strip())
    except:
        await message.reply("❌ Введите числовой ID")
        return
    
    user = db.get_user(user_id)
    
    if not user:
        await message.reply("❌ Пользователь не найден")
        return
    
    new_admin_status = not user.get('is_admin', False)
    action = "назначен админом" if new_admin_status else "снят с админки"
    
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("✅ Подтвердить", callback_data=f"confirm_admin_{user_id}_{int(new_admin_status)}"),
        InlineKeyboardButton("❌ Отмена", callback_data="admin")
    )
    
    await message.reply(
        f"👤 <b>Пользователь:</b> @{user['username']} ({user_id})\n"
        f"👑 <b>Текущий статус:</b> {'Админ' if user['is_admin'] else 'Пользователь'}\n\n"
        f"Подтвердите действие: {action}",
        reply_markup=keyboard
    )
    
    await state.finish()

@dp.callback_query_handler(lambda c: c.data.startswith('confirm_admin_'))
async def confirm_admin(callback: types.CallbackQuery):
    """Подтверждение назначения админа"""
    await callback.answer()
    
    parts = callback.data.split('_')
    user_id = int(parts[2])
    is_admin = bool(int(parts[3]))
    
    success = db.set_admin(user_id, is_admin)
    
    if success:
        status_text = "администратором" if is_admin else "пользователем"
        try:
            await bot.send_message(
                user_id,
                f"👑 <b>Ваш статус изменён</b>\n\n"
                f"Теперь вы {status_text} бота."
            )
        except:
            pass
        
        await callback.message.edit_text(
            f"✅ <b>Статус пользователя изменён</b>\n\n"
            f"Пользователь @{db.get_user(user_id)['username']} теперь {status_text}.",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin")
            )
        )
    else:
        await callback.message.edit_text(
            "❌ <b>Ошибка при изменении статуса</b>",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin")
            )
        )

# === Технические работы ===

@dp.callback_query_handler(lambda c: c.data == 'admin_toggle_maintenance')
async def admin_toggle_maintenance(callback: types.CallbackQuery):
    """Включение/выключение режима техработ"""
    await callback.answer()
    
    current_mode = config.maintenance_mode
    new_mode = not current_mode
    
    if new_mode:
        # Включаем техработы
        keyboard = InlineKeyboardMarkup(row_width=2)
        keyboard.add(
            InlineKeyboardButton("✅ Включить", callback_data="maintenance_confirm_on"),
            InlineKeyboardButton("✏️ Изменить сообщение", callback_data="maintenance_edit_message"),
            InlineKeyboardButton("❌ Отмена", callback_data="admin")
        )
        
        await callback.message.edit_text(
            f"🔧 <b>Включение режима техработ</b>\n\n"
            f"Текущее сообщение:\n{config.maintenance_message}\n\n"
            f"Все пользователи (кроме админов) получат это сообщение и не смогут пользоваться ботом.",
            reply_markup=keyboard
        )
    else:
        # Выключаем техработы
        config.maintenance_mode = False
        
        # Уведомляем всех пользователей о завершении работ
        await notify_all_users(
            "✅ <b>Технические работы завершены</b>\n\n"
            "Бот снова доступен! Приносим извинения за неудобства."
        )
        
        await callback.message.edit_text(
            "✅ <b>Режим техработ выключен</b>\n\n"
            "Все пользователи уведомлены.",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin")
            )
        )

@dp.callback_query_handler(lambda c: c.data == 'maintenance_confirm_on')
async def maintenance_confirm_on(callback: types.CallbackQuery):
    """Подтверждение включения техработ"""
    await callback.answer()
    
    config.maintenance_mode = True
    
    # Уведомляем всех пользователей о начале работ
    await notify_all_users(config.maintenance_message)
    
    await callback.message.edit_text(
        "🔧 <b>Режим техработ включён</b>\n\n"
        f"Сообщение:\n{config.maintenance_message}\n\n"
        f"Все пользователи уведомлены. Только администраторы имеют доступ.",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin")
        )
    )

@dp.callback_query_handler(lambda c: c.data == 'maintenance_edit_message')
async def maintenance_edit_message(callback: types.CallbackQuery, state: FSMContext):
    """Редактирование сообщения о техработах"""
    await callback.answer()
    
    await callback.message.edit_text(
        "✏️ <b>Введите новое сообщение о техработах</b>\n\n"
        "Это сообщение увидят все пользователи при включении режима.",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("◀️ Назад", callback_data="admin_toggle_maintenance")
        )
    )
    
    await AdminStates.waiting_for_maintenance_message.set()

@dp.message_handler(state=AdminStates.waiting_for_maintenance_message)
async def maintenance_save_message(message: types.Message, state: FSMContext):
    """Сохранение нового сообщения о техработах"""
    new_message = message.text.strip()
    
    config.set('maintenance_message', new_message)
    
    await message.reply(
        "✅ <b>Сообщение сохранено</b>\n\n"
        f"Новое сообщение:\n{new_message}",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin")
        )
    )
    
    await state.finish()

async def notify_all_users(text: str):
    """Уведомление всех пользователей"""
    users, total = db.get_all_users(page=1, limit=10000)
    
    notified = 0
    for user in users:
        try:
            await bot.send_message(user['user_id'], text)
            notified += 1
            await asyncio.sleep(0.05)  # Небольшая задержка чтобы не забанили
        except:
            pass
    
    logger.info(f"Уведомление отправлено {notified} пользователям")

# === Бекапы ===

@dp.callback_query_handler(lambda c: c.data == 'admin_create_backup')
async def admin_create_backup(callback: types.CallbackQuery):
    """Создание бекапа базы данных"""
    await callback.answer()
    
    await callback.message.edit_text(
        "💾 <b>Создание бекапа...</b>\n\n"
        "Пожалуйста, подождите."
    )
    
    backup_file = db.create_backup()
    
    if backup_file:
        # Отправляем файл админу
        with open(backup_file, 'rb') as f:
            await callback.message.reply_document(
                f,
                caption="✅ <b>Бекап создан</b>",
                reply_markup=InlineKeyboardMarkup().add(
                    InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin")
                )
            )
    else:
        await callback.message.edit_text(
            "❌ <b>Ошибка создания бекапа</b>",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin")
            )
        )

# === Транзакции ===

@dp.callback_query_handler(lambda c: c.data == 'admin_transactions')
async def admin_transactions(callback: types.CallbackQuery):
    """Просмотр последних транзакций"""
    await callback.answer()
    
    transactions = db.get_transactions(limit=20)
    
    if not transactions:
        await callback.message.edit_text(
            "💰 <b>Транзакции не найдены</b>",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("◀️ Назад", callback_data="admin")
            )
        )
        return
    
    text = "💰 <b>Последние транзакции:</b>\n\n"
    
    for t in transactions:
        date = datetime.fromtimestamp(t['created_at']).strftime('%d.%m %H:%M')
        sign = "➕" if t['type'] == 'credit' else "➖"
        amount = t['amount_stars']
        rub = f" ({t['amount_rub']} ₽)" if t['amount_rub'] else ""
        status = t['status'] if t['status'] else 'completed'
        
        text += f"{sign} {date} | {amount} ⭐️{rub}\n"
        text += f"   👤 ID: {t['user_id']} | {t['description'][:30]}\n"
        text += f"   📊 Статус: {status}\n\n"
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("🔄 Обновить", callback_data="admin_transactions"),
            InlineKeyboardButton("◀️ Назад", callback_data="admin")
        )
    )

# === Статистика ===

@dp.callback_query_handler(lambda c: c.data == 'admin_stats')
async def admin_stats(callback: types.CallbackQuery):
    """Показать статистику"""
    await callback.answer()
    
    stats = db.get_stats()
    
    # Получаем статистику за сегодня
    today = datetime.now().strftime('%Y-%m-%d')
    with db.get_cursor() as cursor:
        cursor.execute('SELECT * FROM stats WHERE date = ?', (today,))
        today_stats = cursor.fetchone()
    
    text = f"""
📊 <b>Общая статистика</b>

<b>👥 Пользователи:</b>
• Всего: {stats['total_users']}
• Админов: {stats['total_admins']}
• Забанено: {stats['total_banned']}
• Активных сегодня: {stats['active_today']}

<b>📱 Номера:</b>
• Доступно: {stats['available_numbers']}
• Продано: {stats['sold_numbers']}
• В обработке: {stats['pending_numbers']}

<b>💰 Финансы:</b>
• Выручка: {stats['total_revenue_stars']} ⭐️
• В рублях: {stats['total_revenue_rub']:.2f}₽
• Транзакций: {stats['total_transactions']}

<b>📅 Сегодня ({today}):</b>
• Новых: {today_stats['new_users'] if today_stats else 0}
• Продаж: {today_stats['purchases'] if today_stats else 0}
• Выручка: {today_stats['revenue_stars'] if today_stats else 0} ⭐️
"""
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("🔄 Обновить", callback_data="admin_stats"),
            InlineKeyboardButton("◀️ Назад", callback_data="admin")
        )
    )

# === Редактирование информации бота ===

@dp.callback_query_handler(lambda c: c.data == 'admin_edit_info')
async def admin_edit_info(callback: types.CallbackQuery):
    """Меню редактирования информации"""
    await callback.answer()
    
    current_info = config.get('bot_info')
    
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("✏️ Текст приветствия", callback_data="admin_edit_bot_info"),
        InlineKeyboardButton("🖼 Фото бота", callback_data="admin_edit_bot_photo"),
        InlineKeyboardButton("📖 Инструкцию", callback_data="admin_edit_instruction"),
        InlineKeyboardButton("◀️ Назад", callback_data="admin")
    )
    
    await callback.message.edit_text(
        f"✏️ <b>Редактирование информации</b>\n\n"
        f"<b>Текущий текст приветствия:</b>\n{current_info[:200]}...",
        reply_markup=keyboard
    )

@dp.callback_query_handler(lambda c: c.data == 'admin_edit_bot_info')
async def admin_edit_bot_info(callback: types.CallbackQuery, state: FSMContext):
    """Редактирование текста приветствия"""
    await callback.answer()
    
    await callback.message.edit_text(
        "✏️ <b>Введите новый текст приветствия</b>\n\n"
        "Можно использовать HTML-теги: <b>, <i>, <code>",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("◀️ Назад", callback_data="admin_edit_info")
        )
    )
    
    await AdminStates.waiting_for_new_info.set()

@dp.message_handler(state=AdminStates.waiting_for_new_info)
async def admin_update_bot_info(message: types.Message, state: FSMContext):
    """Обновление текста приветствия"""
    new_info = message.text.strip()
    
    config.set('bot_info', new_info)
    
    await message.reply(
        "✅ <b>Текст приветствия обновлён!</b>",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin")
        )
    )
    
    await state.finish()

@dp.callback_query_handler(lambda c: c.data == 'admin_edit_bot_photo')
async def admin_edit_bot_photo(callback: types.CallbackQuery, state: FSMContext):
    """Редактирование фото бота"""
    await callback.answer()
    
    await callback.message.edit_text(
        "🖼 <b>Отправьте новое фото для бота</b>\n\n"
        "Или нажмите 'Удалить' чтобы убрать текущее",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("🗑 Удалить фото", callback_data="admin_delete_bot_photo"),
            InlineKeyboardButton("◀️ Назад", callback_data="admin_edit_info")
        )
    )
    
    await AdminStates.waiting_for_new_photo.set()

@dp.callback_query_handler(lambda c: c.data == 'admin_delete_bot_photo', state=AdminStates.waiting_for_new_photo)
async def admin_delete_bot_photo(callback: types.CallbackQuery, state: FSMContext):
    """Удаление фото бота"""
    await callback.answer()
    
    config.set('bot_photo', '')
    
    await callback.message.edit_text(
        "✅ <b>Фото бота удалено</b>",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin")
        )
    )
    
    await state.finish()

@dp.message_handler(content_types=ContentType.PHOTO, state=AdminStates.waiting_for_new_photo)
async def admin_update_bot_photo(message: types.Message, state: FSMContext):
    """Обновление фото бота"""
    photo_id = message.photo[-1].file_id
    
    config.set('bot_photo', photo_id)
    
    await message.reply(
        "✅ <b>Фото бота обновлено!</b>",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin")
        )
    )
    
    await state.finish()

@dp.callback_query_handler(lambda c: c.data == 'admin_edit_instruction')
async def admin_edit_instruction(callback: types.CallbackQuery, state: FSMContext):
    """Редактирование инструкции"""
    await callback.answer()
    
    await callback.message.edit_text(
        "✏️ <b>Введите новый текст инструкции</b>",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("◀️ Назад", callback_data="admin_edit_info")
        )
    )
    
    await AdminStates.waiting_for_new_instruction.set()

@dp.message_handler(state=AdminStates.waiting_for_new_instruction)
async def admin_update_instruction(message: types.Message, state: FSMContext):
    """Обновление инструкции"""
    new_instruction = message.text.strip()
    
    config.set('instruction', new_instruction)
    
    await message.reply(
        "✅ <b>Инструкция обновлена!</b>",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin")
        )
    )
    
    await state.finish()

# === Настройки ===

@dp.callback_query_handler(lambda c: c.data == 'admin_settings')
async def admin_settings(callback: types.CallbackQuery):
    """Настройки админки"""
    await callback.answer()
    
    last_backup = config.get('last_backup', 0)
    last_backup_str = datetime.fromtimestamp(last_backup).strftime('%d.%m.%Y %H:%M') if last_backup else 'Никогда'
    
    text = f"""
⚙️ <b>Настройки бота</b>

<b>Курс валют:</b>
1 ⭐️ = {STAR_TO_RUB} ₽

<b>База данных:</b>
• Файл: {DATABASE_FILE}
• Бекапы: {DATABASE_BACKUP_DIR}
• Последний бекап: {last_backup_str}

<b>Платёжные системы:</b>
• ЮMoney: {YOOMONEY_WALLET[:10]}...
• Crypto Bot: {'Подключён' if CRYPTOBOT_TOKEN else 'Не подключён'}

<b>Администраторы ({len(ADMIN_IDS)}):</b>
"""
    
    for admin_id in ADMIN_IDS:
        admin = db.get_user(admin_id)
        if admin:
            text += f"• @{admin['username']} ({admin_id})\n"
    
    text += f"\n<b>Производительность:</b>\n"
    text += f"• Кэш: {CACHE_TTL} сек\n"
    text += f"• Макс. задач: {MAX_CONCURRENT_TASKS}"
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("◀️ Назад", callback_data="admin")
        )
    )

# ================= АНТИ-СОН СИСТЕМА =================

async def keep_alive():
    """Анти-сон система для Render"""
    while True:
        try:
            await bot.send_chat_action(ADMIN_IDS[0], 'typing')
            logger.info("🔄 Keep-alive ping")
        except Exception as e:
            logger.error(f"Ошибка keep-alive: {e}")
        await asyncio.sleep(30)

async def auto_backup():
    """Автоматический бекап базы данных"""
    while True:
        try:
            if config.get('backup_enabled', True):
                last_backup = config.get('last_backup', 0)
                backup_interval = config.get('backup_interval', 3600)
                
                if time.time() - last_backup > backup_interval:
                    backup_file = db.create_backup()
                    if backup_file:
                        config.set('last_backup', time.time())
                        logger.info(f"✅ Автоматический бекап создан: {backup_file}")
                        
                        # Уведомляем админов
                        for admin_id in ADMIN_IDS:
                            try:
                                await bot.send_message(
                                    admin_id,
                                    f"💾 <b>Автоматический бекап создан</b>\n\n"
                                    f"Файл: {os.path.basename(backup_file)}"
                                )
                            except:
                                pass
        except Exception as e:
            logger.error(f"Ошибка авто-бекапа: {e}")
        
        await asyncio.sleep(3600)  # Проверяем каждый час

async def clean_expired_codes():
    """Очистка просроченных кодов"""
    while True:
        try:
            with db.get_cursor() as cursor:
                now = time.time()
                cursor.execute('''
                    UPDATE numbers 
                    SET status = 'available', buyer_id = NULL, purchased_at = NULL, code = NULL, code_expires = NULL
                    WHERE status = 'pending' AND code_expires < ?
                ''', (now,))
                
                if cursor.rowcount > 0:
                    logger.info(f"Очищено {cursor.rowcount} просроченных кодов")
                    db.cache = {k: v for k, v in db.cache.items() if not k.startswith('numbers_')}
        except Exception as e:
            logger.error(f"Ошибка очистки кодов: {e}")
        await asyncio.sleep(3600)

# ================= ВЕБ-СЕРВЕР ДЛЯ RENDER =================

async def handle(request):
    """Обработчик HTTP запросов для keep-alive"""
    return web.Response(text="Bot is running!")

async def payment_webhook(request):
    """Webhook для платежей"""
    try:
        data = await request.json()
        logger.info(f"Получен webhook: {data}")
        
        # Обработка платежей от Crypto Bot
        if 'payload' in data:
            payment_id = data['payload']
            if data.get('status') == 'paid':
                completed_payment = db.complete_payment(payment_id)
                if completed_payment:
                    user = db.get_user(completed_payment['user_id'])
                    try:
                        await bot.send_message(
                            completed_payment['user_id'],
                            f"✅ <b>Оплата успешна!</b>\n\n"
                            f"💰 Зачислено: {completed_payment['stars_amount']} ⭐️\n"
                            f"💎 Новый баланс: {user['stars_balance']} ⭐️"
                        )
                    except:
                        pass
        
        return web.Response(status=200)
    except Exception as e:
        logger.error(f"Ошибка webhook: {e}")
        return web.Response(status=500)

async def web_server():
    """Запуск веб-сервера для Render"""
    app = web.Application()
    app.router.add_get('/', handle)
    app.router.add_get('/health', handle)
    app.router.add_post('/api/cryptobot/webhook', payment_webhook)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    
    logger.info(f"✅ Веб-сервер запущен на порту {PORT}")
    
    while True:
        await asyncio.sleep(3600)

# ================= ЗАПУСК БОТА =================

async def on_startup(dp):
    """Действия при запуске"""
    os.makedirs(SESSIONS_DIR, exist_ok=True)
    os.makedirs(DATABASE_BACKUP_DIR, exist_ok=True)
    
    logger.info("✅ База данных подключена")
    
    # Делаем первого админа
    first_admin = ADMIN_IDS[0]
    admin = db.get_user(first_admin)
    
    if not admin:
        db.create_user(
            user_id=first_admin,
            username="admin",
            first_name="Admin",
            last_name=""
        )
    
    db.set_admin(first_admin, True)
    
    # Запускаем фоновые задачи
    asyncio.create_task(keep_alive())
    asyncio.create_task(auto_backup())
    asyncio.create_task(clean_expired_codes())
    asyncio.create_task(web_server())
    
    # Уведомление админам
    maintenance_status = "🔧 ВКЛЮЧЕН" if config.maintenance_mode else "✅ ВЫКЛЮЧЕН"
    
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"🚀 <b>Numbers Shop Bot v4.0 запущен!</b>\n\n"
                f"⚡ Режим: Production (Render)\n"
                f"🔧 Техработы: {maintenance_status}\n"
                f"📊 Пользователей: {db.get_stats()['total_users']}\n"
                f"📱 Номеров: {db.get_stats()['available_numbers']}\n"
                f"💾 Авто-бекап: Каждый час",
                parse_mode=ParseMode.HTML
            )
        except:
            pass
    
    logger.info("✅ Бот успешно запущен на Render")

async def on_shutdown(dp):
    """Действия при остановке"""
    # Создаём финальный бекап
    db.create_backup()
    
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                "🛑 <b>Бот остановлен</b>\n\n"
                "✅ Финальный бекап создан",
                parse_mode=ParseMode.HTML
            )
        except:
            pass
    
    db.cache.clear()
    logger.info("✅ Бот остановлен, данные сохранены")

if __name__ == "__main__":
    print("=" * 60)
    print("🚀 Telegram Numbers Shop Bot v4.0")
    print("⚡ Production Ready for Render")
    print("📱 2-х колоночная админка + Техработы")
    print("=" * 60)
    print(f"✅ Bot Token: {BOT_TOKEN[:10]}...")
    print(f"✅ Admins: {ADMIN_IDS}")
    print(f"✅ Database: {DATABASE_FILE}")
    print(f"✅ Port: {PORT}")
    print("=" * 60)
    
    executor.start_polling(
        dp,
        skip_updates=True,
        on_startup=on_startup,
        on_shutdown=on_shutdown
    )
