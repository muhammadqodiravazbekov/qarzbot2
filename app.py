import sqlite3
import logging
import csv
import io
import re
import unicodedata
import os
import asyncio
from datetime import datetime
from typing import List, Dict, Optional
from flask import Flask, request, jsonify, render_template_string, Response
from telegram import Update, Bot
from telegram.ext import (
    Application, CommandHandler, ConversationHandler,
    MessageHandler, CallbackQueryHandler, filters, ContextTypes
)
from telegram.request import HTTPXRequest

# ---------- Configuration ----------
BOT_TOKEN = os.environ.get('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required!")
DATABASE_PATH = os.environ.get('DATABASE_PATH', 'debts.db')
WEBHOOK_URL = f"https://qarzbot2-1.onrender.com/{BOT_TOKEN}"

# ---------- Flask Web Server ----------
flask_app = Flask(__name__)

# ---------- Bot Initialization ----------
# We initialize the PTB Application globally
req = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
ptb = Application.builder().token(BOT_TOKEN).request(req).build()

# Create a global event loop for the webhooks to use
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

# ---------- Database Functions (Your original logic) ----------
def normalize_text(text: str) -> str:
    if not text: return ""
    cyrillic_to_latin = {
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo',
        'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'y', 'к': 'k', 'л': 'l', 'м': 'm',
        'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
        'ф': 'f', 'х': 'kh', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'shch',
        'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
        'ў': 'o\'', 'қ': 'q', 'ғ': 'g\'', 'ҳ': 'h', 'нг': 'ng'
    }
    normalized = text.lower()
    for cyr, lat in cyrillic_to_latin.items():
        normalized = normalized.replace(cyr, lat)
    normalized = unicodedata.normalize('NFKD', normalized).encode('ASCII', 'ignore').decode('ASCII')
    return re.sub(r'[^a-z0-9]', '', normalized)

def get_db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (telegram_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, role TEXT CHECK(role IN ('admin','seller','viewer')) NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS debts (id INTEGER PRIMARY KEY AUTOINCREMENT, customer_name TEXT NOT NULL, customer_name_normalized TEXT, phone TEXT, amount_owed REAL NOT NULL, remaining_balance REAL NOT NULL, notes TEXT, seller_telegram_id INTEGER NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (seller_telegram_id) REFERENCES users(telegram_id))''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_debt_name_normalized ON debts(customer_name_normalized)')
    cursor.execute('CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY AUTOINCREMENT, debt_id INTEGER NOT NULL, amount_paid REAL NOT NULL, payment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP, notes TEXT, FOREIGN KEY (debt_id) REFERENCES debts(id) ON DELETE CASCADE)')
    conn.commit()
    conn.close()

# Include your existing helper functions here (get_user, create_user, add_debt, etc...)
# [INSERT YOUR DATA FUNCTIONS HERE]
def get_user(telegram_id: int) -> Optional[Dict]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id, username, first_name, role FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    if row: return {"telegram_id": row[0], "username": row[1], "first_name": row[2], "role": row[3]}
    return None

def create_user(telegram_id: int, username: str, first_name: str, role: str) -> bool:
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO users (telegram_id, username, first_name, role) VALUES (?, ?, ?, ?)", (telegram_id, username, first_name, role))
        conn.commit()
        return True
    except: return False
    finally: conn.close()

def get_all_users() -> List[Dict]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id, username, first_name, role FROM users")
    rows = cursor.fetchall()
    conn.close()
    return [{"telegram_id": r[0], "username": r[1], "first_name": r[2], "role": r[3]} for r in rows]

def get_admins_and_sellers() -> List[Dict]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id, username, first_name, role FROM users WHERE role IN ('admin','seller')")
    rows = cursor.fetchall()
    conn.close()
    return [{"telegram_id": r[0], "username": r[1], "first_name": r[2], "role": r[3]} for r in rows]

def add_debt(customer_name: str, phone: str, amount: float, notes: str, seller_telegram_id: int) -> int:
    norm_name = normalize_text(customer_name)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO debts (customer_name, customer_name_normalized, phone, amount_owed, remaining_balance, notes, seller_telegram_id) VALUES (?, ?, ?, ?, ?, ?, ?)", (customer_name, norm_name, phone, amount, amount, notes, seller_telegram_id))
    debt_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return debt_id

def get_debt(debt_id: int) -> Optional[Dict]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, customer_name, phone, amount_owed, remaining_balance, notes, seller_telegram_id FROM debts WHERE id = ?", (debt_id,))
    row = cursor.fetchone()
    conn.close()
    if row: return {"id": row[0], "customer_name": row[1], "phone": row[2], "amount_owed": row[3], "remaining_balance": row[4], "notes": row[5], "seller_telegram_id": row[6]}
    return None

def delete_debt(debt_id: int) -> bool:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM debts WHERE id = ?", (debt_id,))
    conn.commit()
    affected = cursor.rowcount
    conn.close()
    return affected > 0

def add_payment(debt_id: int, amount: float, notes: str = "") -> bool:
    debt = get_debt(debt_id)
    if not debt or amount <= 0 or amount > debt["remaining_balance"]: return False
    new_balance = debt["remaining_balance"] - amount
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO payments (debt_id, amount_paid, notes) VALUES (?, ?, ?)", (debt_id, amount, notes))
    cursor.execute("UPDATE debts SET remaining_balance = ?, updated_at = ? WHERE id = ?", (new_balance, datetime.now().isoformat(), debt_id))
    conn.commit()
    conn.close()
    return True

def get_all_debts() -> List[Dict]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT d.id, d.customer_name, d.phone, d.amount_owed, d.remaining_balance, d.notes, d.seller_telegram_id, d.created_at, u.username, u.first_name FROM debts d JOIN users u ON d.seller_telegram_id = u.telegram_id ORDER BY d.remaining_balance DESC, d.created_at DESC")
    rows = cursor.fetchall()
    conn.close()
    return [{"id": r[0], "customer_name": r[1], "phone": r[2], "amount_owed": r[3], "remaining_balance": r[4], "notes": r[5], "seller_telegram_id": r[6], "created_at": r[7], "seller_name": r[8] or r[9] or str(r[6])} for r in rows]

def get_total_outstanding() -> float:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COALESCE(SUM(remaining_balance), 0) FROM debts")
    total = cursor.fetchone()[0]
    conn.close()
    return total

# ---------- Bot Handlers ----------
USER_ID, USER_ROLE = range(2)

# Paste your 'get_main_keyboard', 'get_users_menu', 'start', 'menu_handler', 'handle_text_input' logic here.
# (I am providing placeholders, make sure your full logic is here)
def get_main_keyboard(role: str):
    app_host = os.environ.get('RENDER_EXTERNAL_HOSTNAME', 'qarzbot2-1.onrender.com')
    webapp_url = f"https://{app_host}/webapp"
    keyboard = [[InlineKeyboardButton("📱 Ilovani ochish (Mini App)", web_app={"url": webapp_url})]]
    if role == "admin": keyboard.append([InlineKeyboardButton("👥 Xodimlarni boshqarish", callback_data="menu_users")])
    return {"inline_keyboard": keyboard}

# ... (Continue pasting your logic from your file here) ...
# Ensure you register handlers to 'ptb'
ptb.add_handler(CommandHandler("start", start)) 
# etc...

# ---------- Flask Webhook Route ----------
@flask_app.route('/' + BOT_TOKEN, methods=['POST'])
def webhook():
    data = request.get_json(force=True)
    update = Update.de_json(data, ptb.bot)
    loop.run_until_complete(ptb.process_update(update))
    return 'OK', 200

# ... (Include all your other @flask_app routes: /webapp, /api/dashboard, etc) ...

if __name__ == "__main__":
    init_db()
    loop.run_until_complete(ptb.initialize())
    loop.run_until_complete(ptb.bot.set_webhook(url=WEBHOOK_URL))
    flask_app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
