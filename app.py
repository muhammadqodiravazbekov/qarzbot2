import os
import io
import re
import csv
import logging
import asyncio
import threading
import unicodedata
import psycopg2
from datetime import datetime
from typing import List, Dict, Optional
from flask import Flask, jsonify
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ConversationHandler,
    MessageHandler, CallbackQueryHandler, filters, ContextTypes
)
from telegram.request import HTTPXRequest

# ---------- Flask Веб Сервер (Health Check) ----------
flask_app = Flask(__name__)

@flask_app.route('/')
@flask_app.route('/health')
def health():
    return jsonify({"status": "alive", "message": "Bot is running perfectly!"}), 200

# ---------- Конфигурация ва Муҳит Ўзгарувчилари ----------
BOT_TOKEN = os.environ.get('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN муҳит ўзгарувчиси топилмади!")

DATABASE_URL = os.environ.get('DATABASE_URL')
if not DATABASE_URL:
    raise ValueError("DATABASE_URL муҳит ўзгарувчиси топилмади!")

BACKUP_GROUP_ID = os.environ.get('BACKUP_GROUP_ID')
BACKUP_TOPIC_ID = os.environ.get('BACKUP_TOPIC_ID')

# ---------- Суҳбат Ҳолатлари (Conversation States) ----------
(
    ADD_NAME, ADD_PHONE, ADD_AMOUNT, ADD_NOTES,
    EXIST_SEARCH, EXIST_SELECT, EXIST_AMOUNT,
    PAY_DEBT_ID, PAY_AMOUNT,
    EDIT_DEBT_ID, EDIT_FIELD, EDIT_VALUE,
    DELETE_DEBT_ID, SEARCH_QUERY,
    USER_ID, USER_ROLE
) = range(16)

# ---------- Ёрдамчи Функциялар ----------
def normalize_text(text: str) -> str:
    """Қидирувни осонлаштириш учун матнни нормаллаштириш функцияси."""
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
    return re.sub(r'[^a-z0-9]', '', normalized)

async def send_backup(context: ContextTypes.DEFAULT_TYPE, message: str):
    """Гуруҳ ёки махсус Топик Thread'га автоматик бэкап ҳисоботини юбориш."""
    if BACKUP_GROUP_ID:
        try:
            kwargs = {"chat_id": int(BACKUP_GROUP_ID), "text": message, "parse_mode": "Markdown"}
            if BACKUP_TOPIC_ID:
                kwargs["message_thread_id"] = int(BACKUP_TOPIC_ID)
            await context.bot.send_message(**kwargs)
        except Exception as e:
            logging.error(f"Бэкап юборишда хатолик: {e}")

# ---------- Маълумотлар Базаси Билан Ишлаш (PostgreSQL) ----------
def get_db():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    """Жадвалларни базада шакллантириш."""
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    role TEXT CHECK(role IN ('admin','seller','viewer')) NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            ''')
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
            conn.commit()
    finally:
        conn.close()

def get_user(telegram_id: int) -> Optional[Dict]:
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT telegram_id, username, first_name, role FROM users WHERE telegram_id = %s", (telegram_id,))
            row = cursor.fetchone()
            return {"telegram_id": row[0], "username": row[1], "first_name": row[2], "role": row[3]} if row else None
    finally:
        conn.close()

def create_user(telegram_id: int, username: str, first_name: str, role: str) -> bool:
    conn = get_db()
    try:
        with conn.cursor() as cursor:
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
    try:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM users WHERE telegram_id = %s", (telegram_id,))
            conn.commit()
            return cursor.rowcount > 0
    finally:
        conn.close()

def get_all_users() -> List[Dict]:
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT telegram_id, username, first_name, role, created_at FROM users ORDER BY created_at")
            rows = cursor.fetchall()
            return [{"telegram_id": r[0], "username": r[1], "first_name": r[2], "role": r[3], "created_at": r[4]} for r in rows]
    finally:
        conn.close()

def get_admins_and_sellers() -> List[Dict]:
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT telegram_id, username, first_name, role FROM users WHERE role IN ('admin','seller')")
            rows = cursor.fetchall()
            return [{"telegram_id": r[0], "username": r[1], "first_name": r[2], "role": r[3]} for r in rows]
    finally:
        conn.close()

def add_debt(customer_name: str, phone: str, amount: float, notes: str, seller_telegram_id: int) -> int:
    norm_name = normalize_text(customer_name)
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO debts (customer_name, customer_name_normalized, phone, amount_owed, remaining_balance, notes, seller_telegram_id) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (customer_name, norm_name, phone, amount, amount, notes, seller_telegram_id)
            )
            debt_id = cursor.fetchone()[0]
            conn.commit()
            return debt_id
    finally:
        conn.close()

def get_debt(debt_id: int) -> Optional[Dict]:
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, customer_name, phone, amount_owed, remaining_balance, notes, seller_telegram_id, created_at, updated_at FROM debts WHERE id = %s", (debt_id,))
            row = cursor.fetchone()
            return {"id": row[0], "customer_name": row[1], "phone": row[2], "amount_owed": row[3], "remaining_balance": row[4], "notes": row[5], "seller_telegram_id": row[6], "created_at": row[7], "updated_at": row[8]} if row else None
    finally:
        conn.close()

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
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"UPDATE debts SET {set_clause} WHERE id = %s", values)
            conn.commit()
            return cursor.rowcount > 0
    finally:
        conn.close()

def delete_debt(debt_id: int) -> bool:
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("DELETE FROM debts WHERE id = %s", (debt_id,))
            conn.commit()
            return cursor.rowcount > 0
    finally:
        conn.close()

def add_payment(debt_id: int, amount: float, notes: str = "") -> bool:
    debt = get_debt(debt_id)
    if not debt or amount <= 0 or amount > debt["remaining_balance"]:
        return False
    new_balance = debt["remaining_balance"] - amount
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO payments (debt_id, amount_paid, notes) VALUES (%s, %s, %s)", (debt_id, amount, notes))
            cursor.execute("UPDATE debts SET remaining_balance = %s, updated_at = %s WHERE id = %s", (new_balance, datetime.now(), debt_id))
            conn.commit()
            return True
    finally:
        conn.close()

def search_debts(query: str) -> List[Dict]:
    norm_query = normalize_text(query)
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT d.id, d.customer_name, d.phone, d.amount_owed, d.remaining_balance, d.notes,
                       d.seller_telegram_id, d.created_at, d.updated_at, u.username, u.first_name
                FROM debts d
                JOIN users u ON d.seller_telegram_id = u.telegram_id
                WHERE d.phone LIKE %s OR d.customer_name_normalized LIKE %s
                ORDER BY d.created_at DESC
            """, (f"%{query}%", f"%{norm_query}%"))
            rows = cursor.fetchall()
            return [{"id": r[0], "customer_name": r[1], "phone": r[2], "amount_owed": r[3], "remaining_balance": r[4], "notes": r[5], "seller_telegram_id": r[6], "created_at": r[7], "updated_at": r[8], "seller_name": r[9] or r[10] or str(r[6])} for r in rows]
    finally:
        conn.close()

def get_all_debts() -> List[Dict]:
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT d.id, d.customer_name, d.phone, d.amount_owed, d.remaining_balance, d.notes,
                       d.seller_telegram_id, d.created_at, d.updated_at, u.username, u.first_name
                FROM debts d
                JOIN users u ON d.seller_telegram_id = u.telegram_id
                ORDER BY d.created_at DESC
            """)
            rows = cursor.fetchall()
            return [{"id": r[0], "customer_name": r[1], "phone": r[2], "amount_owed": r[3], "remaining_balance": r[4], "notes": r[5], "seller_telegram_id": r[6], "created_at": r[7], "updated_at": r[8], "seller_name": r[9] or r[10] or str(r[6])} for r in rows]
    finally:
        conn.close()

def get_total_outstanding() -> float:
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COALESCE(SUM(remaining_balance), 0) FROM debts")
            return cursor.fetchone()[0]
    finally:
        conn.close()

def get_outstanding_by_seller() -> List[Dict]:
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT u.telegram_id, u.username, u.first_name, COALESCE(SUM(d.remaining_balance), 0)
                FROM users u
                LEFT JOIN debts d ON u.telegram_id = d.seller_telegram_id
                WHERE u.role IN ('admin','seller')
                GROUP BY u.telegram_id, u.username, u.first_name
                ORDER BY SUM(d.remaining_balance) DESC
            """)
            rows = cursor.fetchall()
            return [{"seller_id": r[0], "name": r[1] or r[2] or str(r[0]), "total": r[3]} for r in rows]
    finally:
        conn.close()

def get_largest_debtors(limit: int = 5) -> List[Dict]:
    conn = get_db()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT customer_name, phone, SUM(remaining_balance) as total_balance
                FROM debts
                GROUP BY customer_name, phone
                ORDER BY total_balance DESC
                LIMIT %s
            """, (limit,))
            rows = cursor.fetchall()
            return [{"name": r[0], "phone": r[1], "total": r[2]} for r in rows]
    finally:
        conn.close()

# ---------- Клавиатура Менюлари ----------
def get_main_keyboard(role: str) -> InlineKeyboardMarkup:
    keyboard = []
    if role in ("admin", "seller"):
        keyboard.append([InlineKeyboardButton("➕ Янги мизож ва қарз", callback_data="menu_adddebt")])
        keyboard.append([InlineKeyboardButton("➕ Мавжуд мизожга қарз", callback_data="menu_existdebt")])
        keyboard.append([InlineKeyboardButton("💰 Тўлов қабул қилиш", callback_data="menu_pay")])
        keyboard.append([InlineKeyboardButton("✏️ Қарзни таҳрирлаш", callback_data="menu_editdebt")])
        keyboard.append([InlineKeyboardButton("🗑️ Қарзни ўчириш", callback_data="menu_deletedebt")])
    
    keyboard.extend([
        [InlineKeyboardButton("🔍 Қарзларни излаш", callback_data="menu_search")],
        [InlineKeyboardButton("📋 Барча қарзлар рўйхати", callback_data="menu_listdebts")],
        [InlineKeyboardButton("📊 Статистика", callback_data="menu_stats")],
        [InlineKeyboardButton("📁 CSV Экспорт", callback_data="menu_export")]
    ])
    
    if role == "admin":
        keyboard.append([InlineKeyboardButton("👥 Фойдаланувчиларни бошқариш", callback_data="menu_users")])
        
    keyboard.append([InlineKeyboardButton("❌ Навбатдаги амални бекор қилиш", callback_data="menu_cancel")])
    return InlineKeyboardMarkup(keyboard)

def get_users_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Фойдаланувчи қўшиш", callback_data="menu_adduser")],
        [InlineKeyboardButton("❌ Фойдаланувчини ўчириш", callback_data="menu_removeuser")],
        [InlineKeyboardButton("📋 Фойдаланувчилар рўйхати", callback_data="menu_listusers")],
        [InlineKeyboardButton("🔙 Орқага", callback_data="menu_back")]
    ])

# ---------- Старт Коммандаси ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = get_user(user.id)
    
    if not db_user:
        if not get_admins_and_sellers():
            create_user(user.id, user.username or "", user.first_name or "", "admin")
            await update.message.reply_text(
                f"✅ {user.first_name}, сиз тизимдаги биринчи фойдаланувчи бўлганингиз учун **АДМИН** этиб тайинландингиз.",
                reply_markup=get_main_keyboard("admin")
            )
        else:
            await update.message.reply_text("❌ Кириш тақиқланган. Тизимдан фойдаланиш учун админ рухсати зарур.")
        return
        
    await update.message.reply_text(
        f"Тизим тайёр. Ролингиз: **{db_user['role'].upper()}**\nКеракли амални танланг:",
        reply_markup=get_main_keyboard(db_user['role'])
    )

# ---------- Меню Callback Навигатсияси ----------
async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db_user = get_user(query.from_user.id)
    if not db_user:
        await query.edit_message_text("Рухсатингиз йўқ.")
        return ConversationHandler.END
        
    action = query.data
    role = db_user['role']
    
    if action == "menu_adddebt":
        if role not in ("admin", "seller"):
            await query.edit_message_text("⛔ Рухсатингиз йўқ.", reply_markup=get_main_keyboard(role))
            return ConversationHandler.END
        await query.edit_message_text("👤 Мизожнинг исми ва фамилиясини киритинг:")
        return ADD_NAME
        
    elif action == "menu_existdebt":
        if role not in ("admin", "seller"):
            await query.edit_message_text("⛔ Рухсатингиз йўқ.", reply_markup=get_main_keyboard(role))
            return ConversationHandler.END
        await query.edit_message_text("🔍 Кўпайтирилмоқчи бўлган мизожнинг исми ёки телини киритинг:")
        return EXIST_SEARCH
        
    elif action == "menu_pay":
        if role not in ("admin", "seller"):
            await query.edit_message_text("⛔ Рухсатингиз йўқ.", reply_markup=get_main_keyboard(role))
            return ConversationHandler.END
        await query.edit_message_text("💰 Тўлов қабул қилиш учун қарзнинг **ID рақамини** киритинг:")
        return PAY_DEBT_ID
        
    elif action == "menu_editdebt":
        if role not in ("admin", "seller"):
            await query.edit_message_text("⛔ Рухсатингиз йўқ.", reply_markup=get_main_keyboard(role))
            return ConversationHandler.END
        await query.edit_message_text("✏️ Ўзгартирилмоқчи бўлган қарзнинг **ID рақамини** киритинг:")
        return EDIT_DEBT_ID
        
    elif action == "menu_deletedebt":
        if role not in ("admin", "seller"):
            await query.edit_message_text("⛔ Рухсатингиз йўқ.", reply_markup=get_main_keyboard(role))
            return ConversationHandler.END
        await query.edit_message_text("🗑️ Ўчирилмоқчи бўлган қарзнинг **ID рақамини** киритинг:")
        return DELETE_DEBT_ID
        
    elif action == "menu_search":
        await query.edit_message_text("🔍 Қидирилаётган мизож исми ёки телефони:")
        return SEARCH_QUERY
        
    elif action == "menu_listdebts":
        debts = get_all_debts()
        if not debts:
            await query.edit_message_text("📋 Қарзлар топилмади.", reply_markup=get_main_keyboard(role))
        else:
            msg = "📋 **Қарздорлар рўйхати (Охирги 15 та):**\n\n"
            for d in debts[:15]:
                msg += f"🆔 `ID: {d['id']}` | {d['customer_name']} | 💰 Қолдиқ: **{d['remaining_balance']:.2f}**\n"
            if len(debts) > 15:
                msg += f"\n... ва яна {len(debts)-15} та мизож. Тўлиқ рўйхат учун экспорт қилинг."
            await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=get_main_keyboard(role))
        return ConversationHandler.END
        
    elif action == "menu_stats":
        msg = f"📊 **Умумий Статистика**\n\n💸 Умумий қарзлар миқдори: **{get_total_outstanding():.2f}**\n\n"
        msg += "**Сотувчилар бўйича қолдиқлар:**\n"
        for s in get_outstanding_by_seller():
            msg += f"• {s['name']}: {s['total']:.2f}\n"
        msg += "\n**Энг йирик қарздорлар:**\n"
        for d in get_largest_debtors(5):
            msg += f"• {d['name']} ({d['phone'] or '-'}): **{d['total']:.2f}**\n"
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=get_main_keyboard(role))
        return ConversationHandler.END
        
    elif action == "menu_export":
        debts = get_all_debts()
        if not debts:
            await query.edit_message_text("Маълумот мавжуд эмас.", reply_markup=get_main_keyboard(role))
            return ConversationHandler.END
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["ID", "Name", "Phone", "Owed", "Balance", "Notes", "Seller", "Date"])
        for d in debts:
            writer.writerow([d["id"], d["customer_name"], d["phone"], d["amount_owed"], d["remaining_balance"], d["notes"], d["seller_name"], d["created_at"]])
        output.seek(0)
        await query.edit_message_text("📁 Файл тайёр, юборилмоқда...", reply_markup=get_main_keyboard(role))
        await query.message.reply_document(document=io.BytesIO(output.getvalue().encode()), filename="qarzlar.csv")
        return ConversationHandler.END
        
    elif action == "menu_users":
        if role != "admin":
            await query.edit_message_text("⛔ Тақиқланган.")
            return ConversationHandler.END
        await query.edit_message_text("👥 Фойдаланувчиларни бошқариш менюси:", reply_markup=get_users_menu())
        return ConversationHandler.END
        
    elif action == "menu_back":
        await query.edit_message_text("Асосий меню:", reply_markup=get_main_keyboard(role))
        return ConversationHandler.END
        
    elif action == "menu_cancel":
        context.user_data.clear()
        await query.edit_message_text("Амал бекор қилинди.", reply_markup=get_main_keyboard(role))
        return ConversationHandler.END

# ---------- Стейтлар Бўйича Алоҳида Модул Функсиялар ----------

# 1. Янги Қарз Қўшиш Занжири
async def add_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['debt_name'] = update.message.text.strip()
    await update.message.reply_text("📞 Телефон рақамини киритинг (ўтказиб юбориш учун /skip юборинг):")
    return ADD_PHONE

async def add_phone_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data['debt_phone'] = "" if text == "/skip" else text
    await update.message.reply_text("💰 Қарз суммасини киритинг:")
    return ADD_AMOUNT

async def add_amount_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip())
        if amount <= 0: raise ValueError
        context.user_data['debt_amount'] = amount
        await update.message.reply_text("📝 Қўшимча изоҳ ёзинг (ўтказиб юбориш учун /skip юборинг):")
        return ADD_NOTES
    except ValueError:
        await update.message.reply_text("❌ Хато пул суммаси. Илтимос, сон киритинг:")
        return ADD_AMOUNT

async def add_notes_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    notes = "" if text == "/skip" else text
    
    debt_id = add_debt(
        context.user_data['debt_name'],
        context.user_data['debt_phone'],
        context.user_data['debt_amount'],
        notes,
        update.effective_user.id
    )
    
    await update.message.reply_text(f"✅ Қарз муваффақиятли ёзилди! Маълумот ID си: `{debt_id}`", parse_mode="Markdown")
    await send_backup(context, f"➕ **ЯНГИ ҚАРЗ**\nСотувчи: {update.effective_user.first_name}\nМизож: {context.user_data['debt_name']}\nСумма: {context.user_data['debt_amount']:.2f}")
    
    context.user_data.clear()
    return ConversationHandler.END

# 2. Мавжуд Мизожга Қарз Қўшиш
async def exist_search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    results = search_debts(text)
    if not results:
        await update.message.reply_text("❌ Мизож топилмади. Қайтадан қидиринг ёки бекор қилинг:")
        return EXIST_SEARCH
        
    buttons = [[InlineKeyboardButton(f"{r['customer_name']} (🆔 {r['id']} | Қолдиқ: {r['remaining_balance']:.2f})", callback_data=f"sel_{r['id']}")] for r in results[:8]]
    buttons.append([InlineKeyboardButton("❌ Бекор қилиш", callback_data="menu_cancel")])
    await update.message.reply_text("👇 Тезкор рўйхатдан керакли мизожни танланг:", reply_markup=InlineKeyboardMarkup(buttons))
    return EXIST_SELECT

async def exist_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    debt_id = int(query.data.split("_")[1])
    context.user_data['selected_debt_id'] = debt_id
    debt = get_debt(debt_id)
    await query.edit_message_text(f"👤 Мизож: *{debt['customer_name']}*\n\nҚўшиладиган янги қарз суммасини ёзинг:", parse_mode="Markdown")
    return EXIST_AMOUNT

async def exist_amount_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip())
        if amount <= 0: raise ValueError
        debt_id = context.user_data['selected_debt_id']
        debt = get_debt(debt_id)
        if debt:
            new_owed = debt['amount_owed'] + amount
            new_balance = debt['remaining_balance'] + amount
            update_debt(debt_id, amount_owed=new_owed, remaining_balance=new_balance)
            await update.message.reply_text(f"✅ Мизожга қарз қўшилди.\nЯнги умумий қолдиқ: **{new_balance:.2f}** сўм.", parse_mode="Markdown")
            await send_backup(context, f"🔄 **ҚАРЗ КЎПАЙТИРИЛДИ**\nМизож: {debt['customer_name']}\nҚўшилган сумма: {amount:.2f}\nЯнги умумий қолдиқ: {new_balance:.2f}")
    except ValueError:
        await update.message.reply_text("❌ Нодеквад сумма. Тўғри пул миқдорини киритинг:")
        return EXIST_AMOUNT
    context.user_data.clear()
    return ConversationHandler.END

# 3. Тўлов Қабул Қилиш
async def pay_debt_id_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        debt_id = int(update.message.text.strip())
        debt = get_debt(debt_id)
        if not debt:
            await update.message.reply_text("❌ Бундай ID ли қарз топилмади. Қайтадан киритинг:")
            return PAY_DEBT_ID
        context.user_data['pay_debt_id'] = debt_id
        await update.message.reply_text(f"👤 Мизож: {debt['customer_name']}\n💸 Жорий қарз қолдиғи: **{debt['remaining_balance']:.2f}**\n\nТўланаётган суммани киритинг:", parse_mode="Markdown")
        return PAY_AMOUNT
    except ValueError:
        await update.message.reply_text("Рақамли ID киритинг:")
        return PAY_DEBT_ID

async def pay_amount_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip())
        debt_id = context.user_data['pay_debt_id']
        if add_payment(debt_id, amount):
            debt = get_debt(debt_id)
            await update.message.reply_text(f"✅ Тўлов қабул қилинди.\n👤 Мизож: {debt['customer_name']}\n📉 Янги қарз қолдиғи: **{debt['remaining_balance']:.2f}**", parse_mode="Markdown")
            await send_backup(context, f"💰 **ТЎЛОВ ҚАБУЛ ҚИЛИНДИ**\nМизож: {debt['customer_name']}\nКиритилган тўлов: {amount:.2f}\nҚолдиқ қарз: {debt['remaining_balance']:.2f}")
        else:
            await update.message.reply_text("❌ Хатолик: Сумма қолдиқ қарздан катта ёки нотўғри.")
    except ValueError:
        await update.message.reply_text("Тўғри пул миқдорини киритинг:")
        return PAY_AMOUNT
    context.user_data.clear()
    return ConversationHandler.END

# 4. Қарзни Таҳрирлаш
async def edit_debt_id_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        debt_id = int(update.message.text.strip())
        debt = get_debt(debt_id)
        if not debt:
            await update.message.reply_text("❌ Топилмади. Қайтадан киритинг:")
            return EDIT_DEBT_ID
        context.user_data['edit_debt_id'] = debt_id
        buttons = [
            [InlineKeyboardButton("Исмни ўзгартириш", callback_data="edit_name")],
            [InlineKeyboardButton("Телефонни ўзгартириш", callback_data="edit_phone")],
            [InlineKeyboardButton("Суммани ўзгартириш", callback_data="edit_amount")],
            [InlineKeyboardButton("Изоҳни ўзгартириш", callback_data="edit_notes")],
            [InlineKeyboardButton("❌ Бекор қилиш", callback_data="edit_cancel")]
        ]
        await update.message.reply_text(f"⚙️ Қарз #{debt_id} ({debt['customer_name']}) учун ўзгартириладиган майдонни танланг:", reply_markup=InlineKeyboardMarkup(buttons))
        return EDIT_FIELD
    except ValueError:
        await update.message.reply_text("Тўғри ID рақам киритинг:")
        return EDIT_DEBT_ID

async def edit_field_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "edit_cancel":
        await query.edit_message_text("Таҳрир бекор қилинди.")
        context.user_data.clear()
        return ConversationHandler.END
    field = query.data.split("_")[1]
    context.user_data['edit_field'] = field
    await query.edit_message_text(f"📝 Янги қийматни матн кўринишида юборинг:")
    return EDIT_VALUE

async def edit_value_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    field = context.user_data['edit_field']
    value = update.message.text.strip()
    debt_id = context.user_data['edit_debt_id']
    old_debt = get_debt(debt_id)
    
    if field == "amount":
        try:
            amt = float(value)
            update_debt(debt_id, amount_owed=amt, remaining_balance=amt)
        except ValueError:
            await update.message.reply_text("❌ Рақам киритинг:")
            return EDIT_VALUE
    elif field == "name": update_debt(debt_id, customer_name=value)
    elif field == "phone": update_debt(debt_id, phone=value)
    elif field == "notes": update_debt(debt_id, notes=value)
    
    await update.message.reply_text("✅ Муваффақиятли таҳрирланди.")
    await send_backup(context, f"✏️ **ҚАРЗ ТАҲРИРЛАНДИ**\nАдмин: {update.effective_user.first_name}\nID: {debt_id} | Майдон: {field}\nЯнги қиймат: {value}")
    context.user_data.clear()
    return ConversationHandler.END

# 5. Ўчириш, Қидирув ва Фойдаланувчилар Бошқаруви
async def delete_debt_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        debt_id = int(update.message.text.strip())
        debt = get_debt(debt_id)
        if debt and delete_debt(debt_id):
            await update.message.reply_text("✅ Қарз маълумотлари базадан бутунлай ўчирилди.")
            await send_backup(context, f"🗑️ **ҚАРЗ ЎЧИРИЛДИ**\nМаълумот ID си: {debt_id}\nМизож исми: {debt['customer_name']}")
        else:
            await update.message.reply_text("❌ Бундай ID ли қарз мавжуд эмас.")
    except ValueError:
        await update.message.reply_text("Тўғри рақам киритинг:")
        return DELETE_DEBT_ID
    context.user_data.clear()
    return ConversationHandler.END

async def search_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    debts = search_debts(text)
    if not debts:
        await update.message.reply_text("🔍 Ҳеч қандай мослик топилмади.")
    else:
        msg = "🔍 **Қидирув натижалари:**\n\n"
        for d in debts[:15]:
            msg += f"🆔 `ID: {d['id']}` | {d['customer_name']} | 📞 {d['phone'] or '-'} | 💰 Қолдиқ: **{d['remaining_balance']:.2f}**\n"
        await update.message.reply_text(msg, parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END

async def user_id_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        tg_id = int(update.message.text.strip())
        context.user_data['target_tg_id'] = tg_id
        await update.message.reply_text("Ушбу фойдаланувчига бериладиган ролни ёзинг (admin / seller / viewer):")
        return USER_ROLE
    except ValueError:
        await update.message.reply_text("Илтимос, рақамли Telegram ID киритинг:")
        return USER_ID

async def user_role_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    role = update.message.text.strip().lower()
    if role not in ("admin", "seller", "viewer"):
        await update.message.reply_text("❌ Номаълум роль. Қайтадан ёзинг (admin/seller/viewer):")
        return USER_ROLE
        
    tg_id = context.user_data['target_tg_id']
    try:
        chat = await context.bot.get_chat(tg_id)
        first_name = chat.first_name or "User"
        username = chat.username or ""
    except Exception:
        first_name = "Рўйхатдан ўтувчи"
        username = ""
        
    if create_user(tg_id, username, first_name, role):
        await update.message.reply_text(f"✅ Фойдаланувчи {first_name} янги **{role.upper()}** роли билан тизимга қўшилди.")
    else:
        await update.message.reply_text("❌ Бу фойдаланувчи аллақачон мавжуд ёки хатолик юз берди.")
    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("Жорий амалиёт тўхтатилди.")
    return ConversationHandler.END

# ---------- Ботни Ишга Тушириш Цикли (Polling Thread) ----------
def run_telegram_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    req = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    app = Application.builder().token(BOT_TOKEN).request(req).build()

    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(menu_handler)],
        states={
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name_handler)],
            ADD_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_phone_handler)],
            ADD_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_amount_handler)],
            ADD_NOTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_notes_handler)],
            
            EXIST_SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, exist_search_handler)],
            EXIST_SELECT: [CallbackQueryHandler(exist_select_callback, pattern="^sel_")],
            EXIST_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, exist_amount_handler)],
            
            PAY_DEBT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, pay_debt_id_handler)],
            PAY_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, pay_amount_handler)],
            
            EDIT_DEBT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_debt_id_handler)],
            EDIT_FIELD: [CallbackQueryHandler(edit_field_callback, pattern="^edit_")],
            EDIT_VALUE: [MessageHandler(filters.TEXT & ~filters.COMMAND, edit_value_handler)],
            
            DELETE_DEBT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, delete_debt_handler)],
            SEARCH_QUERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, search_query_handler)],
            
            USER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, user_id_handler)],
            USER_ROLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, user_role_handler)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(menu_handler, pattern="^menu_cancel$")]
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))

    logging.info("Telegram бот муваффақиятли ишга тушди.")
    app.run_polling(stop_signals=None)

# ---------- Асосий Кириш Нуқтаси (Main) ----------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    # Базани текшириш ва жадвалларни яратиш
    init_db()
    
    # Ботни алоҳида оқимда (thread) юргизиш
    bot_thread = threading.Thread(target=run_telegram_bot)
    bot_thread.daemon = True
    bot_thread.start()
    
    # Flask веб сервер портни банд қилиб Render ўчиб қолмаслигини таъминлайди
    port = int(os.environ.get('PORT', 5000))
    logging.info(f"Flask веб сервери {port} портда бошланмоқда...")
    flask_app.run(host='0.0.0.0', port=port)
