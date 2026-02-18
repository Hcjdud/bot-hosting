"""
Скрипт для ручного создания базы данных
Запустите: python create_db.py
"""

import sqlite3
import os
import time

# Настройки (такие же как в bot.py)
DATABASE_FILE = "shop.db"

def create_database():
    """Создание базы данных вручную"""
    
    # Удаляем старую БД если есть
    if os.path.exists(DATABASE_FILE):
        os.remove(DATABASE_FILE)
        print(f"🗑 Удалена старая БД: {DATABASE_FILE}")
    
    # Подключаемся (файл создастся автоматически)
    conn = sqlite3.connect(DATABASE_FILE)
    cursor = conn.cursor()
    
    print("🚀 Создание базы данных...")
    
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
    print("✅ Таблица users создана")
    
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
    print("✅ Таблица tg_accounts создана")
    
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
    print("✅ Таблица numbers создана")
    
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
    print("✅ Таблица transactions создана")
    
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
    print("✅ Таблица payments создана")
    
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
    print("✅ Таблица session_logs создана")
    
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
    print("✅ Таблица system_logs создана")
    
    # Сохраняем изменения
    conn.commit()
    
    # Проверяем, что все таблицы созданы
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = cursor.fetchall()
    print(f"\n📊 Созданы таблицы: {[t[0] for t in tables]}")
    
    # Добавляем тестовые данные (опционально)
    print("\n🧪 Добавляем тестовые данные...")
    
    # Добавляем админа
    cursor.execute('''
        INSERT OR IGNORE INTO users (user_id, username, first_name, registered_at, last_activity, is_admin)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', (8443743937, "admin", "Admin", time.time(), time.time(), 1))
    
    # Добавляем тестовый номер
    cursor.execute('''
        INSERT OR IGNORE INTO numbers (phone_number, country, description, price_stars, price_rub, status)
        VALUES (?, ?, ?, ?, ?, ?)
    ''', ("+79001234567", "Россия", "Тестовый номер", 100, 150, "available"))
    
    conn.commit()
    print("✅ Тестовые данные добавлены")
    
    conn.close()
    
    # Проверяем размер файла
    size = os.path.getsize(DATABASE_FILE)
    print(f"\n📁 Файл БД: {DATABASE_FILE}")
    print(f"📏 Размер: {size} байт ({size/1024:.2f} КБ)")
    print("✅ База данных успешно создана!")

if __name__ == "__main__":
    create_database()
