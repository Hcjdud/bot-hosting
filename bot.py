"""
Telegram Numbers Shop Bot + Session Manager
Версия: 8.0 (Production Ready - FINAL)
Функции:
- Продажа виртуальных номеров Telegram
- Управление сессиями Telegram аккаунтов
- Автоматическое удаление номеров после продажи
- Сессии живут бесконечно (автоматический перезаход)
- Автоматический выход при заходе владельца
- Оплата через ЮMoney и Crypto Bot
- Админ-панель для управления
- Баланс пользователей в звёздах
- Полный мониторинг и логирование
- Адаптация для Render
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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from contextlib import contextmanager
from urllib.parse import urlencode

# Дополнительные импорты для работы с API и безопасностью
import requests
import urllib3
import certifi
import psutil
from dotenv import load_dotenv
import pytz
from cryptography.fernet import Fernet
from Crypto.Cipher import AES

# Загружаем переменные окружения
load_dotenv()

# Устанавливаем переменные окружения для Render
PORT = int(os.environ.get('PORT', 8080))
BASE_URL = os.environ.get('BASE_URL', f'http://localhost:{PORT}')

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
from aiogram.utils.exceptions import Unauthorized

# Pyrogram для управления сессиями Telegram
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

# Для веб-сервера (нужен для колбэков от платежей)
from aiohttp import web

# ================= КОНФИГУРАЦИЯ =================

# Данные бота - ЗАМЕНИТЕ НА СВОИ!
BOT_TOKEN = "8594091933:AAHCMs2fwNZpbx0lcOWBB1hNXTQJRs_8aPo"  # Получите новый у @BotFather!
ADMIN_IDS = [8443743937]  # Ваш Telegram ID

# API данные для Pyrogram (ваши)
API_ID = 26694682
API_HASH = "1278d6017ba6d2fd2228e69c638f332f"

# Платёжные системы
YOOMONEY_WALLET = "4100119410890051"  # Ваш кошелёк ЮMoney
YOOMONEY_SECRET = os.environ.get('YOOMONEY_SECRET', '')

# Crypto Bot токен
CRYPTOBOT_TOKEN = "UQCpU74nU-1MoECyq1IH24WA3677rgWtsVtJKEGVUGnVyawR"

# Настройки базы данных
DATABASE_FILE = "shop.db"
SESSIONS_DIR = "sessions"
DATABASE_BACKUP_DIR = "backups"
CONFIG_FILE = "bot_config.json"

# Курс: 1 звезда = X рублей
STAR_TO_RUB = 1.5

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

# ================= ПРОВЕРКА ПАПОК =================

# Проверяем и создаём нужные папки
required_dirs = [SESSIONS_DIR, DATABASE_BACKUP_DIR, os.path.dirname(DATABASE_FILE) or '.']
for dir_path in required_dirs:
    if dir_path and not os.path.exists(dir_path):
        try:
            os.makedirs(dir_path, exist_ok=True)
            logger.info(f"✅ Создана папка: {dir_path}")
        except Exception as e:
            logger.error(f"❌ Не удалось создать папку {dir_path}: {e}")
            # Используем текущую папку как запасной вариант
            if dir_path == SESSIONS_DIR:
                SESSIONS_DIR = "sessions"
            elif dir_path == DATABASE_BACKUP_DIR:
                DATABASE_BACKUP_DIR = "backups"
            elif dir_path == os.path.dirname(DATABASE_FILE):
                DATABASE_FILE = "shop.db"

# Проверяем права на запись в текущей папке
try:
    test_file = "test_write.tmp"
    with open(test_file, "w") as f:
        f.write("test")
    os.remove(test_file)
    logger.info(f"✅ Права на запись есть в папке: {os.getcwd()}")
except Exception as e:
    logger.error(f"❌ Нет прав на запись в текущей папке: {e}")
    sys.exit(1)

# Инициализация бота
storage = MemoryStorage()
bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(bot, storage=storage)
dp.middleware.setup(LoggingMiddleware())

# Callback data
numbers_cb = CallbackData('numbers', 'page')
buy_cb = CallbackData('buy', 'number_id')
sessions_cb = CallbackData('sessions', 'page')
session_cb = CallbackData('session', 'action', 'phone')
admin_cb = CallbackData('admin', 'action', 'page')
payment_cb = CallbackData('payment', 'action', 'payment_id')

# ================= БАЗА ДАННЫХ =================

class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.cache = {}
        
        # Создаём папку для БД если её нет
        db_dir = os.path.dirname(db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
            logger.info(f"✅ Создана папка для БД: {db_dir}")
        
        # Проверяем доступность для записи
        self._check_write_permission()
        
        # Инициализируем БД
        self._init_db()
    
    def _check_write_permission(self):
        """Проверка прав на запись"""
        try:
            # Пробуем создать временный файл
            test_file = os.path.join(os.path.dirname(self.db_path) or '.', 'test_write.tmp')
            with open(test_file, 'w') as f:
                f.write('test')
            os.remove(test_file)
            logger.info(f"✅ Права на запись есть в папке: {os.path.dirname(self.db_path) or '.'}")
        except Exception as e:
            logger.error(f"❌ Нет прав на запись: {e}")
            # Пробуем использовать текущую папку
            self.db_path = os.path.join(os.getcwd(), 'shop.db')
            logger.info(f"✅ Используем альтернативный путь: {self.db_path}")
    
    def _get_connection(self):
        """Получение соединения с БД"""
        try:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.Error as e:
            logger.error(f"❌ Ошибка подключения к БД: {e}")
            # Пробуем создать файл заново
            if os.path.exists(self.db_path):
                os.remove(self.db_path)
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            return conn
    
    @contextmanager
    def get_cursor(self):
        """Контекстный менеджер для работы с БД"""
        conn = None
        cursor = None
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            yield cursor
            conn.commit()
        except sqlite3.Error as e:
            logger.error(f"❌ Ошибка SQLite: {e}")
            if conn:
                conn.rollback()
            raise
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()
    
    def _init_db(self):
        """Инициализация таблиц"""
        max_retries = 3
        for attempt in range(max_retries):
            try:
                with self.get_cursor() as cursor:
                    # Таблица пользователей бота
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
                    
                    # Таблица Telegram аккаунтов (сессий)
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
                            notes TEXT
                        )
                    ''')
                    
                    # Таблица номеров для продажи
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
                            payment_system TEXT,
                            payment_id TEXT,
                            status TEXT,
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
                    
                    # Таблица для хранения логов системы
                    cursor.execute('''
                        CREATE TABLE IF NOT EXISTS system_logs (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            level TEXT,
                            module TEXT,
                            message TEXT,
                            created_at REAL
                        )
                    ''')
                    
                    # Проверяем, создались ли таблицы
                    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
                    tables = cursor.fetchall()
                    logger.info(f"✅ Созданы таблицы: {[t[0] for t in tables]}")
                    
                    break  # Успешно, выходим из цикла
                    
            except sqlite3.Error as e:
                logger.error(f"❌ Попытка {attempt + 1}/{max_retries} не удалась: {e}")
                if attempt == max_retries - 1:
                    # Последняя попытка не удалась
                    logger.error("❌ Критическая ошибка: не удалось создать БД")
                    raise
                time.sleep(1)  # Ждём перед повторной попыткой
        
        logger.info(f"✅ База данных инициализирована: {self.db_path}")
    
    # ===== Методы для пользователей бота =====
    
    def get_user(self, user_id: int) -> Optional[Dict]:
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
    
    def create_user(self, user_id: int, username: str, first_name: str) -> bool:
        try:
            with self.get_cursor() as cursor:
                cursor.execute('''
                    INSERT OR IGNORE INTO users (user_id, username, first_name, registered_at, last_activity)
                    VALUES (?, ?, ?, ?, ?)
                ''', (user_id, username, first_name, time.time(), time.time()))
                return True
        except Exception as e:
            logger.error(f"Ошибка создания пользователя: {e}")
            return False
    
    def update_user_activity(self, user_id: int):
        with self.get_cursor() as cursor:
            cursor.execute('UPDATE users SET last_activity = ? WHERE user_id = ?', 
                          (time.time(), user_id))
            if f'user_{user_id}' in self.cache:
                del self.cache[f'user_{user_id}']
    
    def add_stars(self, user_id: int, amount: int) -> bool:
        try:
            with self.get_cursor() as cursor:
                cursor.execute('UPDATE users SET stars_balance = stars_balance + ? WHERE user_id = ?', 
                             (amount, user_id))
                cursor.execute('''
                    INSERT INTO transactions (user_id, amount_stars, type, created_at)
                    VALUES (?, ?, 'credit', ?)
                ''', (user_id, amount, time.time()))
                
                if f'user_{user_id}' in self.cache:
                    del self.cache[f'user_{user_id}']
                return True
        except Exception as e:
            logger.error(f"Ошибка добавления звёзд: {e}")
            return False
    
    def deduct_stars(self, user_id: int, amount: int) -> bool:
        try:
            with self.get_cursor() as cursor:
                cursor.execute('SELECT stars_balance FROM users WHERE user_id = ?', (user_id,))
                row = cursor.fetchone()
                if row and row['stars_balance'] >= amount:
                    cursor.execute('UPDATE users SET stars_balance = stars_balance - ? WHERE user_id = ?', 
                                 (amount, user_id))
                    cursor.execute('''
                        INSERT INTO transactions (user_id, amount_stars, type, created_at)
                        VALUES (?, ?, 'debit', ?)
                    ''', (user_id, amount, time.time()))
                    
                    if f'user_{user_id}' in self.cache:
                        del self.cache[f'user_{user_id}']
                    return True
                return False
        except Exception as e:
            logger.error(f"Ошибка списания звёзд: {e}")
            return False
    
    # ===== Методы для Telegram аккаунтов (сессий) =====
    
    def add_tg_account(self, phone: str, session_name: str, api_id: int, api_hash: str, 
                       user_info: Dict, added_by: int) -> bool:
        try:
            with self.get_cursor() as cursor:
                cursor.execute('''
                    INSERT OR REPLACE INTO tg_accounts 
                    (phone, session_name, api_id, api_hash, first_name, last_name, username, user_id, 
                     added_by, added_at, last_used, status, owner_id, owner_checked)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    phone, session_name, api_id, api_hash,
                    user_info.get('first_name', ''),
                    user_info.get('last_name', ''),
                    user_info.get('username', ''),
                    user_info.get('id', 0),
                    added_by, time.time(), time.time(),
                    'active', 0, 0
                ))
                return True
        except Exception as e:
            logger.error(f"Ошибка добавления аккаунта {phone}: {e}")
            return False
    
    def get_tg_account(self, phone: str) -> Optional[Dict]:
        with self.get_cursor() as cursor:
            cursor.execute('SELECT * FROM tg_accounts WHERE phone = ?', (phone,))
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def get_all_tg_accounts(self) -> List[Dict]:
        with self.get_cursor() as cursor:
            cursor.execute('SELECT * FROM tg_accounts ORDER BY added_at DESC')
            return [dict(row) for row in cursor.fetchall()]
    
    def update_tg_account_status(self, phone: str, status: str, notes: str = ""):
        with self.get_cursor() as cursor:
            cursor.execute('''
                UPDATE tg_accounts 
                SET status = ?, notes = ?, last_used = ? 
                WHERE phone = ?
            ''', (status, notes, time.time(), phone))
    
    def set_tg_account_code(self, phone: str, code: str):
        with self.get_cursor() as cursor:
            cursor.execute('''
                UPDATE tg_accounts 
                SET last_code = ?, last_code_time = ? 
                WHERE phone = ?
            ''', (code, time.time(), phone))
    
    def get_available_tg_account(self) -> Optional[Dict]:
        """Получение случайного активного аккаунта для получения кода"""
        with self.get_cursor() as cursor:
            cursor.execute('''
                SELECT * FROM tg_accounts 
                WHERE status = 'active' AND banned = 0 AND spam_block = 0
                ORDER BY last_used ASC
                LIMIT 1
            ''')
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def log_session_action(self, phone: str, action: str, result: str, error: str = ""):
        with self.get_cursor() as cursor:
            cursor.execute('''
                INSERT INTO session_logs (phone, action, result, error, created_at)
                VALUES (?, ?, ?, ?, ?)
            ''', (phone, action, result, error, time.time()))
    
    def set_account_owner(self, phone: str, owner_id: int, owner_username: str):
        """Установка владельца аккаунта"""
        with self.get_cursor() as cursor:
            cursor.execute('''
                UPDATE tg_accounts 
                SET owner_id = ?, owner_username = ?, owner_checked = 1
                WHERE phone = ?
            ''', (owner_id, owner_username, phone))
    
    def check_account_owner(self, phone: str) -> Tuple[bool, int]:
        """Проверка, есть ли у аккаунта владелец"""
        with self.get_cursor() as cursor:
            cursor.execute('SELECT owner_id, owner_checked FROM tg_accounts WHERE phone = ?', (phone,))
            row = cursor.fetchone()
            if row and row['owner_checked'] and row['owner_id'] > 0:
                return True, row['owner_id']
            return False, 0
    
    # ===== Методы для номеров (товаров) =====
    
    def add_number(self, phone: str, country: str, description: str, 
                   price_stars: int, source_account: str = None) -> bool:
        try:
            price_rub = price_stars * STAR_TO_RUB
            with self.get_cursor() as cursor:
                cursor.execute('''
                    INSERT OR REPLACE INTO numbers 
                    (phone_number, country, description, price_stars, price_rub, source_account, status)
                    VALUES (?, ?, ?, ?, ?, ?, 'available')
                ''', (phone, country, description, price_stars, price_rub, source_account))
                logger.info(f"✅ Добавлен номер: {phone} | {country} | {price_stars}⭐ | {description}")
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
        
        with self.get_cursor() as cursor:
            cursor.execute('SELECT COUNT(*) as count FROM numbers WHERE status = "available"')
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
    
    def get_number(self, number_id: int) -> Optional[Dict]:
        with self.get_cursor() as cursor:
            cursor.execute('SELECT * FROM numbers WHERE id = ?', (number_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def purchase_number(self, number_id: int, user_id: int) -> Optional[Dict]:
        try:
            with self.get_cursor() as cursor:
                # Получаем номер
                cursor.execute('SELECT * FROM numbers WHERE id = ? AND status = "available"', (number_id,))
                number = cursor.fetchone()
                if not number:
                    return None
                number = dict(number)
                
                # Проверяем баланс пользователя
                cursor.execute('SELECT stars_balance FROM users WHERE user_id = ?', (user_id,))
                user = cursor.fetchone()
                if not user or user['stars_balance'] < number['price_stars']:
                    return None
                
                # Списываем звёзды
                cursor.execute('UPDATE users SET stars_balance = stars_balance - ? WHERE user_id = ?', 
                              (number['price_stars'], user_id))
                
                # Обновляем статус номера
                cursor.execute('''
                    UPDATE numbers 
                    SET status = 'pending', sold_to = ?, sold_at = ?
                    WHERE id = ?
                ''', (user_id, time.time(), number_id))
                
                # Записываем транзакцию
                cursor.execute('''
                    INSERT INTO transactions (user_id, number_id, amount_stars, status, created_at)
                    VALUES (?, ?, ?, 'pending', ?)
                ''', (user_id, number_id, number['price_stars'], time.time()))
                
                # Очищаем кэш
                self.cache = {k: v for k, v in self.cache.items() if not k.startswith('numbers_')}
                
                return number
        except Exception as e:
            logger.error(f"Ошибка покупки: {e}")
            return None
    
    def set_number_code(self, number_id: int, code: str) -> bool:
        try:
            with self.get_cursor() as cursor:
                cursor.execute('''
                    UPDATE numbers 
                    SET code = ?, code_expires = ?, status = 'sold'
                    WHERE id = ?
                ''', (code, time.time() + 3600, number_id))  # Код действителен 1 час
                logger.info(f"✅ Для номера {number_id} установлен код: {code}")
                return True
        except Exception as e:
            logger.error(f"Ошибка установки кода: {e}")
            return False
    
    def delete_sold_number(self, number_id: int) -> bool:
        """Удаление проданного номера из магазина"""
        try:
            with self.get_cursor() as cursor:
                cursor.execute('DELETE FROM numbers WHERE id = ? AND status = "sold"', (number_id,))
                if cursor.rowcount > 0:
                    logger.info(f"✅ Номер {number_id} удален из магазина после продажи")
                    # Очищаем кэш
                    self.cache = {k: v for k, v in self.cache.items() if not k.startswith('numbers_')}
                    return True
                return False
        except Exception as e:
            logger.error(f"❌ Ошибка удаления номера {number_id}: {e}")
            return False
    
    def get_stats(self) -> Dict:
        with self.get_cursor() as cursor:
            cursor.execute('SELECT COUNT(*) as count FROM users')
            total_users = cursor.fetchone()['count']
            
            cursor.execute('SELECT COUNT(*) as count FROM numbers WHERE status = "available"')
            available_numbers = cursor.fetchone()['count']
            
            cursor.execute('SELECT COUNT(*) as count FROM numbers WHERE status = "sold"')
            sold_numbers = cursor.fetchone()['count']
            
            cursor.execute('SELECT COUNT(*) as count FROM numbers WHERE status = "pending"')
            pending_numbers = cursor.fetchone()['count']
            
            cursor.execute('SELECT COUNT(*) as count FROM tg_accounts')
            total_accounts = cursor.fetchone()['count']
            
            cursor.execute('SELECT COUNT(*) as count FROM tg_accounts WHERE status = "active"')
            active_accounts = cursor.fetchone()['count']
            
            cursor.execute('SELECT SUM(amount_stars) as total FROM transactions WHERE status = "completed"')
            total_stars_sold = cursor.fetchone()['total'] or 0
            
            cursor.execute('SELECT COUNT(*) as count FROM transactions WHERE status = "completed"')
            completed_transactions = cursor.fetchone()['count'] or 0
            
            return {
                'total_users': total_users,
                'available_numbers': available_numbers,
                'sold_numbers': sold_numbers,
                'pending_numbers': pending_numbers,
                'total_accounts': total_accounts,
                'active_accounts': active_accounts,
                'total_stars_sold': total_stars_sold,
                'completed_transactions': completed_transactions,
                'total_revenue_rub': total_stars_sold * STAR_TO_RUB
            }

# Инициализация БД
db = Database(DATABASE_FILE)

# ================= УПРАВЛЕНИЕ СЕССИЯМИ TELEGRAM =================

class SessionManager:
    """Класс для управления сессиями Telegram аккаунтов"""
    
    def __init__(self):
        self.active_sessions = {}  # phone -> client
        self.waiting_codes = {}  # phone -> {'number_id': id, 'user_id': id, 'callback': func}
        self.session_watchers = {}  # phone -> task
        self.encryption_key = Fernet.generate_key()
        self.cipher = Fernet(self.encryption_key)
    
    def encrypt_data(self, data: str) -> str:
        """Шифрование данных сессии"""
        return self.cipher.encrypt(data.encode()).decode()
    
    def decrypt_data(self, encrypted_data: str) -> str:
        """Дешифровка данных сессии"""
        return self.cipher.decrypt(encrypted_data.encode()).decode()
    
    async def watch_session(self, phone: str, client: Client):
        """Наблюдение за сессией (бесконечное)"""
        try:
            while True:
                try:
                    # Проверяем, авторизован ли еще клиент
                    if not await client.is_user_authorized():
                        logger.warning(f"⚠️ Сессия {phone} потеряла авторизацию, переподключаемся...")
                        await self.reconnect_session(phone)
                        break
                    
                    # Проверяем, не зашел ли владелец
                    has_owner, owner_id = db.check_account_owner(phone)
                    if has_owner:
                        logger.info(f"👤 Аккаунт {phone} имеет владельца {owner_id}, выходим...")
                        await self.logout_session(phone, "owner_logged_in")
                        break
                    
                    # Ждем перед следующей проверкой
                    await asyncio.sleep(30)
                    
                except Exception as e:
                    logger.error(f"❌ Ошибка в watcher для {phone}: {e}")
                    await asyncio.sleep(60)
        except asyncio.CancelledError:
            logger.info(f"🛑 Watcher для {phone} остановлен")
    
    async def reconnect_session(self, phone: str):
        """Переподключение сессии"""
        try:
            if phone in self.active_sessions:
                old_client = self.active_sessions[phone]
                try:
                    await old_client.disconnect()
                except:
                    pass
                del self.active_sessions[phone]
            
            # Пробуем переподключиться
            new_client = await self.get_client(phone)
            if new_client:
                logger.info(f"✅ Сессия {phone} переподключена")
            else:
                logger.error(f"❌ Не удалось переподключить сессию {phone}")
        except Exception as e:
            logger.error(f"❌ Ошибка переподключения {phone}: {e}")
    
    async def logout_session(self, phone: str, reason: str):
        """Принудительный выход из сессии"""
        try:
            if phone in self.active_sessions:
                client = self.active_sessions[phone]
                
                # Завершаем сессию
                await client.log_out()
                await client.disconnect()
                
                del self.active_sessions[phone]
                
                if phone in self.session_watchers:
                    self.session_watchers[phone].cancel()
                    del self.session_watchers[phone]
                
                db.update_tg_account_status(phone, 'logged_out', f"Причина: {reason}")
                db.log_session_action(phone, 'logout', 'success', reason)
                logger.info(f"✅ Сессия {phone} завершена: {reason}")
        except Exception as e:
            logger.error(f"❌ Ошибка выхода из сессии {phone}: {e}")
    
    async def get_client(self, phone: str) -> Optional[Client]:
        """Получение клиента для аккаунта"""
        # Проверяем, есть ли уже активная сессия
        if phone in self.active_sessions:
            return self.active_sessions[phone]
        
        # Получаем аккаунт из БД
        account = db.get_tg_account(phone)
        if not account:
            logger.error(f"❌ Аккаунт {phone} не найден в БД")
            return None
        
        # Проверяем, не зашел ли владелец
        has_owner, owner_id = db.check_account_owner(phone)
        if has_owner:
            logger.warning(f"⚠️ Аккаунт {phone} имеет владельца {owner_id}, не подключаемся")
            return None
        
        # Создаём нового клиента
        session_path = os.path.join(SESSIONS_DIR, account['session_name'])
        client = Client(
            name=session_path,
            api_id=account['api_id'],
            api_hash=account['api_hash'],
            workdir=SESSIONS_DIR
        )
        
        try:
            await client.connect()
            if await client.is_user_authorized():
                self.active_sessions[phone] = client
                db.update_tg_account_status(phone, 'active')
                db.log_session_action(phone, 'connect', 'success')
                
                # Запускаем наблюдателя
                watcher_task = asyncio.create_task(self.watch_session(phone, client))
                self.session_watchers[phone] = watcher_task
                
                return client
            else:
                await client.disconnect()
                db.update_tg_account_status(phone, 'unauthorized')
                db.log_session_action(phone, 'connect', 'fail', 'not authorized')
                return None
        except (UserDeactivated, SessionRevoked, AuthKeyDuplicated) as e:
            logger.warning(f"⚠️ Аккаунт {phone} деактивирован или сессия сброшена: {e}")
            db.update_tg_account_status(phone, 'deactivated', str(e))
            db.log_session_action(phone, 'connect', 'deactivated', str(e))
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
            # Отправляем запрос на код
            sent_code = await client.send_code(phone)
            
            # Сохраняем информацию о ожидании кода
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
            # Отправляем код
            await client.sign_in(
                phone_number=phone,
                phone_code=code,
                phone_code_hash=wait_info['phone_code_hash']
            )
            
            # Получаем информацию о пользователе
            me = await client.get_me()
            
            # Сохраняем код в БД
            db.set_number_code(wait_info['number_id'], code)
            
            # Обновляем информацию об аккаунте
            db.update_tg_account_status(phone, 'active')
            db.set_tg_account_code(phone, code)
            
            # Устанавливаем владельца аккаунта (пользователь, который купил номер)
            db.set_account_owner(phone, wait_info['user_id'], f"user_{wait_info['user_id']}")
            
            # Удаляем из ожидания
            del self.waiting_codes[phone]
            
            db.log_session_action(phone, 'submit_code', 'success')
            
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
            # Требуется 2FA
            logger.info(f"⚠️ Аккаунт {phone} требует 2FA")
            db.log_session_action(phone, 'submit_code', '2fa_required')
            return {'error': '2fa_required'}
        except PhoneCodeInvalid:
            logger.warning(f"⚠️ Неверный код для {phone}")
            db.log_session_action(phone, 'submit_code', 'invalid_code')
            return {'error': 'invalid_code'}
        except PhoneCodeExpired:
            logger.warning(f"⚠️ Код истёк для {phone}")
            db.log_session_action(phone, 'submit_code', 'code_expired')
            return {'error': 'code_expired'}
        except Exception as e:
            logger.error(f"❌ Ошибка отправки кода для {phone}: {e}")
            db.log_session_action(phone, 'submit_code', 'error', str(e))
            return {'error': str(e)}
    
    async def add_new_account(self, phone: str, api_id: int, api_hash: str, 
                             added_by: int) -> Tuple[bool, str]:
        """Добавление нового аккаунта"""
        try:
            # Проверяем, нет ли уже такого аккаунта
            if db.get_tg_account(phone):
                return False, "Аккаунт уже существует"
            
            # Создаём временную сессию
            session_name = f"acc_{phone.replace('+', '')}_{random.randint(1000, 9999)}"
            session_path = os.path.join(SESSIONS_DIR, session_name)
            
            client = Client(
                name=session_path,
                api_id=api_id,
                api_hash=api_hash,
                workdir=SESSIONS_DIR
            )
            
            await client.connect()
            
            # Запрашиваем код
            sent_code = await client.send_code(phone)
            
            # Сохраняем информацию в БД (предварительно)
            with db.get_cursor() as cursor:
                cursor.execute('''
                    INSERT INTO tg_accounts 
                    (phone, session_name, api_id, api_hash, added_by, added_at, status)
                    VALUES (?, ?, ?, ?, ?, ?, 'pending')
                ''', (phone, session_name, api_id, api_hash, added_by, time.time()))
            
            await client.disconnect()
            
            # Сохраняем информацию для подтверждения
            self.waiting_codes[phone] = {
                'action': 'add_account',
                'phone_code_hash': sent_code.phone_code_hash,
                'client': client,
                'session_name': session_name,
                'timestamp': time.time()
            }
            
            return True, "Код отправлен"
            
        except PhoneNumberInvalid:
            return False, "Неверный номер телефона"
        except FloodWait as e:
            return False, f"Слишком много попыток. Подождите {e.value} сек"
        except Exception as e:
            logger.error(f"❌ Ошибка добавления аккаунта: {e}")
            return False, str(e)
    
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
            
            # Обновляем информацию в БД
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
            
            return True, "Аккаунт успешно добавлен", {
                'id': me.id,
                'first_name': me.first_name,
                'username': me.username
            }
            
        except PhoneCodeInvalid:
            return False, "Неверный код", None
        except SessionPasswordNeeded:
            # Требуется 2FA
            return False, "Требуется двухфакторная аутентификация", None
        except Exception as e:
            logger.error(f"❌ Ошибка подтверждения аккаунта: {e}")
            return False, str(e), None
    
    async def cleanup(self):
        """Очистка неактивных сессий"""
        current_time = time.time()
        to_remove = []
        
        for phone, info in self.waiting_codes.items():
            if current_time - info['timestamp'] > 300:  # 5 минут
                to_remove.append(phone)
        
        for phone in to_remove:
            del self.waiting_codes[phone]
            logger.info(f"🧹 Очищена ожидающая сессия для {phone}")

# Инициализация менеджера сессий
session_manager = SessionManager()

# ================= СОСТОЯНИЯ FSM =================

class BuyStates(StatesGroup):
    waiting_for_username = State()
    waiting_for_code = State()

class AddAccountStates(StatesGroup):
    waiting_for_phone = State()
    waiting_for_code = State()

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
    
    # Добавляем кнопку админки если пользователь админ
    user = db.get_user(user_id) if user_id else None
    if user_id in ADMIN_IDS or (user and user.get('is_admin')):
        keyboard.add(InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin"))
    
    return keyboard

def get_numbers_keyboard(page: int, total_pages: int):
    """Клавиатура для списка номеров с пагинацией"""
    keyboard = InlineKeyboardMarkup(row_width=3)
    
    # Кнопки навигации
    nav_buttons = []
    if page > 1:
        nav_buttons.append(InlineKeyboardButton("◀️", callback_data=f"numbers_page_{page-1}"))
    
    nav_buttons.append(InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data="current_page"))
    
    if page < total_pages:
        nav_buttons.append(InlineKeyboardButton("▶️", callback_data=f"numbers_page_{page+1}"))
    
    keyboard.row(*nav_buttons)
    
    # Кнопки действий
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
        InlineKeyboardButton("◀️ Назад", callback_data="main_menu")
    )
    return keyboard

def get_accounts_keyboard(accounts: List[Dict], page: int = 1):
    """Клавиатура для списка аккаунтов"""
    keyboard = InlineKeyboardMarkup(row_width=1)
    
    for acc in accounts[:5]:
        status_emoji = "✅" if acc['status'] == 'active' else "⏳" if acc['status'] == 'pending' else "❌"
        owner_mark = "👑" if acc.get('owner_checked') and acc.get('owner_id') else ""
        keyboard.add(InlineKeyboardButton(
            f"{status_emoji}{owner_mark} {acc['phone']} | {acc.get('first_name', 'Нет имени')}",
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

# ================= ОБРАБОТЧИКИ КОМАНД =================

@dp.message_handler(commands=['start'])
async def cmd_start(message: Message):
    """Обработчик команды /start"""
    user_id = message.from_user.id
    
    # Проверка токена
    try:
        me = await bot.get_me()
        logger.info(f"✅ Бот авторизован: @{me.username}")
    except Unauthorized:
        logger.error("❌ НЕДЕЙСТВИТЕЛЬНЫЙ ТОКЕН! Получите новый у @BotFather")
        await message.reply("❌ Ошибка авторизации бота. Свяжитесь с администратором.")
        return
    
    # Создаём или получаем пользователя
    user = db.get_user(user_id)
    if not user:
        db.create_user(
            user_id=user_id,
            username=message.from_user.username or f"user_{user_id}",
            first_name=message.from_user.first_name or "Пользователь"
        )
        logger.info(f"✅ Новый пользователь: {user_id}")
    
    db.update_user_activity(user_id)
    
    # Отправляем приветствие
    await message.reply(
        "👋 <b>Добро пожаловать в магазин номеров Telegram!</b>\n\n"
        "📱 Здесь вы можете купить виртуальные номера для Telegram.\n\n"
        "🔹 Пополняйте баланс звёздами\n"
        "🔹 Покупайте номера\n"
        "🔹 Получайте коды подтверждения\n\n"
        "Выберите действие:",
        reply_markup=get_main_keyboard(user_id)
    )

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
    
    # Получаем статистику пользователя
    with db.get_cursor() as cursor:
        cursor.execute('SELECT COUNT(*) as count FROM transactions WHERE user_id = ? AND status = "completed"', 
                      (user_id,))
        purchases = cursor.fetchone()['count'] or 0
    
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
"""
    
    keyboard = InlineKeyboardMarkup().add(
        InlineKeyboardButton("🏠 Главное меню", callback_data="main_menu")
    )
    
    await callback.message.edit_text(text, reply_markup=keyboard)

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
        # Выбираем флаг по стране
        flag = "🇷🇺" if num['country'].lower() in ['россия', 'russia'] else "🌍"
        
        text += f"{flag} <b>{num['country']}</b>\n"
        text += f"📞 <code>{num['phone_number']}</code>\n"
        text += f"📝 {num['description']}\n"
        text += f"💰 <b>{num['price_stars']} ⭐️</b> ({num['price_rub']:.0f}₽)\n"
        text += f"🔹 <b>ID:</b> {num['id']}\n\n"
    
    # Добавляем инструкцию
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
    
    # Проверяем номер
    number = db.get_number(number_id)
    
    if not number:
        await message.reply("❌ Номер не найден")
        return
    
    if number['status'] != 'available':
        await message.reply("❌ Номер уже недоступен")
        return
    
    # Проверяем баланс
    user = db.get_user(user_id)
    if not user:
        await message.reply("❌ Сначала запустите бота командой /start")
        return
    
    # Сохраняем в состояние
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
    
    # Создаём платеж
    payment_id = str(uuid.uuid4())
    payment_url = await YooMoneyPayment.create_payment(
        amount=number['price_rub'],
        payment_id=payment_id,
        description=f"Покупка номера {number['phone_number']}"
    )
    
    if payment_url:
        # Сохраняем информацию о платеже
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
    
    # Создаём платеж
    payment_id = str(uuid.uuid4())
    payment_url = await CryptoBotPayment.create_payment(
        amount=number['price_rub'],
        payment_id=payment_id,
        description=f"Покупка номера {number['phone_number']}"
    )
    
    if payment_url:
        # Сохраняем информацию о платеже
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
    
    with db.get_cursor() as cursor:
        cursor.execute('SELECT * FROM payments WHERE id = ?', (payment_id,))
        payment = cursor.fetchone()
    
    if not payment:
        await callback.message.edit_text("❌ Платёж не найден")
        return
    
    if payment['status'] == 'completed':
        await callback.message.edit_text("✅ Платёж уже обработан!")
        return
    
    # Завершаем платеж (для демо - сразу)
    with db.get_cursor() as cursor:
        # Обновляем статус платежа
        cursor.execute('''
            UPDATE payments SET status = 'completed', completed_at = ? WHERE id = ?
        ''', (time.time(), payment_id))
        
        # Начисляем звёзды пользователю
        cursor.execute('''
            UPDATE users SET stars_balance = stars_balance + ? WHERE user_id = ?
        ''', (payment['stars_amount'], payment['user_id']))
        
        # Обновляем статус транзакции
        cursor.execute('''
            UPDATE transactions SET status = 'completed', completed_at = ? 
            WHERE user_id = ? AND number_id = ?
        ''', (time.time(), payment['user_id'], payment['number_id']))
        
        # Получаем обновленный баланс
        cursor.execute('SELECT stars_balance FROM users WHERE user_id = ?', (payment['user_id'],))
        new_balance = cursor.fetchone()['stars_balance']
    
    logger.info(f"✅ Платеж {payment_id} завершен, пользователь {payment['user_id']} получил {payment['stars_amount']} звёзд")
    
    # Получаем аккаунт для отправки кода
    account = db.get_available_tg_account()
    if account:
        # Запрашиваем код
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
    
    # Отправляем код
    result = await session_manager.submit_code(phone, code)
    
    if result and 'code' in result:
        # Получаем номер
        number = db.get_number(number_id)
        
        # Удаляем номер из магазина (он больше не доступен)
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
        
        # Отправляем уведомление админу
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
        await message.reply(
            "❌ Аккаунт требует двухфакторную аутентификацию.\n"
            "Обратитесь к администратору для ручной выдачи кода.",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("👤 Профиль", callback_data="profile")
            )
        )
        await state.finish()
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

# ================= АДМИН-ПАНЕЛЬ =================

@dp.callback_query_handler(lambda c: c.data == 'admin')
async def admin_panel(callback: CallbackQuery):
    """Открыть админ-панель"""
    await callback.answer()
    
    if callback.from_user.id not in ADMIN_IDS:
        await callback.message.edit_text("⛔ У вас нет доступа к админ-панели")
        return
    
    stats = db.get_stats()
    
    # Получаем информацию о системе
    cpu_percent = psutil.cpu_percent(interval=1)
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage('/')
    
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
• ⏱ Uptime: {timedelta(seconds=int(time.time() - start_time))}

Выберите действие:
"""
    
    await callback.message.edit_text(text, reply_markup=get_admin_keyboard())

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
        text += f"{status_emoji}{owner_mark} <b>{acc['phone']}</b>\n"
        text += f"   👤 Имя: {acc.get('first_name', 'Нет имени')}\n"
        text += f"   📊 Статус: {acc['status']}\n"
        if acc.get('owner_id'):
            text += f"   👑 Владелец: {acc['owner_id']}\n"
        if acc.get('last_code'):
            text += f"   🔑 Последний код: {acc['last_code']}\n"
        text += f"   📅 Добавлен: {datetime.fromtimestamp(acc['added_at']).strftime('%d.%m.%Y')}\n\n"
    
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
    
    # Добавляем аккаунт
    success, msg = await session_manager.add_new_account(
        phone=phone,
        api_id=API_ID,
        api_hash=API_HASH,
        added_by=message.from_user.id
    )
    
    if success:
        await state.update_data(phone=phone)
        await message.reply(
            f"✅ {msg}\n\n📲 Введите код подтверждения из Telegram:",
            reply_markup=get_back_keyboard("admin")
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
    
    with db.get_cursor() as cursor:
        cursor.execute('''
            SELECT * FROM numbers ORDER BY id DESC LIMIT 20
        ''')
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
    
    # Добавляем номер в БД
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
    
    # Получаем дополнительную статистику
    with db.get_cursor() as cursor:
        cursor.execute('SELECT COUNT(*) as count FROM transactions WHERE status = "completed"')
        completed_transactions = cursor.fetchone()['count'] or 0
        
        cursor.execute('SELECT COUNT(*) as count FROM transactions WHERE date(created_at, "unixepoch") = date("now")')
        today_transactions = cursor.fetchone()['count'] or 0
        
        cursor.execute('SELECT SUM(amount_stars) as total FROM transactions WHERE status = "completed"')
        total_stars_sold = cursor.fetchone()['total'] or 0
        
        cursor.execute('SELECT AVG(amount_stars) as avg FROM transactions WHERE status = "completed"')
        avg_price = cursor.fetchone()['avg'] or 0
    
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
    except:
        await message.reply("❌ Введите числовой ID")
        return
    
    user = db.get_user(user_id)
    if not user:
        await message.reply("❌ Пользователь не найден")
        await state.finish()
        return
    
    await state.update_data(target_user_id=user_id)
    
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
            raise ValueError
    except:
        await message.reply("❌ Введите положительное число")
        return
    
    data = await state.get_data()
    user_id = data['target_user_id']
    
    if db.add_stars(user_id, amount):
        # Уведомляем пользователя
        try:
            await bot.send_message(
                user_id,
                f"🎁 <b>Вам начислено {amount} ⭐️!</b>\n\n"
                f"💰 Новый баланс: {db.get_user(user_id)['stars_balance']} ⭐️"
            )
        except Exception as e:
            logger.warning(f"Не удалось уведомить пользователя {user_id}: {e}")
        
        await message.reply(
            f"✅ <b>Звёзды успешно выданы!</b>\n\n"
            f"👤 Пользователь: {user_id}\n"
            f"➕ Добавлено: {amount} ⭐️\n"
            f"💰 Новый баланс: {db.get_user(user_id)['stars_balance']} ⭐️",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin"),
                InlineKeyboardButton("🎁 Выдать ещё", callback_data="admin_add_stars")
            )
        )
        logger.info(f"✅ Админ {message.from_user.id} выдал {amount} звёзд пользователю {user_id}")
    else:
        await message.reply(
            "❌ Ошибка при выдаче звёзд",
            reply_markup=InlineKeyboardMarkup().add(
                InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin")
            )
        )
    
    await state.finish()

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
        'database': 'connected',
        'stats': db.get_stats()
    }
    return web.json_response(health_data)

async def payment_webhook(request):
    """Webhook для получения уведомлений о платежах"""
    try:
        data = await request.json()
        logger.info(f"📩 Webhook получен: {data}")
        
        # Обработка платежей от Crypto Bot
        if data.get('payload'):
            payment_id = data['payload']
            if data.get('status') == 'paid':
                with db.get_cursor() as cursor:
                    cursor.execute('SELECT * FROM payments WHERE id = ?', (payment_id,))
                    payment = cursor.fetchone()
                    
                    if payment and payment['status'] == 'pending':
                        # Завершаем платеж
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

# ================= ЗАПУСК =================

# Время запуска для расчета uptime
start_time = time.time()

async def cleanup_task():
    """Периодическая очистка сессий"""
    while True:
        try:
            await session_manager.cleanup()
            
            # Очищаем старые логи
            with db.get_cursor() as cursor:
                week_ago = time.time() - 7 * 24 * 3600
                cursor.execute('DELETE FROM session_logs WHERE created_at < ?', (week_ago,))
        except Exception as e:
            logger.error(f"❌ Ошибка в cleanup_task: {e}")
        
        await asyncio.sleep(60)  # Каждую минуту

async def stats_logger():
    """Периодическое логирование статистики"""
    while True:
        try:
            stats = db.get_stats()
            
            # Получаем информацию о системе
            cpu_percent = psutil.cpu_percent(interval=1)
            memory = psutil.virtual_memory()
            
            logger.info(f"📊 Статистика: Users={stats['total_users']}, "
                       f"Numbers={stats['available_numbers']}, "
                       f"Sold={stats['sold_numbers']}, "
                       f"Accounts={stats['active_accounts']}, "
                       f"CPU={cpu_percent}%, RAM={memory.percent}%")
        except Exception as e:
            logger.error(f"❌ Ошибка в stats_logger: {e}")
        
        await asyncio.sleep(3600)  # Каждый час

async def on_startup(dp):
    """Действия при запуске бота"""
    global start_time
    start_time = time.time()
    
    logger.info("🚀 Бот запускается...")
    
    # Проверка токена
    try:
        me = await bot.get_me()
        logger.info(f"✅ Бот авторизован: @{me.username} (ID: {me.id})")
    except Unauthorized:
        logger.error("❌ НЕДЕЙСТВИТЕЛЬНЫЙ ТОКЕН! Получите новый у @BotFather")
        return
    
    # Проверка папок
    logger.info(f"📁 Папка сессий: {SESSIONS_DIR}")
    logger.info(f"📁 Папка бекапов: {DATABASE_BACKUP_DIR}")
    
    # Запускаем фоновые задачи
    asyncio.create_task(web_server())
    asyncio.create_task(cleanup_task())
    asyncio.create_task(stats_logger())
    
    # Подсчет статистики
    stats = db.get_stats()
    logger.info(f"📊 Начальная статистика: Users={stats['total_users']}, "
                f"Numbers={stats['available_numbers']}, Accounts={stats['total_accounts']}")
    
    # Уведомление админам
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
                f"• Python: {sys.version.split()[0]}\n"
                f"• API ID: {API_ID}"
            )
        except Exception as e:
            logger.error(f"Не удалось отправить уведомление админу {admin_id}: {e}")
    
    logger.info("✅ Бот готов к работе")

async def on_shutdown(dp):
    """Действия при остановке бота"""
    logger.info("🛑 Бот останавливается...")
    
    # Закрываем все активные сессии
    closed_sessions = 0
    for phone, client in session_manager.active_sessions.items():
        try:
            await client.disconnect()
            closed_sessions += 1
        except Exception as e:
            logger.error(f"❌ Ошибка при закрытии сессии {phone}: {e}")
    
    logger.info(f"✅ Закрыто активных сессий: {closed_sessions}")
    
    # Создаем финальный бекап
    try:
        backup_file = os.path.join(DATABASE_BACKUP_DIR, f"final_backup_{int(time.time())}.db")
        shutil.copy2(DATABASE_FILE, backup_file)
        logger.info(f"✅ Создан финальный бекап: {backup_file}")
    except Exception as e:
        logger.error(f"❌ Ошибка создания финального бекапа: {e}")
    
    # Уведомление админам
    uptime = time.time() - start_time
    uptime_str = str(timedelta(seconds=int(uptime)))
    
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(
                admin_id,
                f"🛑 <b>Бот остановлен</b>\n\n"
                f"⏱ Время работы: {uptime_str}\n"
                f"✅ Финальный бекап создан"
            )
        except:
            pass
    
    logger.info(f"✅ Бот остановлен. Время работы: {uptime_str}")

if __name__ == "__main__":
    print("=" * 70)
    print("🚀 Telegram Numbers Shop Bot v8.0 - ФИНАЛЬНАЯ ВЕРСИЯ")
    print("📱 Управление сессиями + Автоудаление номеров + Вечные сессии")
    print("=" * 70)
    print(f"✅ API ID: {API_ID}")
    print(f"✅ API Hash: {API_HASH[:10]}...")
    print(f"✅ Admin ID: {ADMIN_IDS[0]}")
    print(f"✅ Port: {PORT}")
    print(f"✅ Database: {DATABASE_FILE}")
    print(f"✅ Sessions dir: {SESSIONS_DIR}")
    print("=" * 70)
    print("⚠️  Убедитесь, что папки 'sessions' и 'backups' существуют")
    print("⚠️  Проверьте правильность токена бота")
    print("=" * 70)
    
    executor.start_polling(
        dp,
        skip_updates=True,
        on_startup=on_startup,
        on_shutdown=on_shutdown
)
