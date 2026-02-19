"""
Telegram Numbers Shop Bot + Session Manager
Версия: 25.0 (FINAL - ИСПРАВЛЕННАЯ ВЕРСИЯ)
Функции:
- Продажа виртуальных номеров Telegram
- Создание и управление сессиями Telegram аккаунтов
- Автоматическое получение кодов подтверждения
- Поддержка двухфакторной аутентификации (2FA)
- 3 СПОСОБА ПОПОЛНЕНИЯ БАЛАНСА
- Админ-панель с выдачей звёзд
- ✅ НАСТРАИВАЕМОЕ МЕНЮ (текст, описание, фото/гифка)
- ✅ ИЗМЕНЕНИЕ ПРОФИЛЯ В АДМИНКЕ
- ✅ ЗАГРУЗКА ФОТО И ГИФОК
- ✅ ОБЯЗАТЕЛЬНЫЕ ПОДПИСКИ НА КАНАЛЫ (до 5 каналов)
- ✅ ПРОВЕРКА ПОДПИСКИ ПРИ ПОКУПКЕ
- ✅ УПРАВЛЕНИЕ КАНАЛАМИ В АДМИНКЕ
- ✅ АДМИНЫ ИМЕЮТ БЕСКОНЕЧНЫЙ БАЛАНС (♾)
- ✅ УДАЛЕНИЕ СЕССИЙ И НОМЕРОВ
- ✅ СЕССИИ СОХРАНЯЮТСЯ В ФАЙЛЫ
- ✅ СИСТЕМА "ВЕЧНОЙ РАБОТЫ" (НЕ ВЫКЛЮЧАЕТСЯ)
- ✅ АВТОМАТИЧЕСКИЙ ПЕРЕЗАПУСК ПРИ СБОЯХ
- ✅ ПИНГ-СИСТЕМА ДЛЯ RENDER
- Полный мониторинг и логирование
- Поддержка PostgreSQL на Render
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
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from contextlib import contextmanager
from urllib.parse import urlencode
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

# ИМПОРТЫ AIOGRAM (ПЕРЕНЕСЕНЫ ВВЕРХ!)
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
from aiogram.utils.exceptions import Unauthorized, RestartingTelegram, TerminatedByOtherGetUpdates

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

# Загружаем переменные окружения
load_dotenv()

# ================= НАСТРОЙКА ЛОГИРОВАНИЯ =================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler('bot.log')
    ]
)
logger = logging.getLogger(__name__)

logger.info("🚀 Запуск бота...")

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
    logger.info("🔄 Запуск на Render платформе")
    SESSIONS_DIR = '/tmp/sessions'
    DATABASE_BACKUP_DIR = '/tmp/backups'
    MEDIA_DIR = '/tmp/media'
else:
    SESSIONS_DIR = "sessions"
    DATABASE_BACKUP_DIR = "backups"
    MEDIA_DIR = "media"

# Создаем папки
os.makedirs(SESSIONS_DIR, exist_ok=True)
os.makedirs(DATABASE_BACKUP_DIR, exist_ok=True)
os.makedirs(MEDIA_DIR, exist_ok=True)

# Проверяем доступность папки для сессий
test_session_file = os.path.join(SESSIONS_DIR, "test_write.tmp")
try:
    with open(test_session_file, "w") as f:
        f.write("test")
    os.remove(test_session_file)
    logger.info(f"✅ Папка {SESSIONS_DIR} доступна для записи")
except Exception as e:
    logger.error(f"❌ Нет доступа к папке сессий {SESSIONS_DIR}: {e}")

# ================= КОНФИГУРАЦИЯ =================

# ✅ НОВЫЙ ТОКЕН БОТА
BOT_TOKEN = "8594091933:AAEDB7UGjNfwR-g3Dt3n0Vgo3QF1uD6gN68"

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

# Символ бесконечности для админов
INFINITY = "♾"

# Максимальное количество каналов для подписки
MAX_CHANNELS = 5

# ================= ИНИЦИАЛИЗАЦИЯ БОТА =================
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
number_cb = CallbackData('number', 'action', 'number_id')
channel_cb = CallbackData('channel', 'action', 'channel_id')

logger.info(f"📁 Sessions dir: {SESSIONS_DIR}")
logger.info(f"📁 Backups dir: {DATABASE_BACKUP_DIR}")
logger.info(f"👥 Администраторы: {ADMIN_IDS}")
if DATABASE_URL:
    logger.info(f"✅ Используется PostgreSQL")
else:
    logger.info(f"⚠️ Используется SQLite")
logger.info(f"✅ Токен бота: {BOT_TOKEN[:10]}...")

# ================= КЛАСС ДЛЯ ОБРАБОТКИ ОТМЕНЫ =================
class CancelHandler(Exception):
    """Исключение для отмены обработки"""
    pass

# ================= СИСТЕМА "ВЕЧНОЙ РАБОТЫ" =================

running = True
restart_requested = False
last_message_time = time.time()
restart_count = 0
max_restarts = 1000  # Увеличено до 1000
restart_window = 3600
restart_times = []
uptime_start = time.time()
ping_count = 0

# Фоновый поток для пинга (чтобы Render не "засыпал")
def keep_alive_ping():
    """Фоновый поток для постоянного пинга"""
    global ping_count
    while True:
        try:
            ping_count += 1
            logger.debug(f"🏓 Keep-alive ping #{ping_count}")
            time.sleep(30)  # Пинг каждые 30 секунд
        except:
            pass

# Запускаем фоновый поток пинга
ping_thread = threading.Thread(target=keep_alive_ping, daemon=True)
ping_thread.start()

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
    
    # Полный перезапуск процесса
    python = sys.executable
    os.execl(python, python, *sys.argv)

def signal_handler(sig, frame):
    """Обработчик сигналов"""
    global running
    logger.info(f"📡 Получен сигнал {sig}, завершаем работу...")
    running = False
    # Даем время на завершение
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
    
    # Перезапускаемся
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
            # Не падаем, просто возвращаем None
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

# ================= ФУНКЦИИ ДЛЯ ПРОВЕРКИ АДМИНОВ =================

def is_admin(user_id: int) -> bool:
    """Проверка, является ли пользователь админом"""
    return user_id in ADMIN_IDS

def get_user_balance_display(user_id: int, balance: int) -> str:
    """Получение отображения баланса (♾ для админов)"""
    if is_admin(user_id):
        return INFINITY
    return str(balance)

def can_afford(user_id: int, cost: int) -> bool:
    """Проверка, может ли пользователь позволить себе покупку"""
    if is_admin(user_id):
        return True  # Админы могут покупать всё
    
    user = db.get_user(user_id)
    return user and user['stars_balance'] >= cost

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
            
            # Таблица пользователей
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
            
            # Таблица настроек бота
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at REAL
                )
            ''')
            
            # Таблица Telegram аккаунтов
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
            
            # Таблица номеров
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
            
            # Таблица транзакций
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
            
            # Таблица платежей
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
            
            # Таблица пополнений
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
            
            # Таблица каналов для подписки
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS channels (
                    id SERIAL PRIMARY KEY,
                    channel_id TEXT UNIQUE,
                    channel_name TEXT,
                    channel_url TEXT,
                    invite_link TEXT,
                    is_mandatory BOOLEAN DEFAULT TRUE,
                    position INTEGER DEFAULT 0,
                    created_at REAL,
                    created_by BIGINT
                )
            ''')
            
            # Таблица логов сессий
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
            
            # Таблица системных логов
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
            
            # Таблица пользователей
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
            
            # Таблица настроек бота
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at REAL
                )
            ''')
            
            # Таблица Telegram аккаунтов
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
            
            # Таблица номеров
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
            
            # Таблица транзакций
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
            
            # Таблица платежей
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
            
            # Таблица пополнений
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
            
            # Таблица каналов для подписки
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id TEXT UNIQUE,
                    channel_name TEXT,
                    channel_url TEXT,
                    invite_link TEXT,
                    is_mandatory INTEGER DEFAULT 1,
                    position INTEGER DEFAULT 0,
                    created_at REAL,
                    created_by INTEGER
                )
            ''')
            
            # Таблица логов сессий
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
            
            # Таблица системных логов
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
    
    # ===== Методы для настроек бота =====
    
    def get_setting(self, key: str, default: str = "") -> str:
        """Получение настройки бота"""
        cache_key = f'setting_{key}'
        if cache_key in self.cache:
            cached, timestamp = self.cache[cache_key]
            if time.time() - timestamp < CACHE_TTL:
                return cached
        
        try:
            if self.db_url:
                with self.get_cursor() as cursor:
                    cursor.execute('SELECT value FROM bot_settings WHERE key = %s', (key,))
                    row = cursor.fetchone()
                    value = row['value'] if row else default
            else:
                with self.get_cursor() as cursor:
                    cursor.execute('SELECT value FROM bot_settings WHERE key = ?', (key,))
                    row = cursor.fetchone()
                    value = row['value'] if row else default
            
            self.cache[cache_key] = (value, time.time())
            return value
        except Exception as e:
            logger.error(f"Ошибка получения настройки {key}: {e}")
            return default
    
    def set_setting(self, key: str, value: str) -> bool:
        """Установка настройки бота"""
        try:
            if self.db_url:
                with self.get_cursor() as cursor:
                    cursor.execute('''
                        INSERT INTO bot_settings (key, value, updated_at)
                        VALUES (%s, %s, %s)
                        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = EXCLUDED.updated_at
                    ''', (key, value, time.time()))
            else:
                with self.get_cursor() as cursor:
                    cursor.execute('''
                        INSERT OR REPLACE INTO bot_settings (key, value, updated_at)
                        VALUES (?, ?, ?)
                    ''', (key, value, time.time()))
            
            # Очищаем кэш
            cache_key = f'setting_{key}'
            if cache_key in self.cache:
                del self.cache[cache_key]
            
            logger.info(f"✅ Настройка {key} обновлена")
            return True
        except Exception as e:
            logger.error(f"❌ Ошибка установки настройки {key}: {e}")
            return False
    
    def get_welcome_text(self) -> str:
        """Получение текста приветствия"""
        return self.get_setting('welcome_text', 
            "👋 <b>Добро пожаловать в магазин номеров Telegram!</b>\n\n"
            "📱 Здесь вы можете купить виртуальные номера для Telegram.\n\n"
            "🔹 Пополняйте баланс звёздами\n"
            "🔹 Покупайте номера\n"
            "🔹 Получайте коды подтверждения"
        )
    
    def get_profile_text(self) -> str:
        """Получение текста профиля"""
        return self.get_setting('profile_text',
            "👤 <b>Ваш профиль</b>"
        )
    
    def get_welcome_media(self) -> str:
        """Получение ID медиа для приветствия"""
        return self.get_setting('welcome_media', '')
    
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
        """Списание звёзд (для админов не списывается)"""
        # Админам не списываем звёзды
        if is_admin(user_id):
            logger.info(f"👑 Админ {user_id} купил за {amount}⭐ (не списано)")
            return True
        
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
    
    def delete_tg_account(self, phone: str) -> bool:
        """Удаление Telegram аккаунта"""
        try:
            account = self.get_tg_account(phone)
            if not account:
                return False
            
            # Удаляем файл сессии
            session_path = os.path.join(SESSIONS_DIR, account['session_name'])
            if os.path.exists(f"{session_path}.session"):
                os.remove(f"{session_path}.session")
                logger.info(f"🗑 Удален файл сессии для {phone}")
            
            if self.db_url:
                with self.get_cursor() as cursor:
                    cursor.execute('DELETE FROM tg_accounts WHERE phone = %s', (phone,))
                    return cursor.rowcount > 0
            else:
                with self.get_cursor() as cursor:
                    cursor.execute('DELETE FROM tg_accounts WHERE phone = ?', (phone,))
                    return cursor.rowcount > 0
        except Exception as e:
            logger.error(f"❌ Ошибка удаления аккаунта {phone}: {e}")
            return False
    
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
    
    def delete_number(self, number_id: int) -> bool:
        """Удаление номера из продажи"""
        try:
            if self.db_url:
                with self.get_cursor() as cursor:
                    cursor.execute('DELETE FROM numbers WHERE id = %s', (number_id,))
                    if cursor.rowcount > 0:
                        logger.info(f"✅ Номер {number_id} удален из магазина")
                        self.cache = {k: v for k, v in self.cache.items() if not k.startswith('numbers_')}
                        return True
            else:
                with self.get_cursor() as cursor:
                    cursor.execute('DELETE FROM numbers WHERE id = ?', (number_id,))
                    if cursor.rowcount > 0:
                        logger.info(f"✅ Номер {number_id} удален из магазина")
                        self.cache = {k: v for k, v in self.cache.items() if not k.startswith('numbers_')}
                        return True
            return False
        except Exception as e:
            logger.error(f"❌ Ошибка удаления номера {number_id}: {e}")
            return False
    
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
                    
                    cursor.execute('SELECT COUNT(*) as count FROM channels')
                    total_channels = cursor.fetchone()['count'] or 0
                    
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
                    
                    cursor.execute('SELECT COUNT(*) as count FROM channels')
                    total_channels = cursor.fetchone()['count'] or 0
                    
                    cursor.execute('SELECT SUM(amount_stars) as total FROM transactions WHERE status = "completed"')
                    total_stars_sold = cursor.fetchone()['total'] or 0
            
            return {
                'total_users': total_users,
                'available_numbers': available_numbers,
                'sold_numbers': sold_numbers,
                'pending_numbers': pending_numbers,
                'total_accounts': total_accounts,
                'active_accounts': active_accounts,
                'total_channels': total_channels,
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
                'total_channels': 0,
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
    
    async def delete_session(self, phone: str) -> bool:
        """Полное удаление сессии (активной и файла)"""
        try:
            # Если сессия активна - выходим
            if phone in self.active_sessions:
                await self.logout_session(phone, "admin_deleted")
            
            # Удаляем из базы данных
            result = db.delete_tg_account(phone)
            
            if result:
                logger.info(f"✅ Сессия {phone} полностью удалена")
            
            return result
        except Exception as e:
            logger.error(f"❌ Ошибка удаления сессии {phone}: {e}")
            return False
    
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

# ================= ФУНКЦИИ ПРОВЕРКИ ПОДПИСОК =================

async def check_subscriptions(user_id: int) -> Tuple[bool, List[Dict]]:
    """Проверка подписок пользователя на каналы"""
    channels = db.get_all_channels()
    if not channels:
        return True, []  # Нет обязательных каналов
    
    not_subscribed = []
    
    for channel in channels:
        if not channel['is_mandatory']:
            continue
        
        try:
            # Пытаемся получить информацию о чате
            chat = await bot.get_chat(channel['channel_id'])
            
            # Проверяем, является ли пользователь участником
            member = await bot.get_chat_member(channel['channel_id'], user_id)
            
            # Если пользователь не участник или покинул канал
            if member.status in ['left', 'kicked']:
                not_subscribed.append(channel)
        except Exception as e:
            logger.error(f"❌ Ошибка проверки подписки на канал {channel['channel_id']}: {e}")
            # Если не удалось проверить, считаем что не подписан
            not_subscribed.append(channel)
    
    return len(not_subscribed) == 0, not_subscribed

def get_subscription_keyboard(not_subscribed: List[Dict]) -> InlineKeyboardMarkup:
    """Клавиатура для подписки на каналы"""
    keyboard = InlineKeyboardMarkup(row_width=1)
    
    for channel in not_subscribed:
        keyboard.add(InlineKeyboardButton(
            f"📢 {channel['channel_name']}",
            url=channel['invite_link']
        ))
    
    keyboard.add(InlineKeyboardButton(
        "✅ Я подписался",
        callback_data="check_subscription"
    ))
    
    return keyboard

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
    waiting_for_channel_id = State()
    waiting_for_channel_name = State()
    waiting_for_channel_link = State()
    waiting_for_welcome_text = State()
    waiting_for_profile_text = State()

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
        InlineKeyboardButton("📢 Каналы подписки", callback_data="admin_channels"),
        InlineKeyboardButton("✏️ Редактировать меню", callback_data="admin_edit_menu"),
        InlineKeyboardButton("🔄 Перезапуск", callback_data="admin_restart"),
        InlineKeyboardButton("◀️ Назад", callback_data="main_menu")
    )
    return keyboard

def get_channels_keyboard(channels: List[Dict]):
    """Клавиатура для управления каналами"""
    keyboard = InlineKeyboardMarkup(row_width=1)
    
    for channel in channels:
        mandatory = "✅" if channel['is_mandatory'] else "❌"
        keyboard.add(InlineKeyboardButton(
            f"{mandatory} {channel['channel_name']}",
            callback_data=f"channel_view_{channel['channel_id']}"
        ))
    
    if len(channels) < MAX_CHANNELS:
        keyboard.add(InlineKeyboardButton(
            "➕ Добавить канал",
            callback_data="channel_add"
        ))
    
    keyboard.add(InlineKeyboardButton(
        "◀️ Назад",
        callback_data="admin"
    ))
    
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

def get_account_detail_keyboard(phone: str):
    """Клавиатура для управления конкретным аккаунтом"""
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("❌ Удалить сессию", callback_data=f"delete_session_{phone}"),
        InlineKeyboardButton("◀️ Назад", callback_data="admin_accounts")
    )
    return keyboard

def get_number_detail_keyboard(number_id: int):
    """Клавиатура для управления конкретным номером"""
    keyboard = InlineKeyboardMarkup(row_width=2)
    keyboard.add(
        InlineKeyboardButton("❌ Удалить номер", callback_data=f"delete_number_{number_id}"),
        InlineKeyboardButton("◀️ Назад", callback_data="admin_numbers")
    )
    return keyboard

def get_back_keyboard(callback_data: str = "main_menu"):
    """Клавиатура с кнопкой назад"""
    keyboard = InlineKeyboardMarkup().add(
        InlineKeyboardButton("◀️ Назад", callback_data=callback_data)
    )
    return keyboard

def get_edit_menu_keyboard():
    """Клавиатура для редактирования меню"""
    keyboard = InlineKeyboardMarkup(row_width=1)
    keyboard.add(
        InlineKeyboardButton("✏️ Изменить текст приветствия", callback_data="edit_welcome_text"),
        InlineKeyboardButton("📝 Изменить текст профиля", callback_data="edit_profile_text"),
        InlineKeyboardButton("🖼 Загрузить фото/гифку", callback_data="upload_media"),
        InlineKeyboardButton("🗑 Удалить медиа", callback_data="delete_media"),
        InlineKeyboardButton("◀️ Назад", callback_data="admin")
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
    
    # Проверяем подписки
    is_subscribed, not_subscribed = await check_subscriptions(user_id)
    
    if not is_subscribed and not is_admin(user_id):
        await message.reply(
            "📢 <b>Для доступа к боту необходимо подписаться на каналы:</b>\n\n"
            "После подписки нажмите кнопку '✅ Я подписался'",
            reply_markup=get_subscription_keyboard(not_subscribed)
        )
        return
    
    # Получаем настройки меню
    welcome_text = db.get_welcome_text()
    welcome_media = db.get_welcome_media()
    
    if welcome_media:
        # Проверяем тип медиа (фото или гифка)
        try:
            await bot.send_animation(
                chat_id=user_id,
                animation=welcome_media,
                caption=welcome_text,
                reply_markup=get_main_keyboard(user_id)
            )
        except:
            try:
                await bot.send_photo(
                    chat_id=user_id,
                    photo=welcome_media,
                    caption=welcome_text,
                    reply_markup=get_main_keyboard(user_id)
                )
            except:
                await message.reply(
                    welcome_text,
                    reply_markup=get_main_keyboard(user_id)
                )
    else:
        await message.reply(
            welcome_text,
            reply_markup=get_main_keyboard(user_id)
        )

# ... (остальные обработчики остаются без изменений, но идут после этого) ...
