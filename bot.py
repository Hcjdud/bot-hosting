"""
Telegram Numbers Shop Bot + Session Manager
Версия: 18.0 (FINAL - ПОЛНАЯ ВЕРСИЯ)
Функции:
- Продажа виртуальных номеров Telegram
- Создание и управление сессиями Telegram аккаунтов
- Автоматическое получение кодов подтверждения
- Поддержка двухфакторной аутентификации (2FA)
- 3 СПОСОБА ПОПОЛНЕНИЯ БАЛАНСА
- Админ-панель с выдачей звёзд
- ✅ СЕССИИ СОХРАНЯЮТСЯ В ФАЙЛЫ
- ✅ ВОССТАНОВЛЕНИЕ ПОСЛЕ ПЕРЕЗАПУСКА
- Полный мониторинг и логирование
- Поддержка PostgreSQL на Render
- СИСТЕМА БЕСКОНЕЧНОЙ РАБОТЫ
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
import shutil
import signal
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from contextlib import contextmanager
from urllib.parse import urlparse
from functools import wraps

# Дополнительные импорты
import requests
import urllib3
import certifi
import psutil
from dotenv import load_dotenv
import pytz
from cryptography.fernet import Fernet
from Crypto.Cipher import AES

# Для PostgreSQL
import psycopg2
import psycopg2.extras

# Загружаем переменные окружения
load_dotenv()

# ================= ПРОВЕРКА RENDER =================

IS_RENDER = os.environ.get('RENDER', False)
RENDER_EXTERNAL_URL = os.environ.get('RENDER_EXTERNAL_URL', 'localhost')
PORT = int(os.environ.get('PORT', 8080))
BASE_URL = os.environ.get('BASE_URL', f'http://localhost:{PORT}')

# Получаем строку подключения к PostgreSQL
DATABASE_URL = os.environ.get('DATABASE_URL')
if DATABASE_URL and DATABASE_URL.startswith('postgres://'):
    DATABASE_URL = DATABASE_URL.replace('postgres://', 'postgresql://', 1)

# Настройка путей
if IS_RENDER:
    print("🔄 Запуск на Render платформе")
    SESSIONS_DIR = '/tmp/sessions'
    DATABASE_BACKUP_DIR = '/tmp/backups'
else:
    SESSIONS_DIR = "sessions"
    DATABASE_BACKUP_DIR = "backups"

# Создаем папки
os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(DATABASE_BACKUP_DIR, exist_ok=True)

# Проверяем доступность папки для сессий
test_session_file = os.path.join(SESSIONS_DIR, "test_write.tmp")
try:
    with open(test_session_file, "w") as f:
        f.write("test")
    os.remove(test_session_file)
    logger.info(f"✅ Папка {SESSIONS_DIR} доступна для записи")
except Exception as e:
    logger.error(f"❌ Нет доступа к папке сессий {SESSIONS_DIR}: {e}")

# Импорты для aiogram
from aiogram import Bot, Dispatcher, types
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.dispatcher.middlewares import BaseMiddleware
from aiogram.types import ParseMode, InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.types import Message, CallbackQuery, ContentType
from aiogram.utils import executor
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.contrib.fsm_storage.memory import MemoryStorage
from aiogram.utils.callback_data import CallbackData
from aiogram.utils.exceptions import Unauthorized, RestartingTelegram

# Pyrogram для управления сессиями
from pyrogram import Client
from pyrogram.errors import (
    SessionPasswordNeeded, 
    PhoneCodeInvalid, 
    PhoneNumberInvalid,
    FloodWait,
    PhoneCodeExpired,
    PasswordHashInvalid,
    UserDeactivated,
    SessionRevoked,
    AuthKeyDuplicated
)

# Для веб-сервера
from aiohttp import web

# ================= КОНФИГУРАЦИЯ =================

# Берем токен из переменных окружения
BOT_TOKEN = os.environ.get('BOT_TOKEN', "8594091933:AAHk_2iQEdLtlP48zbEqAow3JS4wYxQo0rY")

# ✅ СПИСОК АДМИНОВ
ADMIN_IDS = [8443743937, 7828977683]

# API данные для Pyrogram
API_ID = 26694682
API_HASH = "1278d6017ba6d2fd2228e69c638f332f"

# Платёжные системы
YOOMONEY_WALLET = "4100119410890051"
YOOMONEY_SECRET = os.environ.get('YOOMONEY_SECRET', '')

# Crypto Bot токен
CRYPTOBOT_TOKEN = "UQCpU74nU-1MoECyq1IH24WA3677rgWtsVtJKEGVUGnVyawR"

# Курс: 1 звезда = X рублей
STAR_TO_RUB = 1.5

# Минимальные и максимальные суммы пополнения
MIN_TOPUP_AMOUNT = 10
MAX_TOPUP_AMOUNT = 100000

# Настройки кэша
CACHE_TTL = 60

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

logger.info(f"📁 Sessions dir: {SESSIONS_DIR}")
logger.info(f"📁 Backups dir: {DATABASE_BACKUP_DIR}")
logger.info(f"👥 Администраторы: {ADMIN_IDS}")
if DATABASE_URL:
    logger.info(f"✅ Используется PostgreSQL")
else:
    logger.info(f"⚠️ Используется SQLite")

# Инициализация бота
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(bot, storage=storage)
dp.middleware.setup(LoggingMiddleware())

# Callback data для инлайн кнопок
numbers_cb = CallbackData('numbers', 'page')
buy_cb = CallbackData('buy', 'number_id')
sessions_cb = CallbackData('sessions', 'page')
session_cb = CallbackData('session', 'action', 'phone')
admin_cb = CallbackData('admin', 'action', 'page')
payment_cb = CallbackData('payment', 'action', 'payment_id')
account_cb = CallbackData('account', 'action', 'phone')
user_cb = CallbackData('user', 'action', 'user_id')
topup_cb = CallbackData('topup', 'method', 'amount')

# ================= СИСТЕМА АВТОМАТИЧЕСКОГО ПЕРЕЗАПУСКА =================

running = True
restart_requested = False
last_message_time = time.time()
restart_count = 0
max_restarts = 100
restart_window = 3600
restart_times = []

def should_restart():
    """Проверка, можно ли перезапустить бота"""
    global restart_times
    
    current_time = time.time()
    restart_times = [t for t in restart_times if current_time - t < restart_window]
    
    if len(restart_times) >= max_restarts:
        logger.critical(f"❌ Слишком много перезапусков ({len(restart_times)} за {restart_window/3600}ч)")
        return False
    
    restart_times.append(current_time)
    return True

def restart_bot():
    """Принудительный перезапуск бота"""
    if not should_restart():
        logger.critical("❌ Достигнут лимит перезапусков, бот останавливается")
        sys.exit(1)
    
    logger.info("🔄 Перезапуск бота через 3 секунды...")
    time.sleep(3)
    
    # Сохраняем данные перед перезапуском
    try:
        if 'db' in globals() and hasattr(db, 'create_backup'):
            db.create_backup()
    except:
        pass
    
    python = sys.executable
    os.execl(python, python, *sys.argv)

def signal_handler(sig, frame):
    """Обработчик сигналов"""
    global running
    logger.info(f"📡 Получен сигнал {sig}, завершаем работу...")
    running = False
    time.sleep(2)
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)
signal.signal(signal.SIGTERM, signal_handler)

def global_exception_handler(exc_type, exc_value, exc_traceback):
    """Глобальный обработчик исключений"""
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    
    logger.error("❌ НЕОБРАБОТАННОЕ ИСКЛЮЧЕНИЕ:", exc_info=(exc_type, exc_value, exc_traceback))
    
    # Пытаемся отправить уведомление админу
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(notify_admin_crash(exc_type, exc_value))
        loop.close()
    except:
        pass
    
    restart_bot()

sys.excepthook = global_exception_handler

def protect_coro(coro):
    """Декоратор для защиты корутин от падений"""
    @wraps(coro)
    async def wrapper(*args, **kwargs):
        try:
            return await coro(*args, **kwargs)
        except Exception as e:
            logger.error(f"❌ Ошибка в корутине {coro.__name__}: {e}")
            logger.error(traceback.format_exc())
            return None
    return wrapper

async def notify_admin_crash(exc_type, exc_value):
    """Уведомление админа о падении"""
    try:
        for admin_id in ADMIN_IDS:
            await bot.send_message(
                admin_id,
                f"⚠️ <b>Бот упал с ошибкой!</b>\n\n"
                f"Тип: {exc_type.__name__}\n"
                f"Ошибка: {str(exc_value)[:200]}\n\n"
                f"🔄 Автоматический перезапуск через 3 секунды..."
            )
    except:
        pass

# ================= БАЗА ДАННЫХ =================

class Database:
    def __init__(self):
        self.cache = {}
        self.db_url = DATABASE_URL
        
        if self.db_url:
            logger.info("✅ Инициализация PostgreSQL...")
            self._init_postgres()
        else:
            logger.info("⚠️ Инициализация SQLite...")
            self.db_path = "shop.db"
            self._init_sqlite()
    
    def create_backup(self):
        """Создание бекапа БД"""
        try:
            if not self.db_url:  # Только для SQLite
                timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
                backup_file = os.path.join(DATABASE_BACKUP_DIR, f"backup_{timestamp}.db")
                shutil.copy2(self.db_path, backup_file)
                logger.info(f"✅ Бекап создан: {backup_file}")
                return backup_file
            return None
        except Exception as e:
            logger.error(f"❌ Ошибка создания бекапа: {e}")
            return None
    
    def _init_postgres(self):
        """Инициализация PostgreSQL"""
        try:
            conn = psycopg2.connect(self.db_url)
            cursor = conn.cursor()
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    stars_balance INTEGER DEFAULT 0,
                    rub_balance REAL DEFAULT 0,
                    registered_at REAL,
                    last_activity REAL,
                    is_admin INTEGER DEFAULT 0,
                    banned INTEGER DEFAULT 0
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS tg_accounts (
                    phone TEXT PRIMARY KEY,
                    session_name TEXT UNIQUE,
                    api_id INTEGER,
                    api_hash TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    username TEXT,
                    user_id BIGINT,
                    status TEXT DEFAULT 'active',
                    added_by BIGINT,
                    added_at REAL,
                    last_used REAL,
                    last_code TEXT,
                    last_code_time REAL,
                    banned INTEGER DEFAULT 0,
                    spam_block INTEGER DEFAULT 0,
                    owner_id BIGINT DEFAULT 0,
                    owner_username TEXT,
                    owner_checked INTEGER DEFAULT 0,
                    has_2fa INTEGER DEFAULT 0,
                    notes TEXT
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS numbers (
                    id SERIAL PRIMARY KEY,
                    phone_number TEXT UNIQUE,
                    country TEXT,
                    description TEXT,
                    price_stars INTEGER,
                    price_rub REAL,
                    status TEXT DEFAULT 'available',
                    sold_to BIGINT,
                    sold_at REAL,
                    code TEXT,
                    code_expires REAL,
                    source_account TEXT REFERENCES tg_accounts(phone)
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS transactions (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    number_id INTEGER,
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
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS payments (
                    id TEXT PRIMARY KEY,
                    user_id BIGINT,
                    number_id INTEGER,
                    amount_rub REAL,
                    stars_amount INTEGER,
                    payment_system TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at REAL,
                    completed_at REAL,
                    payment_url TEXT
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS topups (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    amount_rub REAL,
                    stars_amount INTEGER,
                    payment_system TEXT,
                    payment_id TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at REAL,
                    completed_at REAL
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS session_logs (
                    id SERIAL PRIMARY KEY,
                    phone TEXT,
                    action TEXT,
                    result TEXT,
                    error TEXT,
                    created_at REAL
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS system_logs (
                    id SERIAL PRIMARY KEY,
                    level TEXT,
                    module TEXT,
                    message TEXT,
                    created_at REAL
                )
            ''')
            
            conn.commit()
            cursor.close()
            conn.close()
            logger.info("✅ Таблицы PostgreSQL созданы")
            
        except Exception as e:
            logger.error(f"❌ Ошибка инициализации PostgreSQL: {e}")
            self.db_url = None
            self.db_path = "shop.db"
            self._init_sqlite()
    
    def _init_sqlite(self):
        """Инициализация SQLite"""
        try:
            conn = sqlite3.connect(self.db_path, timeout=30)
            cursor = conn.cursor()
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    stars_balance INTEGER DEFAULT 0,
                    rub_balance REAL DEFAULT 0,
                    registered_at REAL,
                    last_activity REAL,
                    is_admin INTEGER DEFAULT 0,
                    banned INTEGER DEFAULT 0
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS tg_accounts (
                    phone TEXT PRIMARY KEY,
                    session_name TEXT UNIQUE,
                    api_id INTEGER,
                    api_hash TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    username TEXT,
                    user_id INTEGER,
                    status TEXT DEFAULT 'active',
                    added_by INTEGER,
                    added_at REAL,
                    last_used REAL,
                    last_code TEXT,
                    last_code_time REAL,
                    banned INTEGER DEFAULT 0,
                    spam_block INTEGER DEFAULT 0,
                    owner_id INTEGER DEFAULT 0,
                    owner_username TEXT,
                    owner_checked INTEGER DEFAULT 0,
                    has_2fa INTEGER DEFAULT 0,
                    notes TEXT
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS numbers (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone_number TEXT UNIQUE,
                    country TEXT,
                    description TEXT,
                    price_stars INTEGER,
                    price_rub REAL,
                    status TEXT DEFAULT 'available',
                    sold_to INTEGER,
                    sold_at REAL,
                    code TEXT,
                    code_expires REAL,
                    source_account TEXT REFERENCES tg_accounts(phone)
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    number_id INTEGER,
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
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS payments (
                    id TEXT PRIMARY KEY,
                    user_id INTEGER,
                    number_id INTEGER,
                    amount_rub REAL,
                    stars_amount INTEGER,
                    payment_system TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at REAL,
                    completed_at REAL,
                    payment_url TEXT
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS topups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    amount_rub REAL,
                    stars_amount INTEGER,
                    payment_system TEXT,
                    payment_id TEXT,
                    status TEXT DEFAULT 'pending',
                    created_at REAL,
                    completed_at REAL
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS session_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phone TEXT,
                    action TEXT,
                    result TEXT,
                    error TEXT,
                    created_at REAL
                )
            ''')
            
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS system_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level TEXT,
                    module TEXT,
                    message TEXT,
                    created_at REAL
                )
            ''')
            
            conn.commit()
            cursor.close()
            conn.close()
            logger.info(f"✅ Таблицы SQLite созданы: {self.db_path}")
            
        except Exception as e:
            logger.error(f"❌ Ошибка инициализации SQLite: {e}")
    
    def _get_connection(self):
        """Получение соединения с БД"""
        if self.db_url:
            try:
                conn = psycopg2.connect(self.db_url)
                conn.cursor_factory = psycopg2.extras.DictCursor
                return conn
            except Exception as e:
                logger.error(f"❌ Ошибка подключения к PostgreSQL: {e}")
                raise
        else:
            try:
                conn = sqlite3.connect(self.db_path, timeout=30)
                conn.row_factory = sqlite3.Row
                return conn
            except sqlite3.Error as e:
                logger.error(f"❌ Ошибка подключения к SQLite: {e}")
                raise
    
    @contextmanager
    def get_cursor(self):
        """Контекстный менеджер для БД"""
        conn = None
        cursor = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            yield cursor
            conn.commit()
        except Exception as e:
            logger.error(f"❌ Ошибка базы данных: {e}")
            if conn:
                conn.rollback()
            raise
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()
    
    # ===== Методы для пользователей =====
    
    def get_user(self, user_id: int) -> Optional[Dict]:
        cache_key = f'user_{user_id}'
        if cache_key in self.cache:
            cached, timestamp = self.cache[cache_key]
            if time.time() - timestamp < CACHE_TTL:
                return cached
        
        try:
            if self.db_url:
                with self.get_cursor() as cursor:
                    cursor.execute('SELECT * FROM users WHERE user_id = %s', (user_id,))
                    row = cursor.fetchone()
                    if row:
                        user = dict(row)
                        self.cache[cache_key] = (user, time.time())
                        return user
            else:
                with self.get_cursor() as cursor:
                    cursor.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
                    row = cursor.fetchone()
                    if row:
                        user = dict(row)
                        self.cache[cache_key] = (user, time.time())
                        return user
        except Exception as e:
            logger.error(f"Ошибка получения пользователя {user_id}: {e}")
        return None
    
    def create_user(self, user_id: int, username: str, first_name: str) -> bool:
        try:
            if self.db_url:
                with self.get_cursor() as cursor:
                    cursor.execute('''
                        INSERT INTO users (user_id, username, first_name, registered_at, last_activity)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (user_id) DO NOTHING
                    ''', (user_id, username, first_name, time.time(), time.time()))
                    return True
            else:
                with self.get_cursor() as cursor:
                    cursor.execute('''
                        INSERT OR IGNORE INTO users (user_id, username, first_name, registered_at, last_activity)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (user_id, username, first_name, time.time(), time.time()))
                    return True
        except Exception as e:
            logger.error(f"Ошибка создания пользователя {user_id}: {e}")
            return False
    
    def update_user_activity(self, user_id: int):
        try:
            if self.db_url:
                with self.get_cursor() as cursor:
                    cursor.execute('UPDATE users SET last_activity = %s WHERE user_id = %s', 
                                  (time.time(), user_id))
            else:
                with self.get_cursor() as cursor:
                    cursor.execute('UPDATE users SET last_activity = ? WHERE user_id = ?', 
                                  (time.time(), user_id))
            if f'user_{user_id}' in self.cache:
                del self.cache[f'user_{user_id}']
        except Exception as e:
            logger.error(f"Ошибка обновления активности {user_id}: {e}")
    
    def add_stars(self, user_id: int, amount: int, payment_system: str = "admin", payment_id: str = None) -> bool:
        """Добавление звёзд пользователю"""
        try:
            if self.db_url:  # PostgreSQL
                with self.get_cursor() as cursor:
                    # Обновляем баланс
                    cursor.execute('UPDATE users SET stars_balance = stars_balance + %s WHERE user_id = %s', 
                                 (amount, user_id))
                    
                    # Записываем транзакцию
                    cursor.execute('''
                        INSERT INTO transactions (user_id, amount_stars, amount_rub, type, payment_system, payment_id, status, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ''', (user_id, amount, amount * STAR_TO_RUB, 'credit', payment_system, payment_id, 'completed', time.time()))
            else:  # SQLite
                with self.get_cursor() as cursor:
                    # Обновляем баланс
                    cursor.execute('UPDATE users SET stars_balance = stars_balance + ? WHERE user_id = ?', 
                                 (amount, user_id))
                    
                    # Записываем транзакцию
                    cursor.execute('''
                        INSERT INTO transactions (user_id, amount_stars, amount_rub, type, payment_system, payment_id, status, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (user_id, amount, amount * STAR_TO_RUB, 'credit', payment_system, payment_id, 'completed', time.time()))
            
            # Очищаем кэш
            if f'user_{user_id}' in self.cache:
                del self.cache[f'user_{user_id}']
            
            logger.info(f"✅ Добавлено {amount}⭐ пользователю {user_id} через {payment_system}")
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка добавления звёзд {user_id}: {e}")
            return False
    
    def deduct_stars(self, user_id: int, amount: int, description: str = "") -> bool:
        try:
            if self.db_url:
                with self.get_cursor() as cursor:
                    cursor.execute('SELECT stars_balance FROM users WHERE user_id = %s', (user_id,))
                    row = cursor.fetchone()
                    if row and row['stars_balance'] >= amount:
                        cursor.execute('UPDATE users SET stars_balance = stars_balance - %s WHERE user_id = %s', 
                                     (amount, user_id))
                        cursor.execute('''
                            INSERT INTO transactions (user_id, amount_stars, type, description, created_at)
                            VALUES (%s, %s, 'debit', %s, %s)
                        ''', (user_id, amount, description, time.time()))
                        
                        if f'user_{user_id}' in self.cache:
                            del self.cache[f'user_{user_id}']
                        return True
            else:
                with self.get_cursor() as cursor:
                    cursor.execute('SELECT stars_balance FROM users WHERE user_id = ?', (user_id,))
                    row = cursor.fetchone()
                    if row and row['stars_balance'] >= amount:
                        cursor.execute('UPDATE users SET stars_balance = stars_balance - ? WHERE user_id = ?', 
                                     (amount, user_id))
                        cursor.execute('''
                            INSERT INTO transactions (user_id, amount_stars, type, description, created_at)
                            VALUES (?, ?, 'debit', ?, ?)
                        ''', (user_id, amount, description, time.time()))
                        
                        if f'user_{user_id}' in self.cache:
                            del self.cache[f'user_{user_id}']
                        return True
            return False
        except Exception as e:
            logger.error(f"Ошибка списания звёзд {user_id}: {e}")
            return False
    
    # ===== Методы для пополнений =====
    
    def create_topup(self, user_id: int, amount_rub: float, payment_system: str) -> Dict:
        """Создание записи о пополнении"""
        stars_amount = int(amount_rub / STAR_TO_RUB)
        payment_id = str(uuid.uuid4())
        
        try:
            if self.db_url:
                with self.get_cursor() as cursor:
                    cursor.execute('''
                        INSERT INTO topups (user_id, amount_rub, stars_amount, payment_system, payment_id, created_at)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        RETURNING id
                    ''', (user_id, amount_rub, stars_amount, payment_system, payment_id, time.time()))
                    row = cursor.fetchone()
                    topup_id = row['id'] if row else None
            else:
                with self.get_cursor() as cursor:
                    cursor.execute('''
                        INSERT INTO topups (user_id, amount_rub, stars_amount, payment_system, payment_id, created_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (user_id, amount_rub, stars_amount, payment_system, payment_id, time.time()))
                    topup_id = cursor.lastrowid
            
            return {
                'id': topup_id,
                'payment_id': payment_id,
                'user_id': user_id,
                'amount_rub': amount_rub,
                'stars_amount': stars_amount,
                'payment_system': payment_system
            }
        except Exception as e:
            logger.error(f"Ошибка создания пополнения: {e}")
            return None
    
    def get_topup(self, payment_id: str) -> Optional[Dict]:
        try:
            if self.db_url:
                with self.get_cursor() as cursor:
                    cursor.execute('SELECT * FROM topups WHERE payment_id = %s', (payment_id,))
                    row = cursor.fetchone()
                    return dict(row) if row else None
            else:
                with self.get_cursor() as cursor:
                    cursor.execute('SELECT * FROM topups WHERE payment_id = ?', (payment_id,))
                    row = cursor.fetchone()
                    return dict(row) if row else None
        except Exception as e:
            logger.error(f"Ошибка получения пополнения {payment_id}: {e}")
            return None
    
    def complete_topup(self, payment_id: str) -> bool:
        """Завершение пополнения и начисление звёзд"""
        try:
            topup = self.get_topup(payment_id)
            if not topup or topup['status'] != 'pending':
                return False
            
            if self.db_url:
                with self.get_cursor() as cursor:
                    cursor.execute('''
                        UPDATE topups SET status = 'completed', completed_at = %s WHERE payment_id = %s
                    ''', (time.time(), payment_id))
                    
                    cursor.execute('''
                        UPDATE users SET stars_balance = stars_balance + %s WHERE user_id = %s
                    ''', (topup['stars_amount'], topup['user_id']))
                    
                    cursor.execute('''
                        INSERT INTO transactions (user_id, amount_stars, amount_rub, type, payment_system, payment_id, status, created_at, completed_at)
                        VALUES (%s, %s, %s, 'credit', %s, %s, 'completed', %s, %s)
                    ''', (topup['user_id'], topup['stars_amount'], topup['amount_rub'], 
                          topup['payment_system'], payment_id, time.time(), time.time()))
            else:
                with self.get_cursor() as cursor:
                    cursor.execute('''
                        UPDATE topups SET status = 'completed', completed_at = ? WHERE payment_id = ?
                    ''', (time.time(), payment_id))
                    
                    cursor.execute('''
                        UPDATE users SET stars_balance = stars_balance + ? WHERE user_id = ?
                    ''', (topup['stars_amount'], topup['user_id']))
                    
                    cursor.execute('''
                        INSERT INTO transactions (user_id, amount_stars, amount_rub, type, payment_system, payment_id, status, created_at, completed_at)
                        VALUES (?, ?, ?, 'credit', ?, ?, 'completed', ?, ?)
                    ''', (topup['user_id'], topup['stars_amount'], topup['amount_rub'],
                          topup['payment_system'], payment_id, time.time(), time.time()))
            
            if f'user_{topup["user_id"]}' in self.cache:
                del self.cache[f'user_{topup["user_id"]}']
            
            logger.info(f"✅ Пополнение {payment_id} завершено, пользователь {topup['user_id']} получил {topup['stars_amount']}⭐")
            return True
            
        except Exception as e:
            logger.error(f"Ошибка завершения пополнения {payment_id}: {e}")
            return False
    
    # ===== Методы для Telegram аккаунтов =====
    
    def add_tg_account(self, phone: str, session_name: str, api_id: int, api_hash: str, 
                       user_info: Dict, added_by: int, has_2fa: bool = False) -> bool:
        try:
            if self.db_url:
                with self.get_cursor() as cursor:
                    cursor.execute('''
                        INSERT INTO tg_accounts 
                        (phone, session_name, api_id, api_hash, first_name, last_name, username, user_id, 
                         added_by, added_at, last_used, status, has_2fa)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (phone) DO UPDATE SET
                            session_name = EXCLUDED.session_name,
                            first_name = EXCLUDED.first_name,
                            last_name = EXCLUDED.last_name,
                            username = EXCLUDED.username,
                            user_id = EXCLUDED.user_id,
                            status = EXCLUDED.status,
                            last_used = EXCLUDED.last_used,
                            has_2fa = EXCLUDED.has_2fa
                    ''', (
                        phone, session_name, api_id, api_hash,
                        user_info.get('first_name', ''),
                        user_info.get('last_name', ''),
                        user_info.get('username', ''),
                        user_info.get('id', 0),
                        added_by, time.time(), time.time(),
                        'active', 1 if has_2fa else 0
                    ))
                    return True
            else:
                with self.get_cursor() as cursor:
                    cursor.execute('''
                        INSERT OR REPLACE INTO tg_accounts 
                        (phone, session_name, api_id, api_hash, first_name, last_name, username, user_id, 
                         added_by, added_at, last_used, status, has_2fa)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (
                        phone, session_name, api_id, api_hash,
                        user_info.get('first_name', ''),
                        user_info.get('last_name', ''),
                        user_info.get('username', ''),
                        user_info.get('id', 0),
                        added_by, time.time(), time.time(),
                        'active', 1 if has_2fa else 0
                    ))
                    return True
        except Exception as e:
            logger.error(f"Ошибка добавления аккаунта {phone}: {e}")
            return False
    
    def get_tg_account(self, phone: str) -> Optional[Dict]:
        try:
            if self.db_url:
                with self.get_cursor() as cursor:
                    cursor.execute('SELECT * FROM tg_accounts WHERE phone = %s', (phone,))
                    row = cursor.fetchone()
                    return dict(row) if row else None
            else:
                with self.get_cursor() as cursor:
                    cursor.execute('SELECT * FROM tg_accounts WHERE phone = ?', (phone,))
                    row = cursor.fetchone()
                    return dict(row) if row else None
        except Exception as e:
            logger.error(f"Ошибка получения аккаунта {phone}: {e}")
            return None
    
    def get_all_tg_accounts(self) -> List[Dict]:
        try:
            if self.db_url:
                with self.get_cursor() as cursor:
                    cursor.execute('SELECT * FROM tg_accounts ORDER BY added_at DESC')
                    return [dict(row) for row in cursor.fetchall()]
            else:
                with self.get_cursor() as cursor:
                    cursor.execute('SELECT * FROM tg_accounts ORDER BY added_at DESC')
                    return [dict(row) for row in cursor.fetchall()]
        except Exception as e:
            logger.error(f"Ошибка получения аккаунтов: {e}")
            return []
    
    def update_tg_account_status(self, phone: str, status: str, notes: str = ""):
        try:
            if self.db_url:
                with self.get_cursor() as cursor:
                    cursor.execute('''
                        UPDATE tg_accounts 
                        SET status = %s, notes = %s, last_used = %s 
                        WHERE phone = %s
                    ''', (status, notes, time.time(), phone))
            else:
                with self.get_cursor() as cursor:
                    cursor.execute('''
                        UPDATE tg_accounts 
                        SET status = ?, notes = ?, last_used = ? 
                        WHERE phone = ?
                    ''', (status, notes, time.time(), phone))
        except Exception as e:
            logger.error(f"Ошибка обновления статуса {phone}: {e}")
    
    def set_tg_account_code(self, phone: str, code: str):
        try:
            if self.db_url:
                with self.get_cursor() as cursor:
                    cursor.execute('''
                        UPDATE tg_accounts 
                        SET last_code = %s, last_code_time = %s 
                        WHERE phone = %s
                    ''', (code, time.time(), phone))
            else:
                with self.get_cursor() as cursor:
                    cursor.execute('''
                        UPDATE tg_accounts 
                        SET last_code = ?, last_code_time = ? 
                        WHERE phone = ?
                    ''', (code, time.time(), phone))
        except Exception as e:
            logger.error(f"Ошибка установки кода {phone}: {e}")
    
    def get_available_tg_account(self) -> Optional[Dict]:
        try:
            if self.db_url:
                with self.get_cursor() as cursor:
                    cursor.execute('''
                        SELECT * FROM tg_accounts 
                        WHERE status = 'active' AND banned = 0 AND spam_block = 0
                        ORDER BY last_used ASC
                        LIMIT 1
                    ''')
                    row = cursor.fetchone()
                    return dict(row) if row else None
            else:
                with self.get_cursor() as cursor:
                    cursor.execute('''
                        SELECT * FROM tg_accounts 
                        WHERE status = 'active' AND banned = 0 AND spam_block = 0
                        ORDER BY last_used ASC
                        LIMIT 1
                    ''')
                    row = cursor.fetchone()
                    return dict(row) if row else None
        except Exception as e:
            logger.error(f"Ошибка получения доступного аккаунта: {e}")
            return None
    
    def log_session_action(self, phone: str, action: str, result: str, error: str = ""):
        try:
            if self.db_url:
                with self.get_cursor() as cursor:
                    cursor.execute('''
                        INSERT INTO session_logs (phone, action, result, error, created_at)
                        VALUES (%s, %s, %s, %s, %s)
                    ''', (phone, action, result, error, time.time()))
            else:
                with self.get_cursor() as cursor:
                    cursor.execute('''
                        INSERT INTO session_logs (phone, action, result, error, created_at)
                        VALUES (?, ?, ?, ?, ?)
                    ''', (phone, action, result, error, time.time()))
        except Exception as e:
            logger.error(f"Ошибка логирования {phone}: {e}")
    
    def set_account_owner(self, phone: str, owner_id: int, owner_username: str):
        try:
            if self.db_url:
                with self.get_cursor() as cursor:
                    cursor.execute('''
                        UPDATE tg_accounts 
                        SET owner_id = %s, owner_username = %s, owner_checked = 1
                        WHERE phone = %s
                    ''', (owner_id, owner_username, phone))
            else:
                with self.get_cursor() as cursor:
                    cursor.execute('''
                        UPDATE tg_accounts 
                        SET owner_id = ?, owner_username = ?, owner_checked = 1
                        WHERE phone = ?
                    ''', (owner_id, owner_username, phone))
        except Exception as e:
            logger.error(f"Ошибка установки владельца {phone}: {e}")
    
    def check_account_owner(self, phone: str) -> Tuple[bool, int]:
        try:
            if self.db_url:
                with self.get_cursor() as cursor:
                    cursor.execute('SELECT owner_id, owner_checked FROM tg_accounts WHERE phone = %s', (phone,))
                    row = cursor.fetchone()
                    if row and row['owner_checked'] and row['owner_id'] > 0:
                        return True, row['owner_id']
            else:
                with self.get_cursor() as cursor:
                    cursor.execute('SELECT owner_id, owner_checked FROM tg_accounts WHERE phone = ?', (phone,))
                    row = cursor.fetchone()
                    if row and row['owner_checked'] and row['owner_id'] > 0:
                        return True, row['owner_id']
            return False, 0
        except Exception as e:
            logger.error(f"Ошибка проверки владельца {phone}: {e}")
            return False, 0
    
    def account_has_2fa(self, phone: str) -> bool:
        """Проверка, есть ли у аккаунта 2FA"""
        try:
            if self.db_url:
                with self.get_cursor() as cursor:
                    cursor.execute('SELECT has_2fa FROM tg_accounts WHERE phone = %s', (phone,))
                    row = cursor.fetchone()
                    return bool(row and row['has_2fa'])
            else:
                with self.get_cursor() as cursor:
                    cursor.execute('SELECT has_2fa FROM tg_accounts WHERE phone = ?', (phone,))
                    row = cursor.fetchone()
                    return bool(row and row['has_2fa'])
        except Exception as e:
            logger.error(f"Ошибка проверки 2FA {phone}: {e}")
            return False
    
    # ===== Методы для номеров =====
    
    def add_number(self, phone: str, country: str, description: str, 
                   price_stars: int, source_account: str = None) -> bool:
        try:
            price_rub = price_stars * STAR_TO_RUB
            if self.db_url:
                with self.get_cursor() as cursor:
                    cursor.execute('''
                        INSERT INTO numbers 
                        (phone_number, country, description, price_stars, price_rub, source_account, status)
                        VALUES (%s, %s, %s, %s, %s, %s, 'available')
                        ON CONFLICT (phone_number) DO UPDATE SET
                            country = EXCLUDED.country,
                            description = EXCLUDED.description,
                            price_stars = EXCLUDED.price_stars,
                            price_rub = EXCLUDED.price_rub,
                            status = 'available'
                    ''', (phone, country, description, price_stars, price_rub, source_account))
                    logger.info(f"✅ Добавлен номер: {phone} | {country} | {price_stars}⭐")
                    return True
            else:
                with self.get_cursor() as cursor:
                    cursor.execute('''
                        INSERT OR REPLACE INTO numbers 
                        (phone_number, country, description, price_stars, price_rub, source_account, status)
                        VALUES (?, ?, ?, ?, ?, ?, 'available')
                    ''', (phone, country, description, price_stars, price_rub, source_account))
                    logger.info(f"✅ Добавлен номер: {phone} | {country} | {price_stars}⭐")
                    return True
        except Exception as e:
            logger.error(f"❌ Ошибка добавления номера {phone}: {e}")
            return False
    
    def get_available_numbers(self, page: int = 1, limit: int = 5) -> Tuple[List[Dict], int]:
        offset = (page - 1) * limit
        cache_key = f'numbers_{page}_{limit}'
        
        if cache_key in self.cache:
            cached, timestamp = self.cache[cache_key]
            if time.time() - timestamp < CACHE_TTL:
                return cached
        
        try:
            if self.db_url:
                with self.get_cursor() as cursor:
                    cursor.execute('SELECT COUNT(*) as count FROM numbers WHERE status = %s', ('available',))
                    total = cursor.fetchone()['count']
                    
                    cursor.execute('''
                        SELECT * FROM numbers 
                        WHERE status = %s 
                        ORDER BY price_stars ASC 
                        LIMIT %s OFFSET %s
                    ''', ('available', limit, offset))
                    
                    numbers = [dict(row) for row in cursor.fetchall()]
                    result = (numbers, total)
                    self.cache[cache_key] = (result, time.time())
                    return result
            else:
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
        except Exception as e:
            logger.error(f"Ошибка получения номеров: {e}")
            return [], 0
    
    def get_number(self, number_id: int) -> Optional[Dict]:
        try:
            if self.db_url:
                with self.get_cursor() as cursor:
                    cursor.execute('SELECT * FROM numbers WHERE id = %s', (number_id,))
                    row = cursor.fetchone()
                    return dict(row) if row else None
            else:
                with self.get_cursor() as cursor:
                    cursor.execute('SELECT * FROM numbers WHERE id = ?', (number_id,))
                    row = cursor.fetchone()
                    return dict(row) if row else None
        except Exception as e:
            logger.error(f"Ошибка получения номера {number_id}: {e}")
            return None
    
    def purchase_number(self, number_id: int, user_id: int) -> Optional[Dict]:
        try:
            if self.db_url:
                with self.get_cursor() as cursor:
                    cursor.execute('SELECT * FROM numbers WHERE id = %s AND status = %s', 
                                 (number_id, 'available'))
                    number = cursor.fetchone()
                    if not number:
                        return None
                    number = dict(number)
                    
                    cursor.execute('SELECT stars_balance FROM users WHERE user_id = %s', (user_id,))
                    user = cursor.fetchone()
                    if not user or user['stars_balance'] < number['price_stars']:
                        return None
                    
                    cursor.execute('UPDATE users SET stars_balance = stars_balance - %s WHERE user_id = %s', 
                                 (number['price_stars'], user_id))
                    
                    cursor.execute('''
                        UPDATE numbers 
                        SET status = 'pending', sold_to = %s, sold_at = %s
                        WHERE id = %s
                    ''', (user_id, time.time(), number_id))
                    
                    cursor.execute('''
                        INSERT INTO transactions (user_id, number_id, amount_stars, status, created_at)
                        VALUES (%s, %s, %s, 'pending', %s)
                    ''', (user_id, number_id, number['price_stars'], time.time()))
            else:
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
                        SET status = 'pending', sold_to = ?, sold_at = ?
                        WHERE id = ?
                    ''', (user_id, time.time(), number_id))
                    
                    cursor.execute('''
                        INSERT INTO transactions (user_id, number_id, amount_stars, status, created_at)
                        VALUES (?, ?, ?, 'pending', ?)
                    ''', (user_id, number_id, number['price_stars'], time.time()))
            
            self.cache = {k: v for k, v in self.cache.items() if not k.startswith('numbers_')}
            if f'user_{user_id}' in self.cache:
                del self.cache[f'user_{user_id}']
            
            return number
            
        except Exception as e:
            logger.error(f"Ошибка покупки {number_id}: {e}")
            return None
    
    def set_number_code(self, number_id: int, code: str) -> bool:
        try:
            if self.db_url:
                with self.get_cursor() as cursor:
                    cursor.execute('''
                        UPDATE numbers 
                        SET code = %s, code_expires = %s, status = 'sold'
                        WHERE id = %s
                    ''', (code, time.time() + 3600, number_id))
            else:
                with self.get_cursor() as cursor:
                    cursor.execute('''
                        UPDATE numbers 
                        SET code = ?, code_expires = ?, status = 'sold'
                        WHERE id = ?
                    ''', (code, time.time() + 3600, number_id))
            
            logger.info(f"✅ Для номера {number_id} установлен код: {code}")
            return True
        except Exception as e:
            logger.error(f"Ошибка установки кода {number_id}: {e}")
            return False
    
    def delete_sold_number(self, number_id: int) -> bool:
        try:
            if self.db_url:
                with self.get_cursor() as cursor:
                    cursor.execute('DELETE FROM numbers WHERE id = %s AND status = %s', 
                                 (number_id, 'sold'))
                    if cursor.rowcount > 0:
                        logger.info(f"✅ Номер {number_id} удален из магазина")
                        self.cache = {k: v for k, v in self.cache.items() if not k.startswith('numbers_')}
                        return True
            else:
                with self.get_cursor() as cursor:
                    cursor.execute('DELETE FROM numbers WHERE id = ? AND status = "sold"', (number_id,))
                    if cursor.rowcount > 0:
                        logger.info(f"✅ Номер {number_id} удален из магазина")
                        self.cache = {k: v for k, v in self.cache.items() if not k.startswith('numbers_')}
                        return True
            return False
        except Exception as e:
            logger.error(f"❌ Ошибка удаления номера {number_id}: {e}")
            return False
    
    def get_stats(self) -> Dict:
        try:
            if self.db_url:
                with self.get_cursor() as cursor:
                    cursor.execute('SELECT COUNT(*) as count FROM users')
                    total_users = cursor.fetchone()['count']
                    
                    cursor.execute("SELECT COUNT(*) as count FROM numbers WHERE status = 'available'")
                    available_numbers = cursor.fetchone()['count']
                    
                    cursor.execute("SELECT COUNT(*) as count FROM numbers WHERE status = 'sold'")
                    sold_numbers = cursor.fetchone()['count']
                    
                    cursor.execute("SELECT COUNT(*) as count FROM numbers WHERE status = 'pending'")
                    pending_numbers = cursor.fetchone()['count']
                    
                    cursor.execute('SELECT COUNT(*) as count FROM tg_accounts')
                    total_accounts = cursor.fetchone()['count']
                    
                    cursor.execute("SELECT COUNT(*) as count FROM tg_accounts WHERE status = 'active'")
                    active_accounts = cursor.fetchone()['count']
                    
                    cursor.execute('SELECT SUM(amount_stars) as total FROM transactions WHERE status = %s', 
                                 ('completed',))
                    total_stars_sold = cursor.fetchone()['total'] or 0
            else:
                with self.get_cursor() as cursor:
                    cursor.execute('SELECT COUNT(*) as count FROM users')
                    total_users = cursor.fetchone()['count']
                    
                    cursor.execute("SELECT COUNT(*) as count FROM numbers WHERE status = 'available'")
                    available_numbers = cursor.fetchone()['count']
                    
                    cursor.execute("SELECT COUNT(*) as count FROM numbers WHERE status = 'sold'")
                    sold_numbers = cursor.fetchone()['count']
                    
                    cursor.execute("SELECT COUNT(*) as count FROM numbers WHERE status = 'pending'")
                    pending_numbers = cursor.fetchone()['count']
                    
                    cursor.execute('SELECT COUNT(*) as count FROM tg_accounts')
                    total_accounts = cursor.fetchone()['count']
                    
                    cursor.execute("SELECT COUNT(*) as count FROM tg_accounts WHERE status = 'active'")
                    active_accounts = cursor.fetchone()['count']
                    
                    cursor.execute('SELECT SUM(amount_stars) as total FROM transactions WHERE status = "completed"')
                    total_stars_sold = cursor.fetchone()['total'] or 0
            
            return {
                'total_users': total_users,
                'available_numbers': available_numbers,
                'sold_numbers': sold_numbers,
                'pending_numbers': pending_numbers,
                'total_accounts': total_accounts,
                'active_accounts': active_accounts,
                'total_stars_sold': total_stars_sold,
                'total_revenue_rub': total_stars_sold * STAR_TO_RUB
            }
        except Exception as e:
            logger.error(f"Ошибка получения статистики: {e}")
            return {
                'total_users': 0,
                'available_numbers': 0,
                'sold_numbers': 0,
                'pending_numbers': 0,
                'total_accounts': 0,
                'active_accounts': 0,
                'total_stars_sold': 0,
                'total_revenue_rub': 0
            }

# Инициализация БД
db = Database()

# ================= УПРАВЛЕНИЕ СЕССИЯМИ TELEGRAM =================

class SessionManager:
    """Класс для управления сессиями Telegram аккаунтов"""
    
    def __init__(self):
        self.active_sessions = {}  # phone -> client
        self.waiting_codes = {}  # phone -> {'number_id': id, 'user_id': id}
        self.waiting_2fa = {}  # phone -> {'number_id': id, 'user_id': id, 'client': client}
        self.session_watchers = {}  # phone -> task
    
    async def load_saved_sessions(self):
        """Загрузка сохраненных сессий из файлов"""
        try:
            accounts = db.get_all_tg_accounts()
            loaded = 0
            for account in accounts:
                if account['status'] == 'active' and account.get('owner_id', 0) == 0:
                    # Проверяем, существует ли файл сессии
                    session_path = os.path.join(SESSIONS_DIR, account['session_name'])
                    if os.path.exists(f"{session_path}.session"):
                        logger.info(f"🔄 Найдена сохраненная сессия для {account['phone']}")
                        loaded += 1
            logger.info(f"✅ Загружено {loaded} сохраненных сессий")
        except Exception as e:
            logger.error(f"❌ Ошибка загрузки сессий: {e}")
    
    async def watch_session(self, phone: str, client: Client):
        """Наблюдение за сессией (бесконечное)"""
        try:
            while True:
                try:
                    if not await client.is_user_authorized():
                        logger.warning(f"⚠️ Сессия {phone} потеряла авторизацию")
                        break
                    
                    has_owner, owner_id = db.check_account_owner(phone)
                    if has_owner:
                        logger.info(f"👤 Аккаунт {phone} имеет владельца {owner_id}, выходим...")
                        await self.logout_session(phone, "owner_logged_in")
                        break
                    
                    await asyncio.sleep(30)
                    
                except Exception as e:
                    logger.error(f"❌ Ошибка в watcher для {phone}: {e}")
                    await asyncio.sleep(60)
        except asyncio.CancelledError:
            logger.info(f"🛑 Watcher для {phone} остановлен")
    
    async def logout_session(self, phone: str, reason: str):
        """Принудительный выход из сессии"""
        try:
            if phone in self.active_sessions:
                client = self.active_sessions[phone]
                try:
                    await client.log_out()
                except:
                    pass
                await client.disconnect()
                
                del self.active_sessions[phone]
                
                if phone in self.session_watchers:
                    self.session_watchers[phone].cancel()
                    del self.session_watchers[phone]
                
                # Удаляем файл сессии
                account = db.get_tg_account(phone)
                if account:
                    session_path = os.path.join(SESSIONS_DIR, account['session_name'])
                    if os.path.exists(f"{session_path}.session"):
                        os.remove(f"{session_path}.session")
                        logger.info(f"🗑 Удален файл сессии для {phone}")
                
                db.update_tg_account_status(phone, 'logged_out', f"Причина: {reason}")
                db.log_session_action(phone, 'logout', 'success', reason)
                logger.info(f"✅ Сессия {phone} завершена: {reason}")
        except Exception as e:
            logger.error(f"❌ Ошибка выхода из сессии {phone}: {e}")
    
    async def get_client(self, phone: str) -> Optional[Client]:
        """Получение клиента для аккаунта с сохранением сессии в файл"""
        if phone in self.active_sessions:
            return self.active_sessions[phone]
        
        account = db.get_tg_account(phone)
        if not account:
            logger.error(f"❌ Аккаунт {phone} не найден в БД")
            return None
        
        has_owner, owner_id = db.check_account_owner(phone)
        if has_owner:
            logger.warning(f"⚠️ Аккаунт {phone} имеет владельца {owner_id}, не подключаемся")
            return None
        
        session_path = os.path.join(SESSIONS_DIR, account['session_name'])
        
        # ✅ ИСПРАВЛЕНО: убран in_memory=True, сессии будут сохраняться в файлы
        client = Client(
            name=session_path,
            api_id=account['api_id'],
            api_hash=account['api_hash'],
            workdir=SESSIONS_DIR,
            device_model="Server Bot",
            system_version="4.16.30-vxCUSTOM",
            app_version="1.0.0"
        )
        
        try:
            await client.connect()
            if await client.is_user_authorized():
                self.active_sessions[phone] = client
                db.update_tg_account_status(phone, 'active')
                db.log_session_action(phone, 'connect', 'success')
                
                watcher_task = asyncio.create_task(self.watch_session(phone, client))
                self.session_watchers[phone] = watcher_task
                
                logger.info(f"✅ Подключена сессия для {phone} из файла {session_path}.session")
                return client
            else:
                await client.disconnect()
                db.update_tg_account_status(phone, 'unauthorized')
                db.log_session_action(phone, 'connect', 'fail', 'not authorized')
                return None
        except Exception as e:
            logger.error(f"❌ Ошибка подключения к аккаунту {phone}: {e}")
            db.log_session_action(phone, 'connect', 'error', str(e))
            return None
    
    async def request_code(self, phone: str, number_id: int, user_id: int) -> bool:
        """Запрос кода на указанный номер через аккаунт"""
        client = await self.get_client(phone)
        if not client:
            return False
        
        try:
            sent_code = await client.send_code(phone)
            
            self.waiting_codes[phone] = {
                'number_id': number_id,
                'user_id': user_id,
                'phone_code_hash': sent_code.phone_code_hash,
                'timestamp': time.time()
            }
            
            db.log_session_action(phone, 'request_code', 'success')
            return True
        except FloodWait as e:
            logger.warning(f"⚠️ Flood wait на {phone}: {e.value} сек")
            db.log_session_action(phone, 'request_code', 'flood', str(e.value))
            return False
        except Exception as e:
            logger.error(f"❌ Ошибка запроса кода на {phone}: {e}")
            db.log_session_action(phone, 'request_code', 'error', str(e))
            return False
    
    async def submit_code(self, phone: str, code: str) -> Optional[Dict]:
        """Отправка кода подтверждения"""
        if phone not in self.waiting_codes:
            logger.error(f"❌ Нет ожидающего кода для {phone}")
            return None
        
        wait_info = self.waiting_codes[phone]
        client = await self.get_client(phone)
        if not client:
            return None
        
        try:
            await client.sign_in(
                phone_number=phone,
                phone_code=code,
                phone_code_hash=wait_info['phone_code_hash']
            )
            
            me = await client.get_me()
            
            db.set_number_code(wait_info['number_id'], code)
            db.update_tg_account_status(phone, 'active')
            db.set_tg_account_code(phone, code)
            db.set_account_owner(phone, wait_info['user_id'], f"user_{wait_info['user_id']}")
            
            del self.waiting_codes[phone]
            db.log_session_action(phone, 'submit_code', 'success')
            
            logger.info(f"✅ Сессия для {phone} сохранена в файл")
            return {
                'number_id': wait_info['number_id'],
                'user_id': wait_info['user_id'],
                'code': code,
                'user_info': {
                    'id': me.id,
                    'first_name': me.first_name,
                    'username': me.username
                }
            }
        except SessionPasswordNeeded:
            logger.info(f"⚠️ Аккаунт {phone} требует 2FA")
            
            self.waiting_2fa[phone] = {
                'number_id': wait_info['number_id'],
                'user_id': wait_info['user_id'],
                'client': client,
                'timestamp': time.time()
            }
            
            del self.waiting_codes[phone]
            db.log_session_action(phone, 'submit_code', '2fa_required')
            return {'error': '2fa_required', 'phone': phone}
        except PhoneCodeInvalid:
            logger.warning(f"⚠️ Неверный код для {phone}")
            return {'error': 'invalid_code'}
        except PhoneCodeExpired:
            logger.warning(f"⚠️ Код истёк для {phone}")
            return {'error': 'code_expired'}
        except Exception as e:
            logger.error(f"❌ Ошибка отправки кода для {phone}: {e}")
            return {'error': str(e)}
    
    async def submit_2fa(self, phone: str, password: str) -> Optional[Dict]:
        """Отправка пароля 2FA"""
        if phone not in self.waiting_2fa:
            logger.error(f"❌ Нет ожидающего 2FA для {phone}")
            return None
        
        info = self.waiting_2fa[phone]
        client = info['client']
        
        try:
            await client.check_password(password)
            
            me = await client.get_me()
            
            # Генерируем случайный код для продажи
            fake_code = ''.join(random.choices(string.digits, k=5))
            db.set_number_code(info['number_id'], fake_code)
            
            db.update_tg_account_status(phone, 'active')
            db.set_tg_account_code(phone, fake_code)
            db.set_account_owner(phone, info['user_id'], f"user_{info['user_id']}")
            
            # Отмечаем, что у аккаунта есть 2FA
            if db.db_url:
                with db.get_cursor() as cursor:
                    cursor.execute('UPDATE tg_accounts SET has_2fa = 1 WHERE phone = %s', (phone,))
            else:
                with db.get_cursor() as cursor:
                    cursor.execute('UPDATE tg_accounts SET has_2fa = 1 WHERE phone = ?', (phone,))
            
            del self.waiting_2fa[phone]
            db.log_session_action(phone, 'submit_2fa', 'success')
            
            logger.info(f"✅ Сессия с 2FA для {phone} сохранена в файл")
            return {
                'number_id': info['number_id'],
                'user_id': info['user_id'],
                'code': fake_code,
                'user_info': {
                    'id': me.id,
                    'first_name': me.first_name,
                    'username': me.username
                }
            }
        except PasswordHashInvalid:
            logger.warning(f"⚠️ Неверный пароль 2FA для {phone}")
            return {'error': 'invalid_password'}
        except Exception as e:
            logger.error(f"❌ Ошибка отправки 2FA для {phone}: {e}")
            return {'error': str(e)}
    
    async def add_new_account(self, phone: str, api_id: int, api_hash: str, 
                             added_by: int) -> Tuple[bool, str]:
        """Добавление нового аккаунта"""
        try:
            if db.get_tg_account(phone):
                return False, "Аккаунт уже существует"
            
            session_name = f"acc_{phone.replace('+', '')}_{random.randint(1000, 9999)}"
            
            # ✅ ИСПРАВЛЕНО: убран in_memory=True
            client = Client(
                name=session_name,
                api_id=api_id,
                api_hash=api_hash,
                workdir=SESSIONS_DIR,
                device_model="Server Bot",
                system_version="4.16.30-vxCUSTOM",
                app_version="1.0.0"
            )
            
            await client.connect()
            sent_code = await client.send_code(phone)
            
            if db.db_url:
                with db.get_cursor() as cursor:
                    cursor.execute('''
                        INSERT INTO tg_accounts 
                        (phone, session_name, api_id, api_hash, added_by, added_at, status)
                        VALUES (%s, %s, %s, %s, %s, %s, 'pending')
                    ''', (phone, session_name, api_id, api_hash, added_by, time.time()))
            else:
                with db.get_cursor() as cursor:
                    cursor.execute('''
                        INSERT INTO tg_accounts 
                        (phone, session_name, api_id, api_hash, added_by, added_at, status)
                        VALUES (?, ?, ?, ?, ?, ?, 'pending')
                    ''', (phone, session_name, api_id, api_hash, added_by, time.time()))
            
            self.waiting_codes[phone] = {
                'action': 'add_account',
                'phone_code_hash': sent_code.phone_code_hash,
                'client': client,
                'session_name': session_name,
                'timestamp': time.time()
            }
            
            await client.disconnect()
            
            # Отправляем красивое сообщение о коде
            try:
                await bot.send_message(
                    added_by,
                    f"📲 <b>Код отправлен!</b>\n\n"
                    f"На номер <code>{phone}</code> отправлен код подтверждения.\n\n"
                    f"<b>Сообщение будет выглядеть так:</b>\n"
                    f"——————————————\n"
                    f"Код для входа в Telegram: <b>XXXXX</b>. Не давайте код никому, даже если его требуют от имени Telegram!\n\n"
                    f"❗️Этот код используется для входа в Ваш аккаунт в Telegram. Он не может быть использован для чего-либо ещё.\n\n"
                    f"Если Вы не запрашивали код для входа, проигнорируйте это сообщение.\n"
                    f"——————————————\n\n"
                    f"📝 Введите код из сообщения (только цифры):"
                )
            except Exception as e:
                logger.error(f"Ошибка отправки сообщения: {e}")
            
            return True, "Код отправлен на указанный номер"
            
        except PhoneNumberInvalid:
            return False, "❌ Неверный номер телефона"
        except FloodWait as e:
            return False, f"❌ Слишком много попыток. Подождите {e.value} сек"
        except Exception as e:
            logger.error(f"❌ Ошибка добавления аккаунта: {e}")
            return False, f"❌ Ошибка: {str(e)}"
    
    async def confirm_new_account(self, phone: str, code: str) -> Tuple[bool, str, Optional[Dict]]:
        """Подтверждение нового аккаунта кодом"""
        if phone not in self.waiting_codes or self.waiting_codes[phone].get('action') != 'add_account':
            return False, "Нет ожидающего подтверждения", None
        
        info = self.waiting_codes[phone]
        client = info['client']
        
        try:
            await client.connect()
            await client.sign_in(
                phone_number=phone,
                phone_code=code,
                phone_code_hash=info['phone_code_hash']
            )
            
            me = await client.get_me()
            
            if db.db_url:
                with db.get_cursor() as cursor:
                    cursor.execute('''
                        UPDATE tg_accounts 
                        SET first_name = %s, last_name = %s, username = %s, user_id = %s, 
                            status = 'active', last_used = %s
                        WHERE phone = %s
                    ''', (me.first_name or '', me.last_name or '', me.username or '', 
                          me.id, time.time(), phone))
            else:
                with db.get_cursor() as cursor:
                    cursor.execute('''
                        UPDATE tg_accounts 
                        SET first_name = ?, last_name = ?, username = ?, user_id = ?, 
                            status = 'active', last_used = ?
                        WHERE phone = ?
                    ''', (me.first_name or '', me.last_name or '', me.username or '', 
                          me.id, time.time(), phone))
            
            await client.disconnect()
            del self.waiting_codes[phone]
            
            logger.info(f"✅ Аккаунт {phone} добавлен, сессия сохранена в файл")
            return True, "Аккаунт успешно добавлен", {
                'id': me.id,
                'first_name': me.first_name,
                'username': me.username
            }
            
        except SessionPasswordNeeded:
            logger.info(f"⚠️ Аккаунт {phone} требует 2FA")
            
            self.waiting_2fa[phone] = {
                'action': 'add_account_2fa',
                'client': client,
                'session_name': info['session_name'],
                'timestamp': time.time()
            }
            
            del self.waiting_codes[phone]
            
            return False, "2FA_REQUIRED", None
            
        except PhoneCodeInvalid:
            return False, "Неверный код", None
        except PhoneCodeExpired:
            return False, "Код истёк", None
        except Exception as e:
            logger.error(f"❌ Ошибка подтверждения аккаунта: {e}")
            return False, str(e), None
    
    async def submit_account_2fa(self, phone: str, password: str) -> Tuple[bool, str, Optional[Dict]]:
        """Подтверждение аккаунта с 2FA"""
        if phone not in self.waiting_2fa or self.waiting_2fa[phone].get('action') != 'add_account_2fa':
            return False, "Нет ожидающего 2FA", None
        
        info = self.waiting_2fa[phone]
        client = info['client']
        
        try:
            await client.connect()
            await client.check_password(password)
            
            me = await client.get_me()
            
            if db.db_url:
                with db.get_cursor() as cursor:
                    cursor.execute('''
                        UPDATE tg_accounts 
                        SET first_name = %s, last_name = %s, username = %s, user_id = %s, 
                            status = 'active', last_used = %s, has_2fa = 1
                        WHERE phone = %s
                    ''', (me.first_name or '', me.last_name or '', me.username or '', 
                          me.id, time.time(), phone))
            else:
                with db.get_cursor() as cursor:
                    cursor.execute('''
                        UPDATE tg_accounts 
                        SET first_name = ?, last_name = ?, username = ?, user_id = ?, 
                            status = 'active', last_used = ?, has_2fa = 1
                        WHERE phone = ?
                    ''', (me.first_name or '', me.last_name or '', me.username or '', 
                          me.id, time.time(), phone))
            
            await client.disconnect()
            del self.waiting_2fa[phone]
            
            logger.info(f"✅ Аккаунт {phone} с 2FA добавлен, сессия сохранена в файл")
            return True, "Аккаунт успешно добавлен с 2FA", {
                'id': me.id,
                'first_name': me.first_name,
                'username': me.username
            }
            
        except PasswordHashInvalid:
            return False, "Неверный пароль 2FA", None
        except Exception as e:
            logger.error(f"❌ Ошибка подтверждения 2FA: {e}")
            return False, str(e), None
    
    async def cleanup(self):
        """Очистка неактивных сессий"""
        current_time = time.time()
        to_remove = []
        
        for phone, info in self.waiting_codes.items():
            if current_time - info['timestamp'] > 300:
                to_remove.append(phone)
        
        for phone in to_remove:
            del self.waiting_codes[phone]
            logger.info(f"🧹 Очищена ожидающая сессия для {phone}")
        
        # Очистка ожидающих 2FA
        to_remove_2fa = []
        for phone, info in self.waiting_2fa.items():
            if current_time - info['timestamp'] > 300:
                to_remove_2fa.append(phone)
        
        for phone in to_remove_2fa:
            del self.waiting_2fa[phone]
            logger.info(f"🧹 Очищена ожидающая 2FA для {phone}")

# Инициализация менеджера сессий
session_manager = SessionManager()

# ================= СОСТОЯНИЯ FSM =================

class BuyStates(StatesGroup):
    waiting_for_username = State()
    waiting_for_code = State()
    waiting_for_2fa = State()

class AddAccountStates(StatesGroup):
    waiting_for_phone = State()
    waiting_for_code = State()
    waiting_for_2fa = State()

class TopUpStates(StatesGroup):
    waiting_for_amount = State()
    waiting_for_payment_method = State()
    waiting_for_stars_amount = State()

class AdminStates(StatesGroup):
    waiting_for_user_id = State()
    waiting_for_stars_amount = State()
    waiting_for_number_phone = State()
    waiting_for_number_country = State()
    waiting_for_number_desc = State()
    waiting_for_number_price = State()

# ================= КЛАВИАТУРЫ =================

def get_main_keyboard(user_id: int = None):
    """Главная клавиатура"""
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("📱 Доступные номера", callback_data="numbers_page_1"),
        InlineKeyboardButton("👤 Мой профиль", callback_data="profile"),
    )
    
    user = db.get_user(user_id) if user_id else None
    if user_id in ADMIN_IDS or (user and user.get('is_admin')):
        keyboard.add(InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin"))
    
    return keyboard

def get_profile_keyboard():
    """Клавиатура профиля с пополнением"""
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("⭐️ Пополнить звёздами", callback_data="topup_stars"),
        InlineKeyboardButton("💳 ЮMoney", callback_data="topup_yoomoney"),
        InlineKeyboardButton("₿ Crypto Bot", callback_data="topup_cryptobot"),
        InlineKeyboardButton("📊 История", callback_data="transactions"),
        InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")
    )
    return keyboard

def get_numbers_keyboard(page: int, total_pages: int):
    """Клавиатура для списка номеров с пагинацией"""
    keyboard = InlineKeyboardMarkup(row_width=3)
    
    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton("◀️", callback_data=f"numbers_page_{page-1}"))
    
    nav_buttons.append(InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data="current_page"))
    
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton("▶️", callback_data=f"numbers_page_{page+1}"))
    
    keyboard.row(*nav_buttons)
    
    keyboard.row(
        InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu"),
        InlineKeyboardButton("🔄 Обновить", callback_data=f"numbers_page_{page}")
    )
    
    return keyboard

def get_payment_keyboard(number_id: int, price_rub: float):
    """Клавиатура для выбора способа оплаты"""
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("💳 ЮMoney", callback_data=f"pay_yoomoney_{number_id}"),
        InlineKeyboardButton("₿ Crypto Bot", callback_data=f"pay_cryptobot_{number_id}"),
    )
    keyboard.row(
        InlineKeyboardButton("❌ Отмена", callback_data="numbers_page_1")
    )
    return keyboard

def get_topup_keyboard():
    """Клавиатура для выбора суммы пополнения"""
    keyboard = InlineKeyboardMarkup(row_width=3)
    keyboard.add(
        InlineKeyboardButton("100 ⭐️", callback_data="topup_amount_100"),
        InlineKeyboardButton("500 ⭐️", callback_data="topup_amount_500"),
        InlineKeyboardButton("1000 ⭐️", callback_data="topup_amount_1000"),
        InlineKeyboardButton("5000 ⭐️", callback_data="topup_amount_5000"),
        InlineKeyboardButton("10000 ⭐️", callback_data="topup_amount_10000"),
        InlineKeyboardButton("✏️ Другая", callback_data="topup_amount_custom"),
    )
    keyboard.row(
        InlineKeyboardButton("◀️ Назад", callback_data="profile")
    )
    return keyboard

def get_admin_keyboard():
    """Клавиатура админ-панели"""
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("➕ Добавить номер", callback_data="admin_add_number"),
        InlineKeyboardButton("📋 Все номера", callback_data="admin_numbers"),
        InlineKeyboardButton("📱 Аккаунты TG", callback_data="admin_accounts"),
        InlineKeyboardButton("➕ Добавить аккаунт", callback_data="admin_add_account"),
        InlineKeyboardButton("👥 Пользователи", callback_data="admin_users"),
        InlineKeyboardButton("📊 Статистика", callback_data="admin_stats"),
        InlineKeyboardButton("🎁 Выдать звёзды", callback_data="admin_add_stars"),
        InlineKeyboardButton("🔄 Перезапуск", callback_data="admin_restart"),
        InlineKeyboardButton("◀️ Назад", callback_data="main_menu")
    )
    return keyboard

def get_accounts_keyboard(accounts: List[Dict], page: int = 1):
    """Клавиатура для списка аккаунтов"""
    keyboard = InlineKeyboardMarkup(row_width=1)
    
    for acc in accounts[:5]:
        status_emoji = "✅" if acc['status'] == 'active' else "⏳" if acc['status'] == 'pending' else "❌"
        owner_mark = "👑" if acc.get('owner_checked') and acc.get('owner_id') else ""
        fa_mark = "🔐" if acc.get('has_2fa') else ""
        keyboard.add(InlineKeyboardButton(
            f"{status_emoji}{owner_mark}{fa_mark} {acc['phone']} | {acc.get('first_name', 'Нет имени')}",
            callback_data=f"account_{acc['phone']}"
        ))
    
    keyboard.add(InlineKeyboardButton("◀️ Назад", callback_data="admin"))
    return keyboard

def get_back_keyboard(callback_data: str = "main_menu"):
    """Клавиатура с кнопкой назад"""
    keyboard = InlineKeyboardMarkup().add(
        InlineKeyboardButton("◀️ Назад", callback_data=callback_data)
    )
    return keyboard

# ================= ПЛАТЁЖНЫЕ СИСТЕМЫ =================

class YooMoneyPayment:
    @staticmethod
    async def create_payment(amount: float, payment_id: str, description: str) -> Optional[str]:
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
            logger.info(f"✅ Создан платеж ЮMoney: {payment_id} на сумму {amount} руб")
            return payment_url
        except Exception as e:
            logger.error(f"❌ Ошибка создания платежа ЮMoney: {e}")
            return None

class CryptoBotPayment:
    @staticmethod
    async def create_payment(amount: float, payment_id: str, description: str) -> Optional[str]:
        try:
            headers = {
                'Crypto-Pay-API-Token': CRYPTOBOT_TOKEN,
                'Content-Type': 'application/json'
            }
            
            data = {
                'asset': 'USDT',
                'amount': str(amount),
                'description': description,
                'payload': payment_id,
                'callback_url': f"{BASE_URL}/api/cryptobot/webhook"
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    "https://pay.crypt.bot/api/createInvoice",
                    headers=headers,
                    json=data
                ) as response:
                    if response.status == 200:
                        result = await response.json()
                        if result.get('ok'):
                            logger.info(f"✅ Создан платеж Crypto Bot: {payment_id} на сумму {amount} USDT")
                            return result['result']['pay_url']
            return None
        except Exception as e:
            logger.error(f"❌ Ошибка создания платежа Crypto Bot: {e}")
            return None

class StarsPayment:
    @staticmethod
    async def create_payment(user_id: int, amount: int) -> str:
        """Создание платежа звёздами (мгновенное начисление)"""
        payment_id = str(uuid.uuid4())
        
        success = db.add_stars(user_id, amount, "stars", payment_id)
        
        if success:
            logger.info(f"✅ Пользователь {user_id} пополнил {amount}⭐ звёздами")
            return payment_id
        return None

# ================= ОБРАБОТЧИКИ КОМАНД =================

@dp.message_handler(commands=['start'])
async def cmd_start(message: Message):
    """Обработчик команды /start"""
    global last_message_time
    last_message_time = time.time()
    
    user_id = message.from_user.id
    
    try:
        me = await bot.get_me()
        logger.info(f"✅ Бот авторизован: @{me.username}")
    except Unauthorized:
        logger.error("❌ НЕДЕЙСТВИТЕЛЬНЫЙ ТОКЕН!")
        await message.reply("❌ Ошибка авторизации бота. Свяжитесь с администратором.")
        return
    
    user = db.get_user(user_id)
    if not user:
        db.create_user(
            user_id=user_id,
            username=message.from_user.username or f"user_{user_id}",
            first_name=message.from_user.first_name or "Пользователь"
        )
        logger.info(f"✅ Новый пользователь: {user_id}")
    
    db.update_user_activity(user_id)
    
    await message.reply(
        "👋 <b>Добро пожаловать в магазин номеров Telegram!</b>\n\n"
        "📱 Здесь вы можете купить виртуальные номера для Telegram.\n\n"
        "🔹 Пополняйте баланс звёздами\n"
        "🔹 Покупайте номера\n"
        "🔹 Получайте коды подтверждения\n\n"
        "Выберите действие:",
        reply_markup=get_main_keyboard(user_id)
    )

@dp.message_handler()
async def track_all_messages(message: Message):
    """Отслеживание всех сообщений"""
    global last_message_time
    last_message_time = time.time()

@dp.callback_query_handler(lambda c: c.data == 'main_menu')
async def main_menu(callback: CallbackQuery):
    """Возврат в главное меню"""
    await callback.answer()
    user_id = callback.from_user.id
    db.update_user_activity(user_id)
    
    await callback.message.edit_text(
        "🏠 <b>Главное меню</b>\n\nВыберите действие:",
        reply_markup=get_main_keyboard(user_id)
    )

@dp.callback_query_handler(lambda c: c.data == 'profile')
async def show_profile(callback: CallbackQuery):
    """Показать профиль пользователя"""
    await callback.answer()
    user_id = callback.from_user.id
    user = db.get_user(user_id)
    db.update_user_activity(user_id)
    
    if not user:
        await callback.message.edit_text("❌ Ошибка загрузки профиля")
        return
    
    purchases = 0
    try:
        if db.db_url:
            with db.get_cursor() as cursor:
                cursor.execute('SELECT COUNT(*) as count FROM transactions WHERE user_id = %s AND status = %s', 
                              (user_id, 'completed'))
                purchases = cursor.fetchone()['count'] or 0
        else:
            with db.get_cursor() as cursor:
                cursor.execute('SELECT COUNT(*) as count FROM transactions WHERE user_id = ? AND status = "completed"', 
                              (user_id,))
                purchases = cursor.fetchone()['count'] or 0
    except:
        pass
    
    text = f"""
👤 <b>Ваш профиль</b>

🆔 <b>ID:</b> <code>{user_id}</code>
👤 <b>Имя:</b> {user['first_name']}
📝 <b>Username:</b> @{user['username']}

💰 <b>Баланс:</b>
• ⭐️ Звёзды: {user['stars_balance']}
• 💵 Рубли: {user['stars_balance'] * STAR_TO_RUB:.2f}₽

📊 <b>Статистика:</b>
• 📱 Куплено номеров: {purchases}
• 📅 Зарегистрирован: {datetime.fromtimestamp(user['registered_at']).strftime('%d.%m.%Y')}

Выберите способ пополнения:
"""
    
    await callback.message.edit_text(text, reply_markup=get_profile_keyboard())

# ================= ОБРАБОТЧИКИ ПОПОЛНЕНИЯ =================

@dp.callback_query_handler(lambda c: c.data == 'topup_stars')
async def topup_stars(callback: CallbackQuery, state: FSMContext):
    """Пополнение звёздами"""
    await callback.answer()
    user_id = callback.from_user.id
    
    await callback.message.edit_text(
        "⭐️ <b>Пополнение звёздами</b>\n\n"
        "Введите количество звёзд для пополнения (целое число):",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("◀️ Назад", callback_data="profile")
        )
    )
    
    await TopUpStates.waiting_for_stars_amount.set()

@dp.message_handler(state=TopUpStates.waiting_for_stars_amount)
async def process_stars_amount(message: Message, state: FSMContext):
    """Обработка количества звёзд"""
    try:
        amount = int(message.text.strip())
        if amount < 1:
            raise ValueError
        if amount > 1000000:
            await message.reply("❌ Максимальная сумма пополнения - 1 000 000 ⭐️")
            return
    except ValueError:
        await message.reply("❌ Введите целое положительное число")
        return
    
    user_id = message.from_user.id
    
    # Создаем платеж звёздами
    payment_id = await StarsPayment.create_payment(user_id, amount)
    
    if payment_id:
        await message.reply(
            f"✅ <b>Пополнение успешно!</b>\n\n"
            f"➕ Добавлено: {amount} ⭐️\n"
            f"💰 Новый баланс: {db.get_user(user_id)['stars_balance']} ⭐️",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("👤 Профиль", callback_data="profile"),
                InlineKeyboardButton("📱 Номера", callback_data="numbers_page_1")
            )
        )
    else:
        await message.reply(
            "❌ Ошибка при пополнении. Попробуйте позже.",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("◀️ Назад", callback_data="profile")
            )
        )
    
    await state.finish()

@dp.callback_query_handler(lambda c: c.data == 'topup_yoomoney')
async def topup_yoomoney(callback: CallbackQuery, state: FSMContext):
    """Пополнение через ЮMoney"""
    await callback.answer()
    
    await callback.message.edit_text(
        "💳 <b>Пополнение через ЮMoney</b>\n\n"
        f"Минимальная сумма: {MIN_TOPUP_AMOUNT} ₽\n"
        f"Максимальная сумма: {MAX_TOPUP_AMOUNT} ₽\n\n"
        f"Курс: 1 ⭐️ = {STAR_TO_RUB} ₽\n\n"
        f"Введите сумму в рублях:",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("◀️ Назад", callback_data="profile")
        )
    )
    
    await TopUpStates.waiting_for_amount.set()
    await state.update_data(payment_method='yoomoney')

@dp.callback_query_handler(lambda c: c.data == 'topup_cryptobot')
async def topup_cryptobot(callback: CallbackQuery, state: FSMContext):
    """Пополнение через Crypto Bot"""
    await callback.answer()
    
    await callback.message.edit_text(
        "₿ <b>Пополнение через Crypto Bot</b>\n\n"
        f"Минимальная сумма: {MIN_TOPUP_AMOUNT} USDT\n"
        f"Максимальная сумма: {MAX_TOPUP_AMOUNT} USDT\n\n"
        f"Курс: 1 ⭐️ = {STAR_TO_RUB} USDT\n\n"
        f"Введите сумму в USDT:",
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("◀️ Назад", callback_data="profile")
        )
    )
    
    await TopUpStates.waiting_for_amount.set()
    await state.update_data(payment_method='cryptobot')

@dp.message_handler(state=TopUpStates.waiting_for_amount)
async def process_topup_amount(message: Message, state: FSMContext):
    """Обработка суммы пополнения"""
    try:
        amount = float(message.text.strip())
        if amount < MIN_TOPUP_AMOUNT:
            await message.reply(f"❌ Минимальная сумма: {MIN_TOPUP_AMOUNT}")
            return
        if amount > MAX_TOPUP_AMOUNT:
            await message.reply(f"❌ Максимальная сумма: {MAX_TOPUP_AMOUNT}")
            return
    except ValueError:
        await message.reply("❌ Введите число")
        return
    
    data = await state.get_data()
    method = data.get('payment_method')
    user_id = message.from_user.id
    
    topup = db.create_topup(user_id, amount, method)
    
    if not topup:
        await message.reply("❌ Ошибка создания пополнения")
        await state.finish()
        return
    
    if method == 'yoomoney':
        payment_url = await YooMoneyPayment.create_payment(
            amount=amount,
            payment_id=topup['payment_id'],
            description=f"Пополнение баланса пользователя {user_id}"
        )
        
        if payment_url:
            await message.reply(
                f"💳 <b>Оплата через ЮMoney</b>\n\n"
                f"💰 Сумма: {amount} ₽\n"
                f"⭐️ Вы получите: {topup['stars_amount']} звёзд\n\n"
                f"1. Нажмите кнопку «💳 Оплатить»\n"
                f"2. Оплатите в ЮMoney\n"
                f"3. Нажмите «✅ Я оплатил»\n\n"
                f"После подтверждения звёзды будут зачислены!",
                reply_markup=InlineKeyboardMarkup().add(
                    InlineKeyboardButton("💳 Оплатить", url=payment_url),
                    InlineKeyboardButton("✅ Я оплатил", callback_data=f"check_topup_{topup['payment_id']}"),
                    InlineKeyboardButton("◀️ Назад", callback_data="profile")
                )
            )
        else:
            await message.reply("❌ Ошибка создания платежа")
    
    elif method == 'cryptobot':
        payment_url = await CryptoBotPayment.create_payment(
            amount=amount,
            payment_id=topup['payment_id'],
            description=f"Пополнение баланса пользователя {user_id}"
        )
        
        if payment_url:
            await message.reply(
                f"₿ <b>Оплата через Crypto Bot</b>\n\n"
                f"💰 Сумма: {amount} USDT\n"
                f"⭐️ Вы получите: {topup['stars_amount']} звёзд\n\n"
                f"1. Нажмите кнопку «₿ Оплатить»\n"
                f"2. Оплатите в Crypto Bot\n"
                f"3. Нажмите «✅ Я оплатил»\n\n"
                f"После подтверждения звёзды будут зачислены!",
                reply_markup=InlineKeyboardMarkup().add(
                    InlineKeyboardButton("₿ Оплатить", url=payment_url),
                    InlineKeyboardButton("✅ Я оплатил", callback_data=f"check_topup_{topup['payment_id']}"),
                    InlineKeyboardButton("◀️ Назад", callback_data="profile")
                )
            )
        else:
            await message.reply("❌ Ошибка создания платежа")
    
    await state.finish()

@dp.callback_query_handler(lambda c: c.data.startswith('check_topup_'))
async def check_topup(callback: CallbackQuery):
    """Проверка статуса пополнения"""
    await callback.answer()
    
    payment_id = callback.data.replace('check_topup_', '')
    
    success = db.complete_topup(payment_id)
    
    if success:
        topup = db.get_topup(payment_id)
        user = db.get_user(topup['user_id'])
        
        await callback.message.edit_text(
            f"✅ <b>Пополнение успешно!</b>\n\n"
            f"💰 Зачислено: {topup['stars_amount']} ⭐️\n"
            f"💎 Новый баланс: {user['stars_balance']} ⭐️",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("👤 Профиль", callback_data="profile"),
                InlineKeyboardButton("📱 Номера", callback_data="numbers_page_1")
            )
        )
    else:
        await callback.message.edit_text(
            "❌ Платёж не найден или уже обработан",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("◀️ Назад", callback_data="profile")
            )
        )

# ================= РАЗДЕЛ ПОКУПКИ НОМЕРОВ =================

@dp.callback_query_handler(lambda c: c.data.startswith('numbers_page_'))
async def show_numbers(callback: CallbackQuery):
    """Показать список доступных номеров с пагинацией"""
    await callback.answer()
    db.update_user_activity(callback.from_user.id)
    
    try:
        page = int(callback.data.split('_')[2])
    except:
        page = 1
    
    numbers, total = db.get_available_numbers(page=page, limit=5)
    total_pages = max(1, (total + 4) // 5)
    
    if not numbers:
        await callback.message.edit_text(
            "📱 <b>Нет доступных номеров</b>\n\n"
            "Номера появятся позже. Следите за обновлениями!",
            reply_markup=get_back_keyboard("main_menu")
        )
        return
    
    text = f"📱 <b>Доступные номера</b> (стр. {page}/{total_pages})\n\n"
    
    for num in numbers:
        flag = "🇷🇺" if num['country'].lower() in ['россия', 'russia'] else "🌍"
        
        text += f"{flag} <b>{num['country']}</b>\n"
        text += f"📞 <code>{num['phone_number']}</code>\n"
        text += f"📝 {num['description']}\n"
        text += f"💰 <b>{num['price_stars']} ⭐️</b> ({num['price_rub']:.0f}₽)\n"
        text += f"🔹 <b>ID:</b> {num['id']}\n\n"
    
    text += "Для покупки нажмите /buy_ ID (например: /buy_1)"
    
    keyboard = get_numbers_keyboard(page, total_pages)
    await callback.message.edit_text(text, reply_markup=keyboard)

@dp.message_handler(lambda message: message.text and message.text.startswith('/buy_'))
async def buy_number_command(message: Message, state: FSMContext):
    """Обработка команды покупки по ID"""
    try:
        number_id = int(message.text.split('_')[1])
    except:
        await message.reply("❌ Неверный формат. Используйте /buy_1")
        return
    
    user_id = message.from_user.id
    db.update_user_activity(user_id)
    
    number = db.get_number(number_id)
    
    if not number:
        await message.reply("❌ Номер не найден")
        return
    
    if number['status'] != 'available':
        await message.reply("❌ Номер уже недоступен")
        return
    
    user = db.get_user(user_id)
    if not user:
        await message.reply("❌ Сначала запустите бота командой /start")
        return
    
    await state.update_data(number_id=number_id)
    
    text = f"""
✅ <b>Подтверждение покупки</b>

📞 <b>Номер:</b> <code>{number['phone_number']}</code>
🌍 <b>Страна:</b> {number['country']}
📝 <b>Описание:</b> {number['description']}
💰 <b>Цена:</b> {number['price_stars']} ⭐️ ({number['price_rub']:.0f}₽)

💳 <b>Ваш баланс:</b> {user['stars_balance']} ⭐️

Выберите способ оплаты:
"""
    
    await message.reply(text, reply_markup=get_payment_keyboard(number_id, number['price_rub']))

@dp.callback_query_handler(lambda c: c.data.startswith('pay_yoomoney_'))
async def pay_yoomoney(callback: CallbackQuery, state: FSMContext):
    """Оплата через ЮMoney"""
    await callback.answer()
    user_id = callback.from_user.id
    db.update_user_activity(user_id)
    
    number_id = int(callback.data.split('_')[2])
    number = db.get_number(number_id)
    
    if not number or number['status'] != 'available':
        await callback.message.edit_text("❌ Номер уже недоступен")
        return
    
    payment_id = str(uuid.uuid4())
    payment_url = await YooMoneyPayment.create_payment(
        amount=number['price_rub'],
        payment_id=payment_id,
        description=f"Покупка номера {number['phone_number']}"
    )
    
    if payment_url:
        if db.db_url:
            with db.get_cursor() as cursor:
                cursor.execute('''
                    INSERT INTO payments (id, user_id, number_id, amount_rub, stars_amount, payment_system, created_at, payment_url)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ''', (payment_id, user_id, number_id, number['price_rub'], number['price_stars'], 
                      'yoomoney', time.time(), payment_url))
        else:
            with db.get_cursor() as cursor:
                cursor.execute('''
                    INSERT INTO payments (id, user_id, number_id, amount_rub, stars_amount, payment_system, created_at, payment_url)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (payment_id, user_id, number_id, number['price_rub'], number['price_stars'], 
                      'yoomoney', time.time(), payment_url))
        
        logger.info(f"✅ Создан платеж {payment_id} для пользователя {user_id}")
        
        await callback.message.edit_text(
            f"💳 <b>Оплата через ЮMoney</b>\n\n"
            f"💰 Сумма: {number['price_rub']}₽\n"
            f"📞 Номер: {number['phone_number']}\n\n"
            f"1. Нажмите кнопку «💳 Оплатить»\n"
            f"2. Оплатите в ЮMoney\n"
            f"3. Нажмите «✅ Я оплатил»\n\n"
            f"После подтверждения вы получите код!",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("💳 Оплатить", url=payment_url),
                InlineKeyboardButton("✅ Я оплатил", callback_data=f"check_payment_{payment_id}"),
                InlineKeyboardButton("❌ Отмена", callback_data="numbers_page_1")
            )
        )
    else:
        await callback.message.edit_text(
            "❌ Ошибка создания платежа. Попробуйте позже.",
            reply_markup=get_back_keyboard("numbers_page_1")
        )

@dp.callback_query_handler(lambda c: c.data.startswith('pay_cryptobot_'))
async def pay_cryptobot(callback: CallbackQuery, state: FSMContext):
    """Оплата через Crypto Bot"""
    await callback.answer()
    user_id = callback.from_user.id
    db.update_user_activity(user_id)
    
    number_id = int(callback.data.split('_')[2])
    number = db.get_number(number_id)
    
    if not number or number['status'] != 'available':
        await callback.message.edit_text("❌ Номер уже недоступен")
        return
    
    payment_id = str(uuid.uuid4())
    payment_url = await CryptoBotPayment.create_payment(
        amount=number['price_rub'],
        payment_id=payment_id,
        description=f"Покупка номера {number['phone_number']}"
    )
    
    if payment_url:
        if db.db_url:
            with db.get_cursor() as cursor:
                cursor.execute('''
                    INSERT INTO payments (id, user_id, number_id, amount_rub, stars_amount, payment_system, created_at, payment_url)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ''', (payment_id, user_id, number_id, number['price_rub'], number['price_stars'], 
                      'cryptobot', time.time(), payment_url))
        else:
            with db.get_cursor() as cursor:
                cursor.execute('''
                    INSERT INTO payments (id, user_id, number_id, amount_rub, stars_amount, payment_system, created_at, payment_url)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (payment_id, user_id, number_id, number['price_rub'], number['price_stars'], 
                      'cryptobot', time.time(), payment_url))
        
        logger.info(f"✅ Создан платеж {payment_id} для пользователя {user_id}")
        
        await callback.message.edit_text(
            f"₿ <b>Оплата через Crypto Bot</b>\n\n"
            f"💰 Сумма: {number['price_rub']} USDT\n"
            f"📞 Номер: {number['phone_number']}\n\n"
            f"1. Нажмите кнопку «₿ Оплатить»\n"
            f"2. Оплатите в Crypto Bot\n"
            f"3. Нажмите «✅ Я оплатил»\n\n"
            f"После подтверждения вы получите код!",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("₿ Оплатить", url=payment_url),
                InlineKeyboardButton("✅ Я оплатил", callback_data=f"check_payment_{payment_id}"),
                InlineKeyboardButton("❌ Отмена", callback_data="numbers_page_1")
            )
        )
    else:
        await callback.message.edit_text(
            "❌ Ошибка создания платежа. Попробуйте позже.",
            reply_markup=get_back_keyboard("numbers_page_1")
        )

@dp.callback_query_handler(lambda c: c.data.startswith('check_payment_'))
async def check_payment(callback: CallbackQuery, state: FSMContext):
    """Проверка статуса платежа"""
    await callback.answer()
    user_id = callback.from_user.id
    db.update_user_activity(user_id)
    
    payment_id = callback.data.replace('check_payment_', '')
    
    payment = None
    if db.db_url:
        with db.get_cursor() as cursor:
            cursor.execute('SELECT * FROM payments WHERE id = %s', (payment_id,))
            payment = cursor.fetchone()
    else:
        with db.get_cursor() as cursor:
            cursor.execute('SELECT * FROM payments WHERE id = ?', (payment_id,))
            payment = cursor.fetchone()
    
    if not payment:
        await callback.message.edit_text("❌ Платёж не найден")
        return
    
    payment = dict(payment)
    
    if payment['status'] == 'completed':
        await callback.message.edit_text("✅ Платёж уже обработан!")
        return
    
    if db.db_url:
        with db.get_cursor() as cursor:
            cursor.execute('''
                UPDATE payments SET status = 'completed', completed_at = %s WHERE id = %s
            ''', (time.time(), payment_id))
            
            cursor.execute('''
                UPDATE users SET stars_balance = stars_balance + %s WHERE user_id = %s
            ''', (payment['stars_amount'], payment['user_id']))
            
            cursor.execute('''
                UPDATE transactions SET status = 'completed', completed_at = %s 
                WHERE user_id = %s AND number_id = %s
            ''', (time.time(), payment['user_id'], payment['number_id']))
            
            cursor.execute('SELECT stars_balance FROM users WHERE user_id = %s', (payment['user_id'],))
            row = cursor.fetchone()
            new_balance = row['stars_balance'] if row else 0
    else:
        with db.get_cursor() as cursor:
            cursor.execute('''
                UPDATE payments SET status = 'completed', completed_at = ? WHERE id = ?
            ''', (time.time(), payment_id))
            
            cursor.execute('''
                UPDATE users SET stars_balance = stars_balance + ? WHERE user_id = ?
            ''', (payment['stars_amount'], payment['user_id']))
            
            cursor.execute('''
                UPDATE transactions SET status = 'completed', completed_at = ? 
                WHERE user_id = ? AND number_id = ?
            ''', (time.time(), payment['user_id'], payment['number_id']))
            
            cursor.execute('SELECT stars_balance FROM users WHERE user_id = ?', (payment['user_id'],))
            row = cursor.fetchone()
            new_balance = row['stars_balance'] if row else 0
    
    logger.info(f"✅ Платеж {payment_id} завершен, пользователь {payment['user_id']} получил {payment['stars_amount']}⭐")
    
    account = db.get_available_tg_account()
    if account:
        success = await session_manager.request_code(
            account['phone'], 
            payment['number_id'], 
            payment['user_id']
        )
        
        if success:
            await state.update_data(
                phone=account['phone'],
                number_id=payment['number_id'],
                payment_id=payment_id
            )
            
            await callback.message.edit_text(
                f"✅ <b>Оплата успешна!</b>\n\n"
                f"💰 На ваш баланс зачислено: {payment['stars_amount']} ⭐️\n"
                f"💎 Новый баланс: {new_balance} ⭐️\n\n"
                f"📲 На номер {account['phone']} отправлен код подтверждения.\n"
                f"✏️ Введите код из Telegram:",
                reply_markup=InlineKeyboardMarkup().add(
                    InlineKeyboardButton("❌ Отмена", callback_data="main_menu")
                )
            )
            await BuyStates.waiting_for_code.set()
        else:
            await callback.message.edit_text(
                f"✅ <b>Оплата успешна!</b>\n\n"
                f"💰 Новый баланс: {new_balance} ⭐️\n\n"
                f"❌ Но возникла ошибка при получении кода. Обратитесь к администратору.",
                reply_markup=InlineKeyboardMarkup().add(
                    InlineKeyboardButton("👤 Профиль", callback_data="profile")
                )
            )
    else:
        await callback.message.edit_text(
            f"✅ <b>Оплата успешна!</b>\n\n"
            f"💰 Новый баланс: {new_balance} ⭐️\n\n"
            f"❌ Нет доступных аккаунтов для отправки кода. Обратитесь к администратору.",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("👤 Профиль", callback_data="profile")
            )
        )

@dp.message_handler(state=BuyStates.waiting_for_code)
async def process_code(message: Message, state: FSMContext):
    """Обработка введенного кода"""
    code = message.text.strip()
    user_id = message.from_user.id
    db.update_user_activity(user_id)
    
    data = await state.get_data()
    phone = data.get('phone')
    number_id = data.get('number_id')
    payment_id = data.get('payment_id')
    
    if not phone or not number_id:
        await message.reply("❌ Ошибка: нет информации о покупке. Начните заново.")
        await state.finish()
        return
    
    result = await session_manager.submit_code(phone, code)
    
    if result and 'code' in result:
        number = db.get_number(number_id)
        db.delete_sold_number(number_id)
        
        await message.reply(
            f"✅ <b>Номер успешно получен!</b>\n\n"
            f"📞 <b>Номер:</b> <code>{number['phone_number']}</code>\n"
            f"🔑 <b>Код:</b> <code>{result['code']}</code>\n\n"
            f"📝 <b>Инструкция:</b>\n"
            f"1. Откройте Telegram\n"
            f"2. Введите номер {number['phone_number']}\n"
            f"3. Введите код {result['code']}\n"
            f"4. Готово!\n\n"
            f"⏱ Код действителен 1 час.\n\n"
            f"🔐 Аккаунт теперь ваш! Сессия будет жить вечно.",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("📱 Купить ещё", callback_data="numbers_page_1"),
                InlineKeyboardButton("👤 Профиль", callback_data="profile")
            )
        )
        logger.info(f"✅ Пользователь {user_id} получил код для номера {number['phone_number']}")
        
        for admin_id in ADMIN_IDS:
            try:
                await bot.send_message(
                    admin_id,
                    f"💰 <b>Продажа!</b>\n\n"
                    f"👤 Покупатель: {user_id}\n"
                    f"📞 Номер: {number['phone_number']}\n"
                    f"💰 Цена: {number['price_stars']}⭐\n"
                    f"🔑 Код: {result['code']}"
                )
            except:
                pass
        
        await state.finish()
    elif result and result.get('error') == '2fa_required':
        await state.update_data(phone=phone, number_id=number_id)
        await message.reply(
            "🔐 <b>Требуется двухфакторная аутентификация</b>\n\n"
            "Введите пароль 2FA:",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("❌ Отмена", callback_data="main_menu")
            )
        )
        await BuyStates.waiting_for_2fa.set()
    elif result and result.get('error') == 'invalid_code':
        await message.reply("❌ Неверный код. Попробуйте ещё раз:")
    else:
        await message.reply(
            "❌ Ошибка получения кода. Обратитесь к администратору.",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("👤 Профиль", callback_data="profile")
            )
        )
        await state.finish()

@dp.message_handler(state=BuyStates.waiting_for_2fa)
async def process_2fa(message: Message, state: FSMContext):
    """Обработка 2FA"""
    password = message.text.strip()
    
    data = await state.get_data()
    phone = data.get('phone')
    number_id = data.get('number_id')
    
    if not phone or not number_id:
        await message.reply("❌ Ошибка. Начните заново.")
        await state.finish()
        return
    
    result = await session_manager.submit_2fa(phone, password)
    
    if result and 'code' in result:
        number = db.get_number(number_id)
        db.delete_sold_number(number_id)
        
        await message.reply(
            f"✅ <b>Номер успешно получен!</b>\n\n"
            f"📞 <b>Номер:</b> <code>{number['phone_number']}</code>\n"
            f"🔑 <b>Код:</b> <code>{result['code']}</code>\n\n"
            f"📝 <b>Инструкция:</b>\n"
            f"1. Откройте Telegram\n"
            f"2. Введите номер {number['phone_number']}\n"
            f"3. Введите код {result['code']}\n"
            f"4. Готово!\n\n"
            f"⏱ Код действителен 1 час.\n\n"
            f"🔐 Аккаунт теперь ваш! Сессия будет жить вечно.",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("📱 Купить ещё", callback_data="numbers_page_1"),
                InlineKeyboardButton("👤 Профиль", callback_data="profile")
            )
        )
        logger.info(f"✅ Пользователь {message.from_user.id} получил код с 2FA")
        await state.finish()
    elif result and result.get('error') == 'invalid_password':
        await message.reply("❌ Неверный пароль 2FA. Попробуйте ещё раз:")
    else:
        await message.reply(
            "❌ Ошибка 2FA. Обратитесь к администратору.",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("👤 Профиль", callback_data="profile")
            )
        )
        await state.finish()

# ================= АДМИН-ПАНЕЛЬ =================

@dp.callback_query_handler(lambda c: c.data == 'admin')
async def admin_panel(callback: CallbackQuery):
    """Открыть админ-панель"""
    await callback.answer()
    
    if callback.from_user.id not in ADMIN_IDS:
        await callback.message.edit_text("⛔ У вас нет доступа к админ-панели")
        return
    
    stats = db.get_stats()
    
    cpu_percent = psutil.cpu_percent(interval=1)
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    uptime = time.time() - start_time
    
    text = f"""
⚙️ <b>Админ-панель</b>

👤 <b>Администратор:</b> @{callback.from_user.username}

📊 <b>Статистика магазина:</b>
• 👥 Пользователей: {stats['total_users']}
• 📱 Номеров в продаже: {stats['available_numbers']}
• ✅ Продано номеров: {stats['sold_numbers']}
• ⏳ В обработке: {stats['pending_numbers']}
• 🤖 Аккаунтов TG: {stats['active_accounts']}/{stats['total_accounts']}
• 💰 Продано звёзд: {stats['total_stars_sold']} ⭐️
• 💵 Выручка: {stats['total_revenue_rub']:.2f}₽

🖥 <b>Система:</b>
• 🔥 CPU: {cpu_percent}%
• 💾 RAM: {memory.percent}%
• 💽 Диск: {disk.percent}%
• ⏱ Uptime: {timedelta(seconds=int(uptime))}
• 🔄 Автоперезапуск: ✅
• 💾 Сессии сохраняются: ✅

Выберите действие:
"""
    
    await callback.message.edit_text(text, reply_markup=get_admin_keyboard())

@dp.callback_query_handler(lambda c: c.data == 'admin_restart')
async def admin_restart(callback: CallbackQuery):
    """Принудительный перезапуск бота"""
    await callback.answer()
    
    if callback.from_user.id not in ADMIN_IDS:
        return
    
    await callback.message.edit_text(
        "🔄 <b>Перезапуск бота...</b>\n\n"
        "Бот будет перезапущен через 3 секунды."
    )
    
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                "🔄 Администратор инициировал перезапуск бота. Ожидайте 10 секунд..."
            )
        except:
            pass
    
    await asyncio.sleep(3)
    restart_bot()

@dp.callback_query_handler(lambda c: c.data == 'admin_accounts')
async def admin_accounts(callback: CallbackQuery):
    """Список всех аккаунтов"""
    await callback.answer()
    
    accounts = db.get_all_tg_accounts()
    
    if not accounts:
        await callback.message.edit_text(
            "📱 <b>Нет добавленных аккаунтов</b>",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("➕ Добавить аккаунт", callback_data="admin_add_account"),
                InlineKeyboardButton("◀️ Назад", callback_data="admin")
            )
        )
        return
    
    text = "📱 <b>Аккаунты Telegram:</b>\n\n"
    for acc in accounts[:10]:
        status_emoji = "✅" if acc['status'] == 'active' else "⏳" if acc['status'] == 'pending' else "❌"
        owner_mark = "👑" if acc.get('owner_checked') and acc.get('owner_id') else ""
        fa_mark = "🔐" if acc.get('has_2fa') else ""
        text += f"{status_emoji}{owner_mark}{fa_mark} <b>{acc['phone']}</b>\n"
        text += f"   👤 Имя: {acc.get('first_name', 'Нет имени')}\n"
        text += f"   📊 Статус: {acc['status']}\n"
        if acc.get('owner_id'):
            text += f"   👑 Владелец: {acc['owner_id']}\n"
        if acc.get('last_code'):
            text += f"   🔑 Последний код: {acc['last_code']}\n"
        text += f"   📅 Добавлен: {datetime.fromtimestamp(acc['added_at']).strftime('%d.%m.%Y')}\n"
        # Проверяем наличие файла сессии
        session_path = os.path.join(SESSIONS_DIR, acc['session_name'])
        if os.path.exists(f"{session_path}.session"):
            text += f"   💾 Файл сессии: ✅\n"
        else:
            text += f"   💾 Файл сессии: ❌\n"
        text += "\n"
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("➕ Добавить аккаунт", callback_data="admin_add_account"),
            InlineKeyboardButton("◀️ Назад", callback_data="admin")
        )
    )

@dp.callback_query_handler(lambda c: c.data == 'admin_add_account')
async def admin_add_account(callback: CallbackQuery, state: FSMContext):
    """Добавление нового аккаунта"""
    await callback.answer()
    
    await callback.message.edit_text(
        "📱 <b>Добавление аккаунта Telegram</b>\n\n"
        "Введите номер телефона в международном формате:\n"
        "<code>+79001234567</code>",
        reply_markup=get_back_keyboard("admin")
    )
    
    await AddAccountStates.waiting_for_phone.set()

@dp.message_handler(state=AddAccountStates.waiting_for_phone)
async def add_account_phone(message: Message, state: FSMContext):
    """Обработка номера телефона для нового аккаунта"""
    phone = message.text.strip()
    
    if not phone.startswith('+') or len(phone) < 10:
        await message.reply("❌ Неверный формат. Используйте +79001234567")
        return
    
    success, msg = await session_manager.add_new_account(
        phone=phone,
        api_id=API_ID,
        api_hash=API_HASH,
        added_by=message.from_user.id
    )
    
    if success:
        await state.update_data(phone=phone)
        await message.reply(
            f"✅ {msg}\n\n📲 Введите код из Telegram:",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("❌ Отмена", callback_data="admin")
            )
        )
        await AddAccountStates.waiting_for_code.set()
    else:
        await message.reply(
            f"❌ {msg}",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("◀️ Назад", callback_data="admin")
            )
        )
        await state.finish()

@dp.message_handler(state=AddAccountStates.waiting_for_code)
async def add_account_code(message: Message, state: FSMContext):
    """Подтверждение аккаунта кодом"""
    code = message.text.strip()
    data = await state.get_data()
    phone = data.get('phone')
    
    if not phone:
        await message.reply("❌ Ошибка: номер не найден. Начните заново.")
        await state.finish()
        return
    
    success, msg, user_info = await session_manager.confirm_new_account(phone, code)
    
    if success:
        await message.reply(
            f"✅ <b>Аккаунт успешно добавлен!</b>\n\n"
            f"📱 <b>Номер:</b> {phone}\n"
            f"👤 <b>Имя:</b> {user_info.get('first_name')}\n"
            f"🆔 <b>ID:</b> <code>{user_info.get('id')}</code>\n"
            f"📝 <b>Username:</b> @{user_info.get('username', 'нет')}",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin")
            )
        )
        logger.info(f"✅ Добавлен новый аккаунт: {phone}")
    elif msg == "2FA_REQUIRED":
        await state.update_data(phone=phone)
        await message.reply(
            "🔐 <b>Требуется двухфакторная аутентификация</b>\n\n"
            "Введите пароль 2FA:",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("❌ Отмена", callback_data="admin")
            )
        )
        await AddAccountStates.waiting_for_2fa.set()
    else:
        await message.reply(
            f"❌ {msg}",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("◀️ Назад", callback_data="admin")
            )
        )
        await state.finish()

@dp.message_handler(state=AddAccountStates.waiting_for_2fa)
async def add_account_2fa(message: Message, state: FSMContext):
    """Подтверждение аккаунта с 2FA"""
    password = message.text.strip()
    data = await state.get_data()
    phone = data.get('phone')
    
    if not phone:
        await message.reply("❌ Ошибка. Начните заново.")
        await state.finish()
        return
    
    success, msg, user_info = await session_manager.submit_account_2fa(phone, password)
    
    if success:
        await message.reply(
            f"✅ <b>Аккаунт успешно добавлен с 2FA!</b>\n\n"
            f"📱 <b>Номер:</b> {phone}\n"
            f"👤 <b>Имя:</b> {user_info.get('first_name')}\n"
            f"🆔 <b>ID:</b> <code>{user_info.get('id')}</code>\n"
            f"📝 <b>Username:</b> @{user_info.get('username', 'нет')}",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin")
            )
        )
        logger.info(f"✅ Добавлен новый аккаунт с 2FA: {phone}")
    else:
        await message.reply(
            f"❌ {msg}",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("◀️ Назад", callback_data="admin")
            )
        )
    
    await state.finish()

@dp.callback_query_handler(lambda c: c.data == 'admin_numbers')
async def admin_numbers(callback: CallbackQuery):
    """Список всех номеров"""
    await callback.answer()
    
    numbers = []
    if db.db_url:
        with db.get_cursor() as cursor:
            cursor.execute('SELECT * FROM numbers ORDER BY id DESC LIMIT 20')
            numbers = [dict(row) for row in cursor.fetchall()]
    else:
        with db.get_cursor() as cursor:
            cursor.execute('SELECT * FROM numbers ORDER BY id DESC LIMIT 20')
            numbers = [dict(row) for row in cursor.fetchall()]
    
    if not numbers:
        await callback.message.edit_text(
            "📋 <b>Нет добавленных номеров</b>",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("➕ Добавить номер", callback_data="admin_add_number"),
                InlineKeyboardButton("◀️ Назад", callback_data="admin")
            )
        )
        return
    
    text = "📋 <b>Номера в базе:</b>\n\n"
    for num in numbers:
        status_emoji = "✅" if num['status'] == 'available' else "❌" if num['status'] == 'sold' else "⏳"
        status_text = "В продаже" if num['status'] == 'available' else "Продан" if num['status'] == 'sold' else "Ожидает кода"
        
        text += f"{status_emoji} <b>ID {num['id']}:</b> {num['phone_number']} | {num['country']}\n"
        text += f"   📝 {num['description']}\n"
        text += f"   💰 {num['price_stars']} ⭐️ ({num['price_rub']:.0f}₽) | {status_text}\n"
        
        if num['status'] == 'sold' and num['code']:
            text += f"   🔑 Код: {num['code']}\n"
            text += f"   👤 Покупатель: {num['sold_to']}\n"
        text += "\n"
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("➕ Добавить номер", callback_data="admin_add_number"),
            InlineKeyboardButton("◀️ Назад", callback_data="admin")
        )
    )

@dp.callback_query_handler(lambda c: c.data == 'admin_add_number')
async def admin_add_number_start(callback: CallbackQuery, state: FSMContext):
    """Начало добавления номера"""
    await callback.answer()
    
    await callback.message.edit_text(
        "📞 <b>Добавление номера</b>\n\n"
        "Введите номер телефона в формате +79001234567:",
        reply_markup=get_back_keyboard("admin")
    )
    
    await AdminStates.waiting_for_number_phone.set()

@dp.message_handler(state=AdminStates.waiting_for_number_phone)
async def admin_add_number_phone(message: Message, state: FSMContext):
    """Обработка номера телефона"""
    phone = message.text.strip()
    
    if not phone.startswith('+') or len(phone) < 10:
        await message.reply("❌ Неверный формат. Используйте +79001234567")
        return
    
    await state.update_data(phone=phone)
    
    await message.reply(
        "🌍 Введите страну (например: Россия, Украина, Казахстан):",
        reply_markup=get_back_keyboard("admin")
    )
    
    await AdminStates.waiting_for_number_country.set()

@dp.message_handler(state=AdminStates.waiting_for_number_country)
async def admin_add_number_country(message: Message, state: FSMContext):
    """Обработка страны"""
    country = message.text.strip()
    await state.update_data(country=country)
    
    await message.reply(
        "📝 Введите описание номера (например: Виртуальный номер для Telegram):",
        reply_markup=get_back_keyboard("admin")
    )
    
    await AdminStates.waiting_for_number_desc.set()

@dp.message_handler(state=AdminStates.waiting_for_number_desc)
async def admin_add_number_desc(message: Message, state: FSMContext):
    """Обработка описания"""
    desc = message.text.strip()
    await state.update_data(desc=desc)
    
    await message.reply(
        "💰 Введите цену в звёздах (целое число, например: 100):",
        reply_markup=get_back_keyboard("admin")
    )
    
    await AdminStates.waiting_for_number_price.set()

@dp.message_handler(state=AdminStates.waiting_for_number_price)
async def admin_add_number_price(message: Message, state: FSMContext):
    """Обработка цены и сохранение номера"""
    try:
        price = int(message.text.strip())
        if price <= 0:
            raise ValueError
    except:
        await message.reply("❌ Введите положительное целое число")
        return
    
    data = await state.get_data()
    
    success = db.add_number(
        phone=data['phone'],
        country=data['country'],
        description=data['desc'],
        price_stars=price
    )
    
    if success:
        await message.reply(
            f"✅ <b>Номер успешно добавлен!</b>\n\n"
            f"📞 {data['phone']}\n"
            f"🌍 {data['country']}\n"
            f"📝 {data['desc']}\n"
            f"💰 {price} ⭐️ ({price * STAR_TO_RUB:.0f}₽)",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin"),
                InlineKeyboardButton("➕ Добавить ещё", callback_data="admin_add_number")
            )
        )
        logger.info(f"✅ Добавлен новый номер: {data['phone']} за {price}⭐")
    else:
        await message.reply(
            "❌ Ошибка: возможно, номер уже существует в базе.",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin")
            )
        )
    
    await state.finish()

@dp.callback_query_handler(lambda c: c.data == 'admin_users')
async def admin_users(callback: CallbackQuery):
    """Список всех пользователей"""
    await callback.answer()
    
    users = []
    if db.db_url:
        with db.get_cursor() as cursor:
            cursor.execute('SELECT user_id, username, first_name, stars_balance, is_admin, banned, registered_at FROM users ORDER BY registered_at DESC LIMIT 20')
            users = [dict(row) for row in cursor.fetchall()]
    else:
        with db.get_cursor() as cursor:
            cursor.execute('SELECT user_id, username, first_name, stars_balance, is_admin, banned, registered_at FROM users ORDER BY registered_at DESC LIMIT 20')
            users = [dict(row) for row in cursor.fetchall()]
    
    if not users:
        await callback.message.edit_text("👥 Нет пользователей")
        return
    
    text = "👥 <b>Последние пользователи:</b>\n\n"
    for user in users:
        admin_mark = "👑 " if user['is_admin'] else ""
        banned_mark = "🔨 " if user['banned'] else ""
        date = datetime.fromtimestamp(user['registered_at']).strftime('%d.%m.%Y')
        
        text += f"{admin_mark}{banned_mark}<b>ID {user['user_id']}</b> | @{user['username']}\n"
        text += f"   👤 {user['first_name']} | 💰 {user['stars_balance']}⭐ | 📅 {date}\n\n"
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("◀️ Назад", callback_data="admin")
        )
    )

@dp.callback_query_handler(lambda c: c.data == 'admin_stats')
async def admin_stats(callback: CallbackQuery):
    """Детальная статистика"""
    await callback.answer()
    
    stats = db.get_stats()
    
    completed_transactions = 0
    today_transactions = 0
    total_stars_sold = stats['total_stars_sold']
    avg_price = 0
    
    try:
        if db.db_url:
            with db.get_cursor() as cursor:
                cursor.execute('SELECT COUNT(*) as count FROM transactions WHERE status = %s', ('completed',))
                completed_transactions = cursor.fetchone()['count'] or 0
                
                cursor.execute('SELECT COUNT(*) as count FROM transactions WHERE status = %s AND date(created_at, "unixepoch") = date("now")', ('completed',))
                today_transactions = cursor.fetchone()['count'] or 0
                
                cursor.execute('SELECT AVG(amount_stars) as avg FROM transactions WHERE status = %s', ('completed',))
                row = cursor.fetchone()
                avg_price = row['avg'] or 0
        else:
            with db.get_cursor() as cursor:
                cursor.execute('SELECT COUNT(*) as count FROM transactions WHERE status = "completed"')
                completed_transactions = cursor.fetchone()['count'] or 0
                
                cursor.execute('SELECT COUNT(*) as count FROM transactions WHERE status = "completed" AND date(created_at, "unixepoch") = date("now")')
                today_transactions = cursor.fetchone()['count'] or 0
                
                cursor.execute('SELECT AVG(amount_stars) as avg FROM transactions WHERE status = "completed"')
                row = cursor.fetchone()
                avg_price = row['avg'] or 0
    except:
        pass
    
    text = f"""
📊 <b>Детальная статистика</b>

👥 <b>Пользователи:</b>
• Всего: {stats['total_users']}
• Активных TG аккаунтов: {stats['active_accounts']}

📱 <b>Номера:</b>
• В продаже: {stats['available_numbers']}
• Продано: {stats['sold_numbers']}
• В обработке: {stats['pending_numbers']}
• Всего аккаунтов TG: {stats['total_accounts']}

💰 <b>Продажи:</b>
• Выполнено транзакций: {completed_transactions}
• Сегодня: {today_transactions}
• Средняя цена: {avg_price:.1f} ⭐️
• Всего продано звёзд: {total_stars_sold}
• Выручка: {total_stars_sold * STAR_TO_RUB:.2f}₽

📈 <b>Конверсия:</b>
• Номеров на пользователя: {stats['sold_numbers'] / stats['total_users'] if stats['total_users'] > 0 else 0:.2f}
• Процент активных аккаунтов: {stats['active_accounts'] / stats['total_accounts'] * 100 if stats['total_accounts'] > 0 else 0:.1f}%
"""
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("🔄 Обновить", callback_data="admin_stats"),
            InlineKeyboardButton("◀️ Назад", callback_data="admin")
        )
    )

# ✅ ИСПРАВЛЕННЫЙ ОБРАБОТЧИК ВЫДАЧИ ЗВЁЗД
@dp.callback_query_handler(lambda c: c.data == 'admin_add_stars')
async def admin_add_stars_start(callback: CallbackQuery, state: FSMContext):
    """Выдача звёзд пользователю"""
    await callback.answer()
    
    await callback.message.edit_text(
        "🎁 <b>Выдача звёзд</b>\n\n"
        "Введите ID пользователя (число):",
        reply_markup=get_back_keyboard("admin")
    )
    
    await AdminStates.waiting_for_user_id.set()

@dp.message_handler(state=AdminStates.waiting_for_user_id)
async def admin_add_stars_user(message: Message, state: FSMContext):
    """Обработка ID пользователя"""
    try:
        user_id = int(message.text.strip())
    except ValueError:
        await message.reply("❌ Введите числовой ID")
        return
    
    user = db.get_user(user_id)
    if not user:
        await message.reply("❌ Пользователь не найден")
        await state.finish()
        return
    
    await state.update_data(target_user_id=user_id, target_username=user['username'])
    
    await message.reply(
        f"👤 <b>Пользователь:</b> @{user['username']} ({user_id})\n"
        f"💰 <b>Текущий баланс:</b> {user['stars_balance']} ⭐️\n\n"
        f"Введите количество звёзд для выдачи:",
        reply_markup=get_back_keyboard("admin")
    )
    
    await AdminStates.waiting_for_stars_amount.set()

@dp.message_handler(state=AdminStates.waiting_for_stars_amount)
async def admin_add_stars_amount(message: Message, state: FSMContext):
    """Обработка количества звёзд и выдача"""
    try:
        amount = int(message.text.strip())
        if amount <= 0:
            await message.reply("❌ Введите положительное число")
            return
    except ValueError:
        await message.reply("❌ Введите целое положительное число")
        return
    
    data = await state.get_data()
    user_id = data['target_user_id']
    username = data.get('target_username', f"ID {user_id}")
    
    # Получаем информацию о пользователе для проверки
    user = db.get_user(user_id)
    if not user:
        await message.reply("❌ Пользователь не найден")
        await state.finish()
        return
    
    # Выдаём звёзды
    success = db.add_stars(user_id, amount, "admin", f"admin_{message.from_user.id}")
    
    if success:
        # Получаем обновленный баланс
        updated_user = db.get_user(user_id)
        new_balance = updated_user['stars_balance'] if updated_user else 0
        
        # Уведомляем пользователя
        try:
            await bot.send_message(
                user_id,
                f"🎁 <b>Вам начислено {amount} ⭐️!</b>\n\n"
                f"💰 Новый баланс: {new_balance} ⭐️\n\n"
                f"👤 Администратор: @{message.from_user.username or 'Admin'}"
            )
            logger.info(f"✅ Уведомление отправлено пользователю {user_id}")
        except Exception as e:
            logger.warning(f"⚠️ Не удалось уведомить пользователя {user_id}: {e}")
        
        await message.reply(
            f"✅ <b>Звёзды успешно выданы!</b>\n\n"
            f"👤 <b>Пользователь:</b> @{username} ({user_id})\n"
            f"➕ <b>Добавлено:</b> {amount} ⭐️\n"
            f"💰 <b>Текущий баланс:</b> {user['stars_balance']} ⭐️\n"
            f"💰 <b>Новый баланс:</b> {new_balance} ⭐️",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin"),
                InlineKeyboardButton("🎁 Выдать ещё", callback_data="admin_add_stars")
            )
        )
        logger.info(f"✅ Админ {message.from_user.id} выдал {amount}⭐ пользователю {user_id}")
    else:
        await message.reply(
            "❌ Ошибка при выдаче звёзд",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin")
            )
        )
    
    await state.finish()

@dp.callback_query_handler(lambda c: c.data == 'transactions')
async def show_transactions(callback: CallbackQuery):
    """История транзакций пользователя"""
    await callback.answer()
    user_id = callback.from_user.id
    
    if db.db_url:
        with db.get_cursor() as cursor:
            cursor.execute('''
                SELECT * FROM transactions 
                WHERE user_id = %s 
                ORDER BY created_at DESC 
                LIMIT 20
            ''', (user_id,))
            transactions = [dict(row) for row in cursor.fetchall()]
    else:
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
        payment = f" | {t['payment_system']}" if t.get('payment_system') else ""
        
        text += f"{sign} {date} | {amount} ⭐️{rub}{payment}\n"
        text += f"   {t['description'] if t.get('description') else ''}\n\n"
    
    await callback.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup().add(
            InlineKeyboardButton("◀️ Назад", callback_data="profile")
        )
    )

# ================= ВЕБ-СЕРВЕР =================

async def handle(request):
    """Обработчик для проверки работы сервера"""
    return web.Response(text="🤖 Telegram Numbers Shop Bot is running!")

async def health_check(request):
    """Проверка здоровья бота"""
    health_data = {
        'status': 'healthy',
        'timestamp': time.time(),
        'uptime': time.time() - start_time,
        'database': 'connected' if db.db_url else 'sqlite',
        'stats': db.get_stats()
    }
    return web.json_response(health_data)

async def payment_webhook(request):
    """Webhook для получения уведомлений о платежах"""
    try:
        data = await request.json()
        logger.info(f"📩 Webhook получен: {data}")
        
        if data.get('payload'):
            payment_id = data['payload']
            if data.get('status') == 'paid':
                if db.db_url:
                    with db.get_cursor() as cursor:
                        cursor.execute('SELECT * FROM payments WHERE id = %s', (payment_id,))
                        payment = cursor.fetchone()
                        
                        if payment and payment['status'] == 'pending':
                            cursor.execute('''
                                UPDATE payments SET status = 'completed', completed_at = %s WHERE id = %s
                            ''', (time.time(), payment_id))
                            
                            cursor.execute('''
                                UPDATE users SET stars_balance = stars_balance + %s WHERE user_id = %s
                            ''', (payment['stars_amount'], payment['user_id']))
                            
                            cursor.execute('''
                                UPDATE transactions SET status = 'completed', completed_at = %s 
                                WHERE user_id = %s AND number_id = %s
                            ''', (time.time(), payment['user_id'], payment['number_id']))
                else:
                    with db.get_cursor() as cursor:
                        cursor.execute('SELECT * FROM payments WHERE id = ?', (payment_id,))
                        payment = cursor.fetchone()
                        
                        if payment and payment['status'] == 'pending':
                            cursor.execute('''
                                UPDATE payments SET status = 'completed', completed_at = ? WHERE id = ?
                            ''', (time.time(), payment_id))
                            
                            cursor.execute('''
                                UPDATE users SET stars_balance = stars_balance + ? WHERE user_id = ?
                            ''', (payment['stars_amount'], payment['user_id']))
                            
                            cursor.execute('''
                                UPDATE transactions SET status = 'completed', completed_at = ? 
                                WHERE user_id = ? AND number_id = ?
                            ''', (time.time(), payment['user_id'], payment['number_id']))
                
                logger.info(f"✅ Webhook: платеж {payment_id} завершен")
        
        return web.Response(status=200)
    except Exception as e:
        logger.error(f"❌ Webhook error: {e}")
        return web.Response(status=500)

async def web_server():
    """Запуск веб-сервера"""
    app = web.Application()
    app.router.add_get('/', handle)
    app.router.add_get('/health', health_check)
    app.router.add_post('/api/cryptobot/webhook', payment_webhook)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    
    logger.info(f"✅ Веб-сервер запущен на порту {PORT}")
    
    while True:
        await asyncio.sleep(3600)

# ================= МОНИТОРИНГ ЗДОРОВЬЯ =================

async def health_monitor():
    """Мониторинг здоровья бота"""
    global running, last_message_time
    
    error_count = 0
    max_errors = 5
    
    while running:
        try:
            me = await bot.get_me()
            
            current_time = time.time()
            if current_time - last_message_time > 300:
                logger.warning("⚠️ Бот неактивен 5 минут, проверка...")
                
                try:
                    await bot.send_message(ADMIN_IDS[0], "🟢 Health check: бот работает")
                    last_message_time = current_time
                    error_count = 0
                except Exception as e:
                    error_count += 1
                    logger.error(f"❌ Health check failed: {e}")
            
            try:
                db.get_stats()
                error_count = max(0, error_count - 1)
            except Exception as e:
                error_count += 1
                logger.error(f"❌ Ошибка базы данных: {e}")
            
            if error_count >= max_errors:
                logger.error(f"❌ Слишком много ошибок ({error_count}), перезапуск...")
                
                try:
                    await bot.send_message(
                        ADMIN_IDS[0],
                        f"⚠️ <b>Автоматический перезапуск</b>\n\n"
                        f"Причина: слишком много ошибок ({error_count})"
                    )
                except:
                    pass
                
                restart_bot()
            
            await asyncio.sleep(60)
            
        except Exception as e:
            logger.error(f"❌ Ошибка в health_monitor: {e}")
            error_count += 1
            await asyncio.sleep(30)

# ================= ПЛАНОВЫЙ ПЕРЕЗАПУСК =================

async def scheduled_restart():
    """Плановый перезапуск бота каждый день в 4 утра"""
    global running
    
    while running:
        try:
            now = datetime.now()
            next_restart = now.replace(hour=4, minute=0, second=0, microsecond=0)
            if now >= next_restart:
                next_restart += timedelta(days=1)
            
            wait_seconds = (next_restart - now).total_seconds()
            logger.info(f"⏰ Следующий плановый перезапуск через {wait_seconds/3600:.1f} часов")
            
            await asyncio.sleep(wait_seconds)
            
            for admin_id in ADMIN_IDS:
                try:
                    await bot.send_message(
                        admin_id,
                        "🔄 <b>Плановый перезапуск бота</b>\n\n"
                        "Бот будет перезапущен для обновления. Ожидайте 10 секунд..."
                    )
                except:
                    pass
            
            logger.info("🔄 Плановый перезапуск...")
            restart_bot()
            
        except Exception as e:
            logger.error(f"❌ Ошибка в scheduled_restart: {e}")
            await asyncio.sleep(3600)

# ================= ЗАПУСК =================

start_time = time.time()

async def cleanup_task():
    """Периодическая очистка сессий"""
    while running:
        try:
            await session_manager.cleanup()
            
            if db.db_url:
                with db.get_cursor() as cursor:
                    week_ago = time.time() - 7 * 24 * 3600
                    cursor.execute('DELETE FROM session_logs WHERE created_at < %s', (week_ago,))
            else:
                with db.get_cursor() as cursor:
                    week_ago = time.time() - 7 * 24 * 3600
                    cursor.execute('DELETE FROM session_logs WHERE created_at < ?', (week_ago,))
        except Exception as e:
            logger.error(f"❌ Ошибка в cleanup_task: {e}")
        
        await asyncio.sleep(3600)

async def stats_logger():
    """Периодическое логирование статистики"""
    while running:
        try:
            stats = db.get_stats()
            
            cpu_percent = psutil.cpu_percent(interval=1)
            memory = psutil.virtual_memory()
            
            logger.info(f"📊 Статистика: Users={stats['total_users']}, "
                       f"Numbers={stats['available_numbers']}, "
                       f"Sold={stats['sold_numbers']}, "
                       f"Accounts={stats['active_accounts']}, "
                       f"CPU={cpu_percent}%, RAM={memory.percent}%")
        except Exception as e:
            logger.error(f"❌ Ошибка в stats_logger: {e}")
        
        await asyncio.sleep(3600)

async def on_startup(dp):
    """Действия при запуске бота"""
    global start_time
    start_time = time.time()
    
    logger.info("🚀 Бот запускается...")
    
    try:
        me = await bot.get_me()
        logger.info(f"✅ Бот авторизован: @{me.username} (ID: {me.id})")
    except Unauthorized:
        logger.error("❌ НЕДЕЙСТВИТЕЛЬНЫЙ ТОКЕН! Получите новый у @BotFather")
        return
    
    logger.info(f"📁 Папка сессий: {SESSIONS_DIR}")
    logger.info(f"📁 Папка бекапов: {DATABASE_BACKUP_DIR}")
    if db.db_url:
        logger.info(f"📁 База данных: PostgreSQL")
    else:
        logger.info(f"📁 База данных: SQLite")
    
    # Загружаем сохраненные сессии
    await session_manager.load_saved_sessions()
    
    asyncio.create_task(web_server())
    asyncio.create_task(cleanup_task())
    asyncio.create_task(stats_logger())
    asyncio.create_task(health_monitor())
    asyncio.create_task(scheduled_restart())
    
    stats = db.get_stats()
    logger.info(f"📊 Начальная статистика: Users={stats['total_users']}, "
                f"Numbers={stats['available_numbers']}, Accounts={stats['total_accounts']}")
    
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"🚀 <b>Numbers Shop Bot запущен!</b>\n\n"
                f"📊 <b>Статистика:</b>\n"
                f"• Пользователей: {stats['total_users']}\n"
                f"• Номеров в продаже: {stats['available_numbers']}\n"
                f"• Аккаунтов TG: {stats['active_accounts']}\n"
                f"• Продано номеров: {stats['sold_numbers']}\n\n"
                f"⚙️ <b>Система:</b>\n"
                f"• База данных: {'PostgreSQL' if db.db_url else 'SQLite'}\n"
                f"• Сессии сохраняются: ✅\n"
                f"• Автоперезапуск: ✅\n"
                f"• Python: {sys.version.split()[0]}\n"
                f"• API ID: {API_ID}"
            )
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")
    
    logger.info("✅ Бот готов к работе")

async def on_shutdown(dp):
    """Действия при остановке бота"""
    global running
    running = False
    
    logger.info("🛑 Бот останавливается...")
    
    closed_sessions = 0
    for phone, client in session_manager.active_sessions.items():
        try:
            await client.disconnect()
            closed_sessions += 1
        except Exception as e:
            logger.error(f"❌ Ошибка при закрытии сессии {phone}: {e}")
    
    logger.info(f"✅ Закрыто активных сессий: {closed_sessions}")
    
    try:
        if not db.db_url:
            backup_file = os.path.join(DATABASE_BACKUP_DIR, f"final_backup_{int(time.time())}.db")
            shutil.copy2(db.db_path, backup_file)
            logger.info(f"✅ Создан финальный бекап: {backup_file}")
    except Exception as e:
        logger.error(f"❌ Ошибка создания финального бекапа: {e}")
    
    uptime = time.time() - start_time
    uptime_str = str(timedelta(seconds=int(uptime)))
    
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"🛑 <b>Бот остановлен</b>\n\n"
                f"⏱ Время работы: {uptime_str}\n"
                f"✅ Все сессии закрыты, файлы сохранены"
            )
        except:
            pass
    
    logger.info(f"✅ Бот остановлен. Время работы: {uptime_str}")

# ================= ЗАПУСК БОТА =================

def start_bot():
    """Запуск бота с защитой от падений"""
    max_retries = 10
    retry_count = 0
    
    while retry_count < max_retries:
        try:
            logger.info(f"🚀 Попытка запуска #{retry_count + 1}")
            
            executor.start_polling(
                dp,
                skip_updates=True,
                on_startup=on_startup,
                on_shutdown=on_shutdown
            )
            
            logger.info("✅ Бот нормально завершил работу")
            break
            
        except (Unauthorized, Exception) as e:
            retry_count += 1
            logger.error(f"❌ Критическая ошибка при запуске: {e}")
            logger.error(traceback.format_exc())
            
            if retry_count < max_retries:
                logger.info(f"⏳ Ожидание 10 секунд перед перезапуском...")
                time.sleep(10)
            else:
                logger.error(f"❌ Достигнут лимит попыток ({max_retries})")
                sys.exit(1)

# ================= ТОЧКА ВХОДА =================

if __name__ == "__main__":
    print("=" * 70)
    print("🚀 Telegram Numbers Shop Bot v18.0 - ФИНАЛЬНАЯ ВЕРСИЯ")
    print("📱 3 способа пополнения: ЮMoney | Crypto Bot | Звёзды TG")
    print("✅ Сессии СОХРАНЯЮТСЯ в файлы")
    print("=" * 70)
    print(f"👥 Администраторы: {ADMIN_IDS}")
    print(f"📁 Папка сессий: {SESSIONS_DIR}")
    print(f"📁 Папка бекапов: {DATABASE_BACKUP_DIR}")
    print(f"💾 База данных: {'PostgreSQL' if DATABASE_URL else 'SQLite'}")
    print("=" * 70)
    print("⚡ Система автоперезапуска: АКТИВНА")
    print("⚡ Health monitor: АКТИВЕН")
    print("⚡ Плановый перезапуск: 4:00 daily")
    print("=" * 70)
    
    # Запускаем бота
    start_bot()
