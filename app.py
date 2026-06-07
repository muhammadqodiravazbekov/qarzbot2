import os
import io
import re
import csv
import logging
import asyncio
import threading
import unicodedata
import psycopg2
from psycopg2.pool import ThreadedConnectionPool
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional
from flask import Flask, jsonify
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
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
    return jsonify({"status": "alive", "message": "Bot is running perfectly!"}), 200

# ---------- Configuration & Timezone ----------
BOT_TOKEN = os.environ.get('BOT_TOKEN')
DATABASE_URL = os.environ.get('DATABASE_URL')
BACKUP_GROUP_ID = os.environ.get('BACKUP_GROUP_ID')
BACKUP_TOPIC_ID = os.environ.get('BACKUP_TOPIC_ID')

if not BOT_TOKEN or not DATABASE_URL:
    raise ValueError("CRITICAL ERROR: BOT_TOKEN or DATABASE_URL variables are missing!")

# Uzbekistan Timezone (UTC+5)
UZB_TZ = timezone(timedelta(hours=5))

def get_current_time():
    return datetime.now(UZB_TZ)

# ---------- Connection Pool ----------
db_pool = ThreadedConnectionPool(1, 20, dsn=DATABASE_URL)

def get_db_connection():
    return db_pool.getconn()

def release_db_connection(conn):
    db_pool.putconn(conn)

# ---------- Conversation States ----------
(
    ADD_NAME, ADD_AMOUNT, ADD_NOTES,
    EXIST_SEARCH, EXIST_AMOUNT,
    PAY_SEARCH, PAY_AMOUNT,
    SEARCH_QUERY
) = range(8)

# ---------- Helper Functions ----------
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

async def notify_group(context: ContextTypes.DEFAULT_TYPE, action: str, customer: str, amount: float, seller: str, note: str = "", old_bal: float = None, new_bal: float = None):
    if not BACKUP_GROUP_ID:
        return
    
    time_str = get_current_time().strftime('%d.%m.%Y %H:%M')
    
    msg = f"📢 **{action}**\n\n"
    msg += f"👤 Мижоз: {customer}\n"
    msg += f"💰 Сумма: {amount:,.2f} сўм\n"
    
    if old_bal is not None and new_bal is not None:
        msg += f"📊 Баланс: {old_bal:,.2f} ➡️ {new_bal:,.2f} сўм\n"
        
    msg += f"📝 Изоҳ: {note if note else '-'}\n"
    msg += f"💼 Сотувчи: {seller}\n"
    msg += f"🕒 Вақт: {time_str}"

    try:
        kwargs = {"chat_id": int(BACKUP_GROUP_ID), "text": msg, "parse_mode": "Markdown"}
        if BACKUP_TOPIC_ID:
            kwargs["message_thread_id"] = int(BACKUP_TOPIC_ID)
        await context.bot.send_message(**kwargs)
    except Exception as e:
        logging.error(f"Failed to send group notification: {e}")

# ---------- Database Functions ----------
def init_db():
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute('''CREATE TABLE IF NOT EXISTS users (
                    telegram_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT,
                    role TEXT CHECK(role IN ('admin','seller','viewer')) NOT NULL, created_at TIMESTAMP)''')
            # Keeping 'phone' in schema for backwards compatibility but not using it
            cursor.execute('''CREATE TABLE IF NOT EXISTS debts (
                    id SERIAL PRIMARY KEY, customer_name TEXT NOT NULL, customer_name_normalized TEXT,
                    phone TEXT, amount_owed REAL NOT NULL, remaining_balance REAL NOT NULL, notes TEXT,
                    seller_telegram_id BIGINT NOT NULL, created_at TIMESTAMP, updated_at TIMESTAMP,
                    FOREIGN KEY (seller_telegram_id) REFERENCES users(telegram_id))''')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_debt_name_normalized ON debts(customer_name_normalized)')
            cursor.execute('''CREATE TABLE IF NOT EXISTS payments (
                    id SERIAL PRIMARY KEY, debt_id INTEGER NOT NULL, amount_paid REAL NOT NULL,
                    payment_date TIMESTAMP, notes TEXT, FOREIGN KEY (debt_id) REFERENCES debts(id) ON DELETE CASCADE)''')
            conn.commit()
    finally:
        release_db_connection(conn)

def get_user(telegram_id: int) -> Optional[Dict]:
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT telegram_id, username, first_name, role FROM users WHERE telegram_id = %s", (telegram_id,))
            row = cursor.fetchone()
            return {"telegram_id": row[0], "username": row[1], "first_name": row[2], "role": row[3]} if row else None
    finally:
        release_db_connection(conn)

def create_user(telegram_id: int, username: str, first_name: str, role: str) -> bool:
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO users (telegram_id, username, first_name, role, created_at) VALUES (%s, %s, %s, %s, %s)",
                           (telegram_id, username, first_name, role, get_current_time()))
            conn.commit()
            return True
    except psycopg2.IntegrityError: return False
    finally: release_db_connection(conn)

def get_admins_and_sellers() -> List[Dict]:
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT telegram_id, username, first_name, role FROM users WHERE role IN ('admin','seller')")
            return [{"telegram_id": r[0], "username": r[1], "first_name": r[2], "role": r[3]} for r in cursor.fetchall()]
    finally: release_db_connection(conn)

def add_debt(customer_name: str, amount: float, notes: str, seller_telegram_id: int) -> int:
    norm_name = normalize_text(customer_name)
    conn = get_db_connection()
    now = get_current_time()
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                "INSERT INTO debts (customer_name, customer_name_normalized, phone, amount_owed, remaining_balance, notes, seller_telegram_id, created_at, updated_at) "
                "VALUES (%s, %s, '', %s, %s, %s, %s, %s, %s) RETURNING id",
                (customer_name, norm_name, amount, amount, notes, seller_telegram_id, now, now)
            )
            debt_id = cursor.fetchone()[0]
            conn.commit()
            return debt_id
    finally: release_db_connection(conn)

def get_debt(debt_id: int) -> Optional[Dict]:
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, customer_name, amount_owed, remaining_balance, notes, seller_telegram_id FROM debts WHERE id = %s", (debt_id,))
            row = cursor.fetchone()
            return {"id": row[0], "customer_name": row[1], "amount_owed": row[2], "remaining_balance": row[3], "notes": row[4], "seller_telegram_id": row[5]} if row else None
    finally: release_db_connection(conn)

def update_debt(debt_id: int, **kwargs) -> bool:
    allowed_fields = {"amount_owed", "remaining_balance"}
    updates = {k: v for k, v in kwargs.items() if k in allowed_fields}
    if not updates: return False
    updates["updated_at"] = get_current_time()
    
    set_clause = ", ".join([f"{key} = %s" for key in updates.keys()])
    values = list(updates.values()) + [debt_id]
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"UPDATE debts SET {set_clause} WHERE id = %s", values)
            conn.commit()
            return cursor.rowcount > 0
    finally: release_db_connection(conn)

def add_payment(debt_id: int, amount: float, notes: str = "") -> str:
    debt = get_debt(debt_id)
    if not debt or amount <= 0 or amount > debt["remaining_balance"]: return "error"
        
    new_balance = debt["remaining_balance"] - amount
    conn = get_db_connection()
    now = get_current_time()
    try:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO payments (debt_id, amount_paid, payment_date, notes) VALUES (%s, %s, %s, %s)", (debt_id, amount, now, notes))
            if new_balance <= 0.01:
                cursor.execute("DELETE FROM debts WHERE id = %s", (debt_id,))
                conn.commit()
                return "paid_off"
            else:
                cursor.execute("UPDATE debts SET remaining_balance = %s, updated_at = %s WHERE id = %s", (new_balance, now, debt_id))
                conn.commit()
                return "updated"
    finally: release_db_connection(conn)

def search_debts(query: str) -> List[Dict]:
    norm_query = normalize_text(query)
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT d.id, d.customer_name, d.amount_owed, d.remaining_balance, d.notes, d.seller_telegram_id, u.first_name
                FROM debts d JOIN users u ON d.seller_telegram_id = u.telegram_id
                WHERE d.customer_name_normalized LIKE %s AND d.remaining_balance > 0.01 ORDER BY d.created_at DESC
            """, (f"%{norm_query}%",))
            rows = cursor.fetchall()
            return [{"id": r[0], "customer_name": r[1], "amount_owed": r[2], "remaining_balance": r[3], "notes": r[4], "seller_name": r[6] or str(r[5])} for r in rows]
    finally: release_db_connection(conn)

def get_all_debts() -> List[Dict]:
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT d.id, d.customer_name, d.remaining_balance, d.notes, u.first_name, d.created_at
                FROM debts d JOIN users u ON d.seller_telegram_id = u.telegram_id 
                WHERE d.remaining_balance > 0.01 ORDER BY d.created_at DESC
            """)
            rows = cursor.fetchall()
            return [{"id": r[0], "customer_name": r[1], "remaining_balance": r[2], "notes": r[3], "seller_name": r[4], "created_at": r[5]} for r in rows]
    finally: release_db_connection(conn)

def get_stats() -> tuple:
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COALESCE(SUM(remaining_balance), 0) FROM debts WHERE remaining_balance > 0.01")
            total = cursor.fetchone()[0]
            cursor.execute("""
                SELECT customer_name, SUM(remaining_balance) FROM debts WHERE remaining_balance > 0.01
                GROUP BY customer_name ORDER BY SUM(remaining_balance) DESC LIMIT 5
            """)
            top_debtors = [{"name": r[0], "total": r[1]} for r in cursor.fetchall()]
            return total, top_debtors
    finally: release_db_connection(conn)

# ---------- Permanent Bottom Menu ----------
def get_main_reply_keyboard(role: str) -> ReplyKeyboardMarkup:
    keyboard = []
    if role in ("admin", "seller"):
        keyboard.append([KeyboardButton("➕ Янги мизож ва қарз"), KeyboardButton("➕ Мавжуд мизожга қарз")])
        keyboard.append([KeyboardButton("💰 Тўлов қабул қилиш"), KeyboardButton("❌ Амални бекор қилиш")])
    keyboard.append([KeyboardButton("🔍 Qарзларни излаш"), KeyboardButton("📋 Барча қарзлар рўйхати")])
    keyboard.append([KeyboardButton("📊 Статистика"), KeyboardButton("📢 Гуруҳга Бэкап юбориш")])
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def cancel_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Бекор қилиш", callback_data="cancel_action")]])

# ---------- Initialization ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = await asyncio.to_thread(get_user, user.id)
    
    if not db_user:
        admins_sellers = await asyncio.to_thread(get_admins_and_sellers)
        if not admins_sellers:
            await asyncio.to_thread(create_user, user.id, user.username or "", user.first_name or "", "admin")
            await update.message.reply_text(f"✅ {user.first_name}, сиз тизимга АДМИН этиб тайинландингиз.", reply_markup=get_main_reply_keyboard("admin"))
        else:
            await update.message.reply_text("❌ Кириш тақиқланган. Тизимдан фойдаланиш учун Админ рухсати зарур.")
        return
    await update.message.reply_text("Тизим тайёр. Пастки менюдан фойдаланинг:", reply_markup=get_main_reply_keyboard(db_user['role']))

# ---------- Central Cancel Callback ----------
async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data.clear()
    await query.edit_message_text("🚫 Амал бекор қилинди.")
    return ConversationHandler.END

# ---------- FLOW 1: NEW DEBT ----------
async def add_debt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👤 **Мижоз исми ва фамилиясини киритинг:**", parse_mode="Markdown", reply_markup=cancel_inline_keyboard())
    return ADD_NAME

async def add_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['debt_name'] = update.message.text.strip()
    await update.message.reply_text("💰 **Қарз суммасини киритинг (сўмда):**", parse_mode="Markdown", reply_markup=cancel_inline_keyboard())
    return ADD_AMOUNT

async def add_amount_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['debt_amount'] = float(update.message.text.strip())
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➡️ Изоҳсиз сақлаш", callback_data="skip_notes")],
            [InlineKeyboardButton("❌ Бекор қилиш", callback_data="cancel_action")]
        ])
        await update.message.reply_text("📝 **Изоҳ ёзинг** (ёки пастдаги тугмани босинг):", parse_mode="Markdown", reply_markup=kb)
        return ADD_NOTES
    except ValueError:
        await update.message.reply_text("❌ Илтимос, фақат рақам/сон киритинг:", reply_markup=cancel_inline_keyboard())
        return ADD_AMOUNT

async def process_new_debt_save(context: ContextTypes.DEFAULT_TYPE, update: Update, note: str):
    name = context.user_data['debt_name']
    amount = context.user_data['debt_amount']
    seller_name = update.effective_user.first_name
    
    debt_id = await asyncio.to_thread(add_debt, name, amount, note, update.effective_user.id)
    
    # Notify Group
    await notify_group(context, "ЯНГИ ҚАРЗ ҚЎШИЛДИ", name, amount, seller_name, note)
    
    text = f"✅ **Базага сақланди!**\n👤 Мижоз: {name}\n💰 Сумма: {amount:,.2f} сўм"
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="Markdown")
    else:
        await update.message.reply_text(text, parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END

async def add_notes_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_new_debt_save(context, update, update.message.text.strip())

async def add_notes_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "skip_notes":
        return await process_new_debt_save(context, update, "")

# ---------- FLOW 2: ADD TO EXISTING DEBT ----------
async def exist_debt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 **Қарзи кўпайтириладиган мизожнинг исмини киритинг:**", parse_mode="Markdown", reply_markup=cancel_inline_keyboard())
    return EXIST_SEARCH

async def search_and_select(update: Update, context: ContextTypes.DEFAULT_TYPE, next_state, action_type="exist"):
    text = update.message.text.strip()
    results = await asyncio.to_thread(search_debts, text)
    if not results:
        await update.message.reply_text("❌ Мос келувчи фаол қарздор топилмади. Бошқа исм ёзиб кўринг:", reply_markup=cancel_inline_keyboard())
        return next_state - 1 # Keeps it in the search state
        
    buttons = [[InlineKeyboardButton(f"{r['customer_name']} | {r['remaining_balance']:,.0f} сўм", callback_data=f"{action_type}_{r['id']}")] for r in results[:8]]
    buttons.append([InlineKeyboardButton("❌ Бекор қилиш", callback_data="cancel_action")])
    await update.message.reply_text("👇 **Рўйхатдан керакли мижозни танланг:**", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
    return next_state

async def exist_search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await search_and_select(update, context, EXIST_AMOUNT, "exist")

async def select_debt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, debt_id = query.data.split("_")
    context.user_data['selected_debt_id'] = int(debt_id)
    
    debt = await asyncio.to_thread(get_debt, int(debt_id))
    if action == "exist":
        msg = f"👤 Мижоз: **{debt['customer_name']}**\n📊 Жорий қарз: {debt['remaining_balance']:,.2f} сўм\n\n💰 **Қўшиладиган янги суммани киритинг:**"
        state = EXIST_AMOUNT
    else: # pay
        msg = f"👤 Мижоз: **{debt['customer_name']}**\n💸 Жорий қарз: {debt['remaining_balance']:,.2f} сўм\n\n💵 **Олинган тўлов суммасини киритинг:**"
        state = PAY_AMOUNT
        
    await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=cancel_inline_keyboard())
    return state

async def exist_amount_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip())
        debt_id = context.user_data['selected_debt_id']
        debt = await asyncio.to_thread(get_debt, debt_id)
        if debt:
            old_bal = debt['remaining_balance']
            new_bal = old_bal + amount
            await asyncio.to_thread(update_debt, debt_id, amount_owed=debt['amount_owed']+amount, remaining_balance=new_bal)
            
            await notify_group(context, "ҚАРЗ ОШИРИЛДИ", debt['customer_name'], amount, update.effective_user.first_name, "", old_bal, new_bal)
            await update.message.reply_text(f"✅ **Қарз қўшилди!**\n📊 Янги қолдиқ: {new_bal:,.2f} сўм", parse_mode="Markdown")
    except ValueError:
        await update.message.reply_text("❌ Илтимос, тўғри сон киритинг:", reply_markup=cancel_inline_keyboard())
        return EXIST_AMOUNT
    context.user_data.clear()
    return ConversationHandler.END

# ---------- FLOW 3: RECEIVE PAYMENT ----------
async def pay_debt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 **Тўлов қилаётган мижознинг исмини ёзинг:**", parse_mode="Markdown", reply_markup=cancel_inline_keyboard())
    return PAY_SEARCH

async def pay_search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await search_and_select(update, context, PAY_AMOUNT, "pay")

async def pay_amount_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip())
        debt_id = context.user_data['selected_debt_id']
        debt = await asyncio.to_thread(get_debt, debt_id)
        old_bal = debt['remaining_balance']
        
        status = await asyncio.to_thread(add_payment, debt_id, amount)
        
        if status == "paid_off":
            await update.message.reply_text(f"🎉 ✅ **Тўлов қабул қилинди!**\nҚарз тўлиқ ёпилди ва базадан ўчирилди.", parse_mode="Markdown")
            await notify_group(context, "ТЎЛОВ (ҚАРЗ ЁПИЛДИ)", debt['customer_name'], amount, update.effective_user.first_name, "", old_bal, 0.0)
        elif status == "updated":
            new_bal = old_bal - amount
            await update.message.reply_text(f"✅ **Тўлов қабул қилинди!**\n📊 Янги қолдиқ: {new_bal:,.2f} сўм", parse_mode="Markdown")
            await notify_group(context, "ТЎЛОВ ҚАБУЛ ҚИЛИНДИ", debt['customer_name'], amount, update.effective_user.first_name, "", old_bal, new_bal)
        else:
            await update.message.reply_text("❌ Хато: Тўлов суммаси қарз қолдиғидан катта бўлиши мумкин эмас.", reply_markup=cancel_inline_keyboard())
            return PAY_AMOUNT
            
    except ValueError:
        await update.message.reply_text("❌ Илтимос, тўғри сумма киритинг:", reply_markup=cancel_inline_keyboard())
        return PAY_AMOUNT
    context.user_data.clear()
    return ConversationHandler.END

# ---------- SEARCH & GENERAL HANDLERS ----------
async def search_debt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 **Қидирилаётган мижоз исмини ёзинг:**", parse_mode="Markdown", reply_markup=cancel_inline_keyboard())
    return SEARCH_QUERY

async def search_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    debts = await asyncio.to_thread(search_debts, text)
    if not debts:
        await update.message.reply_text("🔍 Мос келувчи фаол мижож топилмади.")
    else:
        msg = "🔍 **Қидирув натижалари:**\n\n"
        for d in debts[:15]:
            msg += f"👤 **{d['customer_name']}** | 💰 {d['remaining_balance']:,.2f} сўм\n📝 Изоҳ: {d['notes'] or '-'}\n\n"
        await update.message.reply_text(msg, parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END

async def list_debts_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    debts = await asyncio.to_thread(get_all_debts)
    if not debts:
        await update.message.reply_text("📋 Базада фаол қарздорлар йўқ.")
    else:
        msg = "📋 **Қарздорлар рўйхати:**\n\n"
        for d in debts[:20]:
            msg += f"👤 **{d['customer_name']}** | 💰 {d['remaining_balance']:,.2f} сўм\n📝 Изоҳ: {d['notes'] or '-'}\n\n"
        await update.message.reply_text(msg, parse_mode="Markdown")

async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total, largest = await asyncio.to_thread(get_stats)
    
    msg = f"📊 **Умумий Статистика**\n\n💸 Жамланган фаол қарз: **{total:,.2f} сўм**\n\n"
    msg += "**Энг йирик қарздорлар:**\n"
    for d in largest:
        msg += f"• {d['name']}: {d['total']:,.2f} сўм\n"
    
    await update.message.reply_text(msg, parse_mode="Markdown")

async def send_backup_to_group_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    debts = await asyncio.to_thread(get_all_debts)
    if not debts:
        await update.message.reply_text("Базада фаол қарздорлик мавжуд эмас.")
        return
        
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Name", "Notes", "Current Debt Status", "Date Added"])
    for d in debts: 
        writer.writerow([d["id"], d["customer_name"], d["notes"] or "-", f"{d['remaining_balance']:.2f}", d["created_at"].strftime('%d.%m.%Y')])
    output.seek(0)
    
    if BACKUP_GROUP_ID:
        try:
            kwargs = {
                "chat_id": int(BACKUP_GROUP_ID),
                "document": io.BytesIO(output.getvalue().encode()),
                "filename": f"Qarzlar_Backup_{get_current_time().strftime('%d_%m_%Y')}.csv",
                "caption": f"📢 **БАЗА БЭКАПИ**\n👤 Масъул: {update.effective_user.first_name}\n📅 Сана: {get_current_time().strftime('%d.%m.%Y %H:%M')}",
                "parse_mode": "Markdown"
            }
            if BACKUP_TOPIC_ID: kwargs["message_thread_id"] = int(BACKUP_TOPIC_ID)
            await context.bot.send_document(**kwargs)
            await update.message.reply_text("📢 Тўлиқ CSV бэкап файли гуруҳга муваффақиятли юборилди!")
        except Exception as e:
            logging.error(f"Backup group send error: {e}")
            await update.message.reply_text("❌ Гуруҳга бэкап юборишда хатолик юз берди.")
    else:
        await update.message.reply_text("❌ Бэкап гуруҳ созланмаган.")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("🚫 Амал бекор қилинди.")
    return ConversationHandler.END

# ---------- Bot Runner ----------
def run_telegram_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    req = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    app = Application.builder().token(BOT_TOKEN).request(req).build()

    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^➕ Янги мизож ва қарз$"), add_debt_start),
            MessageHandler(filters.Regex("^➕ Мавжуд мизожга қарз$"), exist_debt_start),
            MessageHandler(filters.Regex("^💰 Тўлов қабул қилиш$"), pay_debt_start),
            MessageHandler(filters.Regex("^🔍 Qарзларни излаш$"), search_debt_start),
            MessageHandler(filters.Regex("^📋 Барча қарзлар рўйхати$"), list_debts_handler),
            MessageHandler(filters.Regex("^📊 Статистика$"), stats_handler),
            MessageHandler(filters.Regex("^📢 Гуруҳга Бэкап юбориш$"), send_backup_to_group_handler),
        ],
        states={
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name_handler)],
            ADD_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_amount_handler)],
            ADD_NOTES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_notes_text_handler),
                CallbackQueryHandler(add_notes_callback_handler, pattern="^skip_notes$")
            ],
            EXIST_SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, exist_search_handler)],
            EXIST_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, exist_amount_handler)],
            PAY_SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, pay_search_handler)],
            PAY_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, pay_amount_handler)],
            SEARCH_QUERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, search_query_handler)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel), 
            MessageHandler(filters.Regex("^❌ Амални бекор қилиш$"), cancel),
            CallbackQueryHandler(cancel_callback, pattern="^cancel_action$"),
            CallbackQueryHandler(select_debt_callback, pattern="^(exist|pay)_")
        ]
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("start", start))
    app.run_polling(stop_signals=None)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    init_db()
    bot_thread = threading.Thread(target=run_telegram_bot)
    bot_thread.daemon = True
    bot_thread.start()
    
    port = int(os.environ.get('PORT', 5000))
    flask_app.run(host='0.0.0.0', port=port)
