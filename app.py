# Import the standard library and third-party modules
import psycopg2
import logging
import csv
import io
import re
import unicodedata
import threading
import os
import asyncio
from datetime import datetime
from typing import List, Dict, Optional
from flask import Flask, request, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ConversationHandler,
    MessageHandler, CallbackQueryHandler, filters, ContextTypes
)
from telegram.request import HTTPXRequest

# ---------- Flask Web Server ----------
flask_app = Flask(__name__)

@flask_app.route('/')
@flask_app.route('/health')
def health():
    return jsonify({"status": "alive", "message": "Bot is running!"}), 200

# ---------- Configuration ----------
BOT_TOKEN = os.environ.get('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required!")

DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    raise ValueError("DATABASE_URL environment variable is required!")

# ---------- Helper Functions ----------
def normalize_text(text: str) -> str:
    """Normalize text for case-insensitive search."""
    if not text:
        return ""
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
    normalized = re.sub(r'[^a-z0-9]', '', normalized)
    return normalized

# ---------- Database Setup ----------
def get_db():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    """Set up the cloud PostgreSQL database schemas and handle automatic updates."""
    conn = get_db()
    cursor = conn.cursor()
    
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            telegram_id BIGINT PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            role TEXT CHECK(role IN ('admin','seller','viewer')) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    
    # Schema Migration: Safely ensure first_name column exists if an old schema is present
    try:
        cursor.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name TEXT;")
    except Exception:
        conn.rollback()
        cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS debts (
            id SERIAL PRIMARY KEY,
            customer_name TEXT NOT NULL,
            customer_name_normalized TEXT,
            phone TEXT,
            amount_owed REAL NOT NULL,
            remaining_balance REAL NOT NULL,
            notes TEXT,
            seller_telegram_id BIGINT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (seller_telegram_id) REFERENCES users(telegram_id)
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_debt_name_normalized ON debts(customer_name_normalized)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_debt_phone ON debts(phone)')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS payments (
            id SERIAL PRIMARY KEY,
            debt_id INTEGER NOT NULL,
            amount_paid REAL NOT NULL,
            payment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            notes TEXT,
            FOREIGN KEY (debt_id) REFERENCES debts(id) ON DELETE CASCADE
        )
    ''')
    cursor.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)')
    conn.commit()
    conn.close()

# ---------- Database Functions ----------
def get_user(telegram_id: int) -> Optional[Dict]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id, username, first_name, role FROM users WHERE telegram_id = %s", (telegram_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"telegram_id": row[0], "username": row[1], "first_name": row[2], "role": row[3]}
    return None

def create_user(telegram_id: int, username: str, first_name: str, role: str) -> bool:
    conn = get_db()
    try:
        cursor = conn.cursor()
        cursor.execute("INSERT INTO users (telegram_id, username, first_name, role) VALUES (%s, %s, %s, %s)",
                       (telegram_id, username, first_name, role))
        conn.commit()
        return True
    except psycopg2.IntegrityError:
        return False
    finally:
        conn.close()

def delete_user(telegram_id: int) -> bool:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE telegram_id = %s", (telegram_id,))
    conn.commit()
    affected = cursor.rowcount
    conn.close()
    return affected > 0

def get_all_users() -> List[Dict]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT telegram_id, username, first_name, role, created_at FROM users ORDER BY created_at")
    rows = cursor.fetchall()
    conn.close()
    return [{"telegram_id": r[0], "username": r[1], "first_name": r[2], "role": r[3], "created_at": r[4]} for r in rows]

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
    cursor.execute(
        "INSERT INTO debts (customer_name, customer_name_normalized, phone, amount_owed, remaining_balance, notes, seller_telegram_id) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
        (customer_name, norm_name, phone, amount, amount, notes, seller_telegram_id)
    )
    debt_id = cursor.fetchone()[0]
    conn.commit()
    conn.close()
    return debt_id

def get_debt(debt_id: int) -> Optional[Dict]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT id, customer_name, phone, amount_owed, remaining_balance, notes, seller_telegram_id, created_at, updated_at FROM debts WHERE id = %s", (debt_id,))
    row = cursor.fetchone()
    conn.close()
    if row:
        return {"id": row[0], "customer_name": row[1], "phone": row[2], "amount_owed": row[3], "remaining_balance": row[4], "notes": row[5], "seller_telegram_id": row[6], "created_at": row[7], "updated_at": row[8]}
    return None

def update_debt(debt_id: int, **kwargs) -> bool:
    allowed_fields = {"customer_name", "phone", "amount_owed", "remaining_balance", "notes"}
    updates = {k: v for k, v in kwargs.items() if k in allowed_fields}
    if not updates:
        return False
    if "customer_name" in updates:
        updates["customer_name_normalized"] = normalize_text(updates["customer_name"])
    updates["updated_at"] = datetime.now()
    set_clause = ", ".join([f"{key} = %s" for key in updates.keys()])
    values = list(updates.values()) + [debt_id]
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(f"UPDATE debts SET {set_clause} WHERE id = %s", values)
    conn.commit()
    affected = cursor.rowcount
    conn.close()
    return affected > 0

def delete_debt(debt_id: int) -> bool:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM debts WHERE id = %s", (debt_id,))
    conn.commit()
    affected = cursor.rowcount
    conn.close()
    return affected > 0

def add_payment(debt_id: int, amount: float, notes: str = "") -> bool:
    debt = get_debt(debt_id)
    if not debt or amount <= 0 or amount > debt["remaining_balance"]:
        return False
    new_balance = debt["remaining_balance"] - amount
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO payments (debt_id, amount_paid, notes) VALUES (%s, %s, %s)", (debt_id, amount, notes))
    cursor.execute("UPDATE debts SET remaining_balance = %s, updated_at = %s WHERE id = %s", (new_balance, datetime.now(), debt_id))
    conn.commit()
    conn.close()
    return True

def search_debts(query: str) -> List[Dict]:
    norm_query = normalize_text(query)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT d.id, d.customer_name, d.phone, d.amount_owed, d.remaining_balance, d.notes,
               d.seller_telegram_id, d.created_at, d.updated_at, u.username, u.first_name
        FROM debts d
        JOIN users u ON d.seller_telegram_id = u.telegram_id
        WHERE d.phone LIKE %s OR d.customer_name_normalized LIKE %s
        ORDER BY d.created_at DESC
    """, (f"%{query}%", f"%{norm_query}%"))
    rows = cursor.fetchall()
    conn.close()
    return [{"id": r[0], "customer_name": r[1], "phone": r[2], "amount_owed": r[3], "remaining_balance": r[4], "notes": r[5], "seller_telegram_id": r[6], "created_at": r[7], "updated_at": r[8], "seller_name": r[9] or r[10] or str(r[6])} for r in rows]

def get_all_debts(filters: Dict = None) -> List[Dict]:
    query = """
        SELECT d.id, d.customer_name, d.phone, d.amount_owed, d.remaining_balance, d.notes,
               d.seller_telegram_id, d.created_at, d.updated_at, u.username, u.first_name
        FROM debts d
        JOIN users u ON d.seller_telegram_id = u.telegram_id
    """
    conditions = []
    params = []
    if filters:
        if filters.get("seller_id"):
            conditions.append("d.seller_telegram_id = %s")
            params.append(filters["seller_id"])
        if filters.get("customer_name"):
            norm_name = normalize_text(filters["customer_name"])
            conditions.append("d.customer_name_normalized LIKE %s")
            params.append(f"%{norm_name}%")
        if filters.get("phone"):
            conditions.append("d.phone LIKE %s")
            params.append(f"%{filters['phone']}%")
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY d.created_at DESC"
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    return [{"id": r[0], "customer_name": r[1], "phone": r[2], "amount_owed": r[3], "remaining_balance": r[4], "notes": r[5], "seller_telegram_id": r[6], "created_at": r[7], "updated_at": r[8], "seller_name": r[9] or r[10] or str(r[6])} for r in rows]

def get_total_outstanding() -> float:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("SELECT COALESCE(SUM(remaining_balance), 0) FROM debts")
    total = cursor.fetchone()[0]
    conn.close()
    return total

def get_outstanding_by_seller() -> List[Dict]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT u.telegram_id, u.username, u.first_name, COALESCE(SUM(d.remaining_balance), 0)
        FROM users u
        LEFT JOIN debts d ON u.telegram_id = d.seller_telegram_id
        WHERE u.role IN ('admin','seller')
        GROUP BY u.telegram_id, u.username, u.first_name
        ORDER BY SUM(d.remaining_balance) DESC
    """)
    rows = cursor.fetchall()
    conn.close()
    return [{"seller_id": r[0], "name": r[1] or r[2] or str(r[0]), "total": r[3]} for r in rows]

def get_largest_debtors(limit: int = 5) -> List[Dict]:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT customer_name, phone, SUM(remaining_balance) as total_balance
        FROM debts
        GROUP BY customer_name, phone
        ORDER BY total_balance DESC
        LIMIT %s
    """, (limit,))
    rows = cursor.fetchall()
    conn.close()
    return [{"name": r[0], "phone": r[1], "total": r[2]} for r in rows]

# ---------- Telegram Handlers ----------
NAME, PHONE, AMOUNT, NOTES, DEBT_ID, PAY_AMOUNT, EDIT_FIELD, EDIT_VALUE, SEARCH_QUERY, USER_ID, USER_ROLE = range(11)

def get_main_keyboard(role: str):
    if role == "admin":
        keyboard = [
            [InlineKeyboardButton("➕ Қарз қўшиш", callback_data="menu_adddebt")],
            [InlineKeyboardButton("💰 Тўлов қабул қилиш", callback_data="menu_pay")],
            [InlineKeyboardButton("✏️ Қарзни таҳрирлаш", callback_data="menu_editdebt")],
            [InlineKeyboardButton("🗑️ Қарзни ўчириш", callback_data="menu_deletedebt")],
            [InlineKeyboardButton("🔍 Қарзларни излаш", callback_data="menu_search")],
            [InlineKeyboardButton("📋 Барча қарзлар", callback_data="menu_listdebts")],
            [InlineKeyboardButton("📊 Статистика", callback_data="menu_stats")],
            [InlineKeyboardButton("📁 CSV экспорт", callback_data="menu_export")],
            [InlineKeyboardButton("👥 Фойдаланувчилар", callback_data="menu_users")],
            [InlineKeyboardButton("❌ Бекор қилиш", callback_data="menu_cancel")]
        ]
    elif role == "seller":
        keyboard = [
            [InlineKeyboardButton("➕ Қарз қўшиш", callback_data="menu_adddebt")],
            [InlineKeyboardButton("💰 Тўлов қабул қилиш", callback_data="menu_pay")],
            [InlineKeyboardButton("✏️ Қарзни таҳрирлаш", callback_data="menu_editdebt")],
            [InlineKeyboardButton("🗑️ Қарзни ўчириш", callback_data="menu_deletedebt")],
            [InlineKeyboardButton("🔍 Қарзларни излаш", callback_data="menu_search")],
            [InlineKeyboardButton("📋 Барча қарзлар", callback_data="menu_listdebts")],
            [InlineKeyboardButton("📊 Статистика", callback_data="menu_stats")],
            [InlineKeyboardButton("📁 CSV экспорт", callback_data="menu_export")],
            [InlineKeyboardButton("❌ Бекор қилиш", callback_data="menu_cancel")]
        ]
    else:
        keyboard = [
            [InlineKeyboardButton("🔍 Қарзларни излаш", callback_data="menu_search")],
            [InlineKeyboardButton("📋 Барча қарзлар", callback_data="menu_listdebts")],
            [InlineKeyboardButton("📊 Статистика", callback_data="menu_stats")],
            [InlineKeyboardButton("📁 CSV экспорт", callback_data="menu_export")],
            [InlineKeyboardButton("❌ Бекор қилиш", callback_data="menu_cancel")]
        ]
    return InlineKeyboardMarkup(keyboard)

def get_users_menu():
    keyboard = [
        [InlineKeyboardButton("➕ Фойдаланувчи қўшиш", callback_data="menu_adduser")],
        [InlineKeyboardButton("❌ Фойдаланувчини ўчириш", callback_data="menu_removeuser")],
        [InlineKeyboardButton("📋 Фойдаланувчилар рўйхати", callback_data="menu_listusers")],
        [InlineKeyboardButton("🔙 Орқага", callback_data="menu_back")]
    ]
    return InlineKeyboardMarkup(keyboard)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    telegram_id = user.id
    username = user.username or ""
    first_name = user.first_name or ""
    db_user = get_user(telegram_id)
    if not db_user:
        admins = [u for u in get_admins_and_sellers() if u["role"] == "admin"]
        if not admins:
            create_user(telegram_id, username, first_name, "admin")
            db_user = get_user(telegram_id)
            await update.message.reply_text(
                f"✅ Ассалому алайкум {first_name}! Сиз биринчи фойдаланувчисиз ва **АДМИН** этиб белгиландингиз.\n\n"
                f"Қарзларни бошқариш учун тугмалардан фойдаланинг:",
                reply_markup=get_main_keyboard("admin")
            )
        else:
            await update.message.reply_text(
                "❌ Кириш ҳуқуқи йўқ. Сиз рўйхатдан ўтмагансиз.\n"
                "Супермаркет администраторига мурожаат қилинг."
            )
        return
    await update.message.reply_text(
        f"✅ Ассалому алайкум {first_name}! Сизнинг ролингиз: **{db_user['role'].upper()}**\n\n"
        f"Амални танланг:",
        reply_markup=get_main_keyboard(db_user['role'])
    )

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    db_user = get_user(user_id)
    if not db_user:
        await query.edit_message_text("Ҳуқуқингиз йўқ. Админга мурожаат қилинг.")
        return
    
    action = query.data
    role = db_user['role']
    
    if action == "menu_adddebt":
        if role not in ("admin", "seller"):
            await query.edit_message_text("⛔ Фақат сотувчилар ва adminлар қарз қўша олади.", reply_markup=get_main_keyboard(role))
            return
        context.user_data['action'] = 'adddebt'
        await query.edit_message_text("➕ **Янги қарз қўшиш**\n\nМизожнинг **исмини** ёзинг:", parse_mode="Markdown")
        return NAME
    
    elif action == "menu_pay":
        if role not in ("admin", "seller"):
            await query.edit_message_text("⛔ Ҳуқуқингиз йўқ.", reply_markup=get_main_keyboard(role))
            return
        context.user_data['action'] = 'pay'
        await query.edit_message_text("💰 **Тўлов қабул қилиш**\n\nҚарзнинг **ID рақамини** ёзинг:", parse_mode="Markdown")
        return DEBT_ID
    
    elif action == "menu_editdebt":
        if role not in ("admin", "seller"):
            await query.edit_message_text("⛔ Ҳуқуқингиз йўқ.", reply_markup=get_main_keyboard(role))
            return
        context.user_data['action'] = 'editdebt'
        await query.edit_message_text("✏️ **Қарзни таҳрирлаш**\n\nҚарзнинг **ID рақамини** ёзинг:", parse_mode="Markdown")
        return DEBT_ID
    
    elif action == "menu_deletedebt":
        if role not in ("admin", "seller"):
            await query.edit_message_text("⛔ Ҳуқуқингиз йўқ.", reply_markup=get_main_keyboard(role))
            return
        context.user_data['action'] = 'deletedebt'
        await query.edit_message_text("🗑️ **Қарзни ўчириш**\n\nЎчириш учун қарзнинг **ID рақамини** ёзинг:", parse_mode="Markdown")
        return DEBT_ID
    
    elif action == "menu_search":
        context.user_data['action'] = 'search'
        await query.edit_message_text("🔍 **Қарзларни излаш**\n\nМизож **исми** ёки **телефон рақамини** ёзинг:", parse_mode="Markdown")
        return SEARCH_QUERY
    
    elif action == "menu_listdebts":
        debts = get_all_debts()
        if not debts:
            await query.edit_message_text("📋 Қарзлар топилмади.", reply_markup=get_main_keyboard(role))
        else:
            msg = "📋 **Барча қарзлар:**\n\n"
            for d in debts[:15]:
                msg += f"ID: `{d['id']}` | {d['customer_name']} | Қолдиқ: {d['remaining_balance']:.2f} | Сотувчи: {d['seller_name']}\n"
            if len(debts) > 15:
                msg += f"\n... ва яна {len(debts)-15} та. Тўлиқ рўйхат учун экспорт қилинг."
            await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=get_main_keyboard(role))
    
    elif action == "menu_stats":
        total_out = get_total_outstanding()
        by_seller = get_outstanding_by_seller()
        largest = get_largest_debtors(5)
        msg = f"📊 **Статистика**\n\n💸 Жами қарз: **{total_out:.2f}**\n\n**Сотувчилар бўйича:**\n"
        for s in by_seller:
            msg += f"• {s['name']}: {s['total']:.2f}\n"
        msg += "\n**Энг кўп қарздорлар (5 та):**\n"
        for d in largest:
            msg += f"• {d['name']} ({d['phone']}): {d['total']:.2f}\n"
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=get_main_keyboard(role))
    
    elif action == "menu_export":
        debts = get_all_debts()
        if not debts:
            await query.edit_message_text("Экспорт қилиш учун маълумот йўқ.", reply_markup=get_main_keyboard(role))
            return
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["ID","Мизож номи","Телефон","Қарз суммаси","Қолдиқ","Изоҳ","Сотувчи","Сана"])
        for d in debts:
            writer.writerow([d["id"], d["customer_name"], d["phone"], d["amount_owed"], d["remaining_balance"], d["notes"], d["seller_name"], d["created_at"]])
        output.seek(0)
        await query.edit_message_text("📁 CSV файл тайёрланмоқда...", reply_markup=get_main_keyboard(role))
        await query.message.reply_document(document=io.BytesIO(output.getvalue().encode()), filename="qarzlar_eksport.csv", caption="Қарзлар экспорти")
    
    elif action == "menu_users":
        if role != "admin":
            await query.edit_message_text("⛔ Фақат админлар учун.", reply_markup=get_main_keyboard(role))
            return
        await query.edit_message_text("👥 **Фойдаланувчиларни бошқариш**", reply_markup=get_users_menu())
    
    elif action == "menu_adduser":
        if role != "admin":
            await query.edit_message_text("⛔ Фақат админлар учун.", reply_markup=get_main_keyboard(role))
            return
        context.user_data['action'] = 'adduser'
        await query.edit_message_text("➕ **Фойдаланувчи қўшиш**\n\nҚўшиладиган фойдаланувчининг **Telegram ID** рақамини ёзинг.\n\n(ID ни @userinfobot дан олиш мумкин)", parse_mode="Markdown")
        return USER_ID
    
    elif action == "menu_removeuser":
        if role != "admin":
            await query.edit_message_text("⛔ Фақат админлар учун.", reply_markup=get_main_keyboard(role))
            return
        context.user_data['action'] = 'removeuser'
        await query.edit_message_text("❌ **Фойдаланувчини ўчириш**\n\nЎчириладиган фойдаланувчининг **Telegram ID** рақамини ёзинг.", parse_mode="Markdown")
        return USER_ID
    
    elif action == "menu_listusers":
        if role != "admin":
            await query.edit_message_text("⛔ Фақат админлар учун.", reply_markup=get_main_keyboard(role))
            return
        users = get_all_users()
        if not users:
            await query.edit_message_text("Фойдаланувчилар топилмади.", reply_markup=get_users_menu())
            return
        msg = "📋 **Рўйхатдан ўтган фойдаланувчилар:**\n\n"
        for u in users:
            msg += f"• {u['first_name']} (@{u['username']}) - {u['role']} (ID: `{u['telegram_id']}`)\n"
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=get_users_menu())
    
    elif action == "menu_back":
        await query.edit_message_text("Асосий меню", reply_markup=get_main_keyboard(role))
    
    elif action == "menu_cancel":
        context.user_data.clear()
        await query.edit_message_text("Амал бекор қилинди. Асосий меню:", reply_markup=get_main_keyboard(role))

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    action = context.user_data.get('action')
    if not action:
        db_user = get_user(update.effective_user.id)
        if db_user:
            await update.message.reply_text("Илтимос, тугмалардан фойдаланинг:", reply_markup=get_main_keyboard(db_user['role']))
        return ConversationHandler.END
    
    text = update.message.text.strip()
    
    if action == 'adddebt':
        step = context.user_data.get('step', 'name')
        if step == 'name':
            context.user_data['debt_name'] = text
            context.user_data['step'] = 'phone'
            await update.message.reply_text("📞 **Телефон рақамини** ёзинг (ёки /skip юборинг):", parse_mode="Markdown")
            return NAME
        elif step == 'phone':
            if text == "/skip":
                context.user_data['debt_phone'] = ""
            else:
                context.user_data['debt_phone'] = text
            context.user_data['step'] = 'amount'
            await update.message.reply_text("💰 **Қарз суммасини** ёзинг (масалан, 150.50):", parse_mode="Markdown")
            return AMOUNT
        elif step == 'amount':
            try:
                amount = float(text)
                if amount <= 0:
                    raise ValueError
                context.user_data['debt_amount'] = amount
                context.user_data['step'] = 'notes'
                await update.message.reply_text("📝 **Изоҳ** ёзинг (ихтиёрий, ёки /skip):", parse_mode="Markdown")
                return NOTES
            except:
                await update.message.reply_text("❌ Хато сумма. Ижобий сон ёзинг:")
                return AMOUNT
        elif step == 'notes':
            if text == "/skip":
                notes = ""
            else:
                notes = text
            debt_id = add_debt(
                context.user_data['debt_name'],
                context.user_data['debt_phone'],
                context.user_data['debt_amount'],
                notes,
                update.effective_user.id
            )
            await update.message.reply_text(f"✅ Қарз қўшилди! ID: `{debt_id}`", parse_mode="Markdown")
            context.user_data.clear()
            db_user = get_user(update.effective_user.id)
            await update.message.reply_text("Асосий меню:", reply_markup=get_main_keyboard(db_user['role']))
            return ConversationHandler.END
    
    elif action == 'pay':
        if 'pay_debt_id' not in context.user_data:
            try:
                debt_id = int(text)
                debt = get_debt(debt_id)
                if not debt:
                    await update.message.reply_text("❌ Қарз топилмади. Тўғри ID ёзинг:")
                    return DEBT_ID
                context.user_data['pay_debt_id'] = debt_id
                await update.message.reply_text(f"Қарз: {debt['customer_name']} дан {debt['remaining_balance']:.2f} сўм қарз.\nТўлов суммасини ёзинг:")
                return PAY_AMOUNT
            except ValueError:
                await update.message.reply_text("Нотўғри ID. Рақамли ID ёзинг:")
                return DEBT_ID
        else:
            try:
                amount = float(text)
                if amount <= 0:
                    raise ValueError
                debt_id = context.user_data['pay_debt_id']
                if add_payment(debt_id, amount):
                    new_debt = get_debt(debt_id)
                    await update.message.reply_text(f"✅ Тўлов қабул қилинди! Қолган қарз: {new_debt['remaining_balance']:.2f}")
                else:
                    await update.message.reply_text("❌ Тўлов амалга ошмади. Суммани текширинг.")
            except:
                await update.message.reply_text("Нотўғри сумма.")
            context.user_data.clear()
            db_user = get_user(update.effective_user.id)
            await update.message.reply_text("Асосий меню:", reply_markup=get_main_keyboard(db_user['role']))
            return ConversationHandler.END
    
    elif action == 'editdebt':
        if 'edit_debt_id' not in context.user_data:
            try:
                debt_id = int(text)
                debt = get_debt(debt_id)
                if not debt:
                    await update.message.reply_text("Қарз топилмади. Тўғри ID ёзинг:")
                    return DEBT_ID
                context.user_data['edit_debt_id'] = debt_id
                keyboard = [
                    [InlineKeyboardButton("Мизож исми", callback_data="edit_name")],
                    [InlineKeyboardButton("Телефон рақам", callback_data="edit_phone")],
                    [InlineKeyboardButton("Қарз суммаси", callback_data="edit_amount")],
                    [InlineKeyboardButton("Изоҳ", callback_data="edit_notes")],
                    [InlineKeyboardButton("Бекор қилиш", callback_data="edit_cancel")]
                ]
                await update.message.reply_text(f"Қарз #{debt_id} ({debt['customer_name']}) таҳрирланмоқда. Нимани ўзгартирасиз?", reply_markup=InlineKeyboardMarkup(keyboard))
                return EDIT_FIELD
            except ValueError:
                await update.message.reply_text("Нотўғри ID. Рақамли ID ёзинг:")
                return DEBT_ID
    
    elif action == 'deletedebt':
        try:
            debt_id = int(text)
            debt = get_debt(debt_id)
            if not debt:
                await update.message.reply_text("Қарз топилмади. Тўғри ID ёзинг:")
                return DEBT_ID
            delete_debt(debt_id)
            await update.message.reply_text(f"✅ Қарз #{debt_id} ўчирилди.")
        except ValueError:
            await update.message.reply_text("Нотўғри ID.")
        context.user_data.clear()
        db_user = get_user(update.effective_user.id)
        await update.message.reply_text("Асосий меню:", reply_markup=get_main_keyboard(db_user['role']))
        return ConversationHandler.END
    
    elif action == 'search':
        debts = search_debts(text)
        if not debts:
            await update.message.reply_text("Қарзлар топилмади.")
        else:
            msg = "🔍 **Қидирув натижалари:**\n\n"
            for d in debts[:20]:
                msg += f"ID: `{d['id']}` | {d['customer_name']} | {d['phone'] or '-'} | Қолдиқ: {d['remaining_balance']:.2f}\n"
            if len(debts) > 20:
                msg += f"\n... ва яна {len(debts)-20} та. Аниқроқ сўз ёзинг."
            await update.message.reply_text(msg, parse_mode="Markdown")
        context.user_data.clear()
        db_user = get_user(update.effective_user.id)
        await update.message.reply_text("Асосий меню:", reply_markup=get_main_keyboard(db_user['role']))
        return ConversationHandler.END
    
    elif action == 'adduser':
        try:
            telegram_id = int(text)
            existing = get_user(telegram_id)
            if existing:
                await update.message.reply_text(f"Фойдаланувчи мавжуд, роли: {existing['role']}.")
                context.user_data.clear()
                db_user = get_user(update.effective_user.id)
                await update.message.reply_text("Асосий меню:", reply_markup=get_main_keyboard(db_user['role']))
                return ConversationHandler.END
            context.user_data['new_user_id'] = telegram_id
            context.user_data['action'] = 'adduser_role'
            await update.message.reply_text("Ролни ёзинг (admin / seller / viewer):")
            return USER_ROLE
        except ValueError:
            await update.message.reply_text("Нотўғри ID. Рақамли Telegram ID ёзинг:")
            return USER_ID
    
    elif action == 'adduser_role':
        role = text.lower()
        if role not in ("admin", "seller", "viewer"):
            await update.message.reply_text("Нотўғри роль. admin, seller ёки viewer ёзинг:")
            return USER_ROLE
        telegram_id = context.user_data['new_user_id']
        try:
            chat = await context.bot.get_chat(telegram_id)
            username = chat.username or ""
            first_name = chat.first_name or ""
        except:
            username = ""
            first_name = "Номаълум"
        create_user(telegram_id, username, first_name, role)
        await update.message.reply_text(f"✅ {telegram_id} ID ли фойдаланувчи {role.upper()} роли билан қўшилди.")
        context.user_data.clear()
        db_user = get_user(update.effective_user.id)
        await update.message.reply_text("Асосий меню:", reply_markup=get_main_keyboard(db_user['role']))
        return ConversationHandler.END
    
    elif action == 'removeuser':
        try:
            telegram_id = int(text)
            if telegram_id == update.effective_user.id:
                await update.message.reply_text("Ўзингизни ўчиролмайсиз.")
            elif delete_user(telegram_id):
                await update.message.reply_text(f"✅ {telegram_id} ID ли фойдаланувчи ўчирилди.")
            else:
                await update.message.reply_text("Фойдаланувчи топилмади.")
        except ValueError:
            await update.message.reply_text("Нотўғри ID.")
        context.user_data.clear()
        db_user = get_user(update.effective_user.id)
        await update.message.reply_text("Асосий меню:", reply_markup=get_main_keyboard(db_user['role']))
        return ConversationHandler.END
    
    return ConversationHandler.END

async def edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data
    if action == "edit_cancel":
        await query.edit_message_text("Таҳрирлаш бекор қилинди.")
        context.user_data.clear()
        db_user = get_user(query.from_user.id)
        await query.message.reply_text("Асосий меню:", reply_markup=get_main_keyboard(db_user['role']))
        return ConversationHandler.END
    field = action.split("_")[1]
    context.user_data['edit_field'] = field
    await query.edit_message_text(f"Янги {field} ни ёзинг:")
    return EDIT_VALUE

async def edit_value_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = context.user_data['edit_field']
    new_value = update.message.text.strip()
    debt_id = context.user_data['edit_debt_id']
    if field == "amount":
        try:
            new_amount = float(new_value)
            if new_amount <= 0:
                raise ValueError
            update_debt(debt_id, amount_owed=new_amount, remaining_balance=new_amount)
        except:
            await update.message.reply_text("Нотўғри сумма.")
            return EDIT_FIELD
    elif field == "name":
        update_debt(debt_id, customer_name=new_value)
    elif field == "phone":
        update_debt(debt_id, phone=new_value)
    elif field == "notes":
        update_debt(debt_id, notes=new_value)
    await update.message.reply_text(f"✅ {field} янгиланди.")
    context.user_data.clear()
    db_user = get_user(update.effective_user.id)
    await update.message.reply_text("Асосий меню:", reply_markup=get_main_keyboard(db_user['role']))
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Амал бекор қилинди.")
    context.user_data.clear()
    db_user = get_user(update.effective_user.id)
    if db_user:
        await update.message.reply_text("Асосий меню:", reply_markup=get_main_keyboard(db_user['role']))
    return ConversationHandler.END

async def main_bot_async():
    """Asynchronous entry point that manages the bot lifecycle safely in an isolated event loop context."""
    req = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    app = Application.builder().token(BOT_TOKEN).request(req).build()

    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(menu_handler)],
        states={
            NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input)],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input)],
            AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input)],
            NOTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input)],
            DEBT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input)],
            PAY_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input)],
            EDIT_FIELD: [CallbackQueryHandler(edit_callback)],
            EDIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_value_handler)],
            SEARCH_QUERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input)],
            USER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input)],
            USER_ROLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))

    logging.info("Telegram bot initializing...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling()
    logging.info("Telegram bot started polling successfully.")
    
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

def run_telegram_bot():
    """This function safely boots up the bot context within its assigned background thread thread loop."""
    asyncio.run(main_bot_async())

# ---------- Main Entry Point ----------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    # Initialize connection and verify schemas inside PostgreSQL database
    init_db()
    
    bot_thread = threading.Thread(target=run_telegram_bot)
    bot_thread.daemon = True
    bot_thread.start()
    
    port = int(os.environ.get('PORT', 5000))
    logging.info(f"Starting Flask web server on port {port}")
    flask_app.run(host='0.0.0.0', port=port)
