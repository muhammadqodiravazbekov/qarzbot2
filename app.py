import os
import re
import logging
import asyncio
import threading
import unicodedata
import psycopg2
from psycopg2.pool import ThreadedConnectionPool
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ConversationHandler,
    MessageHandler, CallbackQueryHandler, filters, ContextTypes
)
from telegram.request import HTTPXRequest

# ==========================================
# 1. CONFIGURATION & TIMEZONE SETUP
# ==========================================
BOT_TOKEN = os.environ.get('BOT_TOKEN')
DATABASE_URL = os.environ.get('DATABASE_URL')
BACKUP_GROUP_ID = os.environ.get('BACKUP_GROUP_ID')
BACKUP_TOPIC_ID = os.environ.get('BACKUP_TOPIC_ID')

if not BOT_TOKEN or not DATABASE_URL:
    raise ValueError("CRITICAL ERROR: BOT_TOKEN or DATABASE_URL environment variables are missing!")

UZB_TZ = timezone(timedelta(hours=5))

def get_current_time(): 
    return datetime.now(UZB_TZ)

# ==========================================
# 2. FLASK WEB SERVER (Render Keep-Alive)
# ==========================================
flask_app = Flask(__name__)

@flask_app.route('/')
@flask_app.route('/health')
def health(): 
    return jsonify({"status": "online", "timestamp": get_current_time().isoformat()}), 200

# ==========================================
# 3. DATABASE CONNECTION POOL & INIT
# ==========================================
db_pool = ThreadedConnectionPool(1, 20, dsn=DATABASE_URL)

@contextmanager
def get_db(commit=False):
    conn = db_pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            yield cursor
        if commit:
            conn.commit()
    except Exception as e:
        conn.rollback()
        logging.error(f"Database transaction rolled back: {e}")
        raise e
    finally:
        db_pool.putconn(conn)

def init_db():
    with get_db(commit=True) as cursor:
        cursor.execute('''CREATE TABLE IF NOT EXISTS users (
            telegram_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT,
            role TEXT CHECK(role IN ('admin','seller','viewer')) NOT NULL, created_at TIMESTAMP)''')
        
        cursor.execute('''CREATE TABLE IF NOT EXISTS ledger_customers (
            id SERIAL PRIMARY KEY, name TEXT NOT NULL, name_normalized TEXT,
            balance REAL NOT NULL DEFAULT 0, created_at TIMESTAMP, updated_at TIMESTAMP)''')
        
        cursor.execute('''CREATE TABLE IF NOT EXISTS ledger_transactions (
            id SERIAL PRIMARY KEY, customer_id INTEGER REFERENCES ledger_customers(id) ON DELETE CASCADE,
            t_type TEXT CHECK(t_type IN ('debt', 'payment')) NOT NULL,
            amount REAL NOT NULL, note TEXT, seller_username TEXT NOT NULL, created_at TIMESTAMP)''')
            
        # Performance Indexing for fast searching
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_customer_name_norm ON ledger_customers (name_normalized);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_customer_balance ON ledger_customers (balance);")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_trans_customer_id ON ledger_transactions (customer_id);")

# ==========================================
# 4. UTILITY & HELPER FUNCTIONS
# ==========================================
def normalize_text(text: str) -> str:
    """Converts Cyrillic to Latin, lowercases, and strips specials for multi-script search."""
    if not text: return ""
    cyrillic_to_latin = {
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo', 'ж': 'j', 'з': 'z', 'и': 'i', 'й': 'y', 
        'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u', 'ф': 'f', 
        'х': 'x', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sh', 'ъ': '', 'ы': 'i', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
        'ў': 'o', 'қ': 'k', 'ғ': 'g', 'ҳ': 'x', 'нг': 'ng'
    }
    normalized = text.lower()
    for cyr, lat in cyrillic_to_latin.items(): 
        normalized = normalized.replace(cyr, lat)
    normalized = normalized.replace("'", "")
    normalized = unicodedata.normalize('NFKD', normalized).encode('ASCII', 'ignore').decode('ASCII')
    return re.sub(r'[^a-z0-9]', '', normalized)

def extract_amount(text: str) -> float:
    """Intelligently parses money input like '50 000 сум' or '25,000' to pure float."""
    cleaned = re.sub(r'[^\d.]', '', text.replace(',', '.'))
    if not cleaned: raise ValueError("Invalid amount")
    return float(cleaned)

def get_seller_username(user) -> str:
    if user.username: return f"@{user.username}"
    return f"@id_{user.id}"

async def notify_group(context: ContextTypes.DEFAULT_TYPE, action: str, customer: str, amount: float, seller_username: str, note: str = "", new_bal: float = None):
    if not BACKUP_GROUP_ID: return
    msg = f"📢 **{action}**\n\n👤 Мижоз: {customer}\n💰 Сумма: {amount:,.0f} сўм\n"
    if new_bal is not None: msg += f"📊 Янги умумий қолдиқ: {new_bal:,.0f} сўм\n"
    msg += f"📝 Изоҳ: {note or '-'}\n💼 Сотувчи: {seller_username}\n🕒 Вақт: {get_current_time().strftime('%d.%m.%Y %H:%M')}"
    
    try:
        kwargs = {"chat_id": int(BACKUP_GROUP_ID), "text": msg, "parse_mode": "Markdown"}
        if BACKUP_TOPIC_ID: kwargs["message_thread_id"] = int(BACKUP_TOPIC_ID)
        await context.bot.send_message(**kwargs)
    except Exception as e: 
        logging.error(f"Group backup notification failed: {e}")

# ==========================================
# 5. CORE DATABASE QUERIES
# ==========================================
def get_user(telegram_id: int):
    with get_db() as cursor:
        cursor.execute("SELECT * FROM users WHERE telegram_id = %s", (telegram_id,))
        return cursor.fetchone()

def create_user(telegram_id: int, username: str, first_name: str, role: str) -> bool:
    try:
        with get_db(commit=True) as cursor:
            cursor.execute("INSERT INTO users (telegram_id, username, first_name, role, created_at) VALUES (%s, %s, %s, %s, %s)",
                           (telegram_id, username, first_name, role, get_current_time()))
            return True
    except psycopg2.IntegrityError: return False

def get_all_users():
    with get_db() as cursor:
        cursor.execute("SELECT * FROM users ORDER BY created_at")
        return cursor.fetchall()

def add_new_customer_and_debt(name: str, amount: float, note: str, seller_username: str):
    now = get_current_time()
    norm_name = normalize_text(name)
    with get_db(commit=True) as cursor:
        cursor.execute("INSERT INTO ledger_customers (name, name_normalized, balance, created_at, updated_at) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                       (name, norm_name, amount, now, now))
        cust_id = cursor.fetchone()['id']
        cursor.execute("INSERT INTO ledger_transactions (customer_id, t_type, amount, note, seller_username, created_at) VALUES (%s, 'debt', %s, %s, %s, %s)",
                       (cust_id, amount, note, seller_username, now))
        return cust_id

def process_ledger_transaction(customer_id: int, t_type: str, amount: float, note: str, seller_username: str):
    now = get_current_time()
    with get_db(commit=True) as cursor:
        cursor.execute("SELECT balance FROM ledger_customers WHERE id = %s", (customer_id,))
        current_bal = cursor.fetchone()['balance']
        new_bal = (current_bal + amount) if t_type == 'debt' else (current_bal - amount)
        
        cursor.execute("INSERT INTO ledger_transactions (customer_id, t_type, amount, note, seller_username, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
                       (customer_id, t_type, amount, note, seller_username, now))
        cursor.execute("UPDATE ledger_customers SET balance = %s, updated_at = %s WHERE id = %s", (new_bal, now, customer_id))
        return new_bal

def get_customer(customer_id: int):
    with get_db() as cursor:
        cursor.execute("SELECT * FROM ledger_customers WHERE id = %s", (customer_id,))
        return cursor.fetchone()

def search_customers(query: str):
    norm_query = normalize_text(query)
    with get_db() as cursor:
        cursor.execute("""
            SELECT * FROM ledger_customers 
            WHERE (name ILIKE %s OR name_normalized LIKE %s)
            ORDER BY updated_at DESC LIMIT 8
        """, (f"%{query}%", f"%{norm_query}%"))
        return cursor.fetchall()

def get_customer_history(customer_id: int, limit: int = 20):
    with get_db() as cursor:
        cursor.execute("SELECT * FROM ledger_transactions WHERE customer_id = %s ORDER BY created_at DESC LIMIT %s", (customer_id, limit))
        return cursor.fetchall()

def get_daily_stats():
    now = get_current_time()
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    with get_db() as cursor:
        cursor.execute("SELECT COALESCE(SUM(balance), 0) as total FROM ledger_customers WHERE balance > 0")
        total = cursor.fetchone()['total']
        
        cursor.execute("SELECT COALESCE(SUM(amount), 0) as daily_debt FROM ledger_transactions WHERE t_type='debt' AND created_at >= %s", (start_of_day,))
        daily_debt = cursor.fetchone()['daily_debt']
        
        cursor.execute("SELECT COALESCE(SUM(amount), 0) as daily_pay FROM ledger_transactions WHERE t_type='payment' AND created_at >= %s", (start_of_day,))
        daily_pay = cursor.fetchone()['daily_pay']
        
        cursor.execute("SELECT name, balance FROM ledger_customers WHERE balance > 0 ORDER BY balance DESC LIMIT 5")
        top_debtors = cursor.fetchall()
        return total, daily_debt, daily_pay, top_debtors

# ==========================================
# 6. UI KEYBOARDS & CONSTANTS
# ==========================================
(
    ADD_NAME, ADD_AMOUNT, ADD_NOTES,
    EXIST_SEARCH, EXIST_SELECT, EXIST_AMOUNT, EXIST_NOTES,
    PAY_SEARCH, PAY_SELECT, PAY_AMOUNT, PAY_NOTES,
    SEARCH_QUERY
) = range(12)

def get_main_reply_keyboard() -> ReplyKeyboardMarkup:
    kb = [
        [KeyboardButton("➕ Янги мизож ва қарз"), KeyboardButton("➕ Мавжуд мизожга қарз")],
        [KeyboardButton("💰 Тўлов қабул қилиш"), KeyboardButton("🔍 Қарзларни излаш")],
        [KeyboardButton("📊 Статистика"), KeyboardButton("❌ Амални бекор қилиш")]
    ]
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)

def cancel_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Бекор қилиш", callback_data="cancel_action")]])

# ==========================================
# 7. BOT HANDLERS & LOGIC
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = await asyncio.to_thread(get_user, user.id)
    if not db_user:
        if not await asyncio.to_thread(get_all_users):
            await asyncio.to_thread(create_user, user.id, user.username or "", user.first_name or "", "admin")
            await update.message.reply_text("✅ Тизим базаси бўш. Сиз АДМИН этиб тайинландингиз.", reply_markup=get_main_reply_keyboard())
        else:
            await update.message.reply_text("❌ Кириш тақиқланган. Асосий сотувчи сизни рўйхатга киритиши керак.")
        return
    await update.message.reply_text("🛒 Мини-маркет Бухгалтерия Тизими тайёр:", reply_markup=get_main_reply_keyboard())

async def cancel_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("🚫 Жорий амалиёт бекор қилинди.")
    else:
        await update.message.reply_text("🚫 Жорий амалиёт бекор қилинди.", reply_markup=get_main_reply_keyboard())
    return ConversationHandler.END

# --- FLOW 1: NEW CUSTOMER ---
async def add_debt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👤 **Янги мизожнинг Исм/Фамилиясини киритинг:**", parse_mode="Markdown", reply_markup=cancel_inline_keyboard())
    return ADD_NAME

async def add_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['debt_name'] = update.message.text.strip()
    await update.message.reply_text("💰 **Қарз суммасини киритинг (масалан: 50000):**", parse_mode="Markdown", reply_markup=cancel_inline_keyboard())
    return ADD_AMOUNT

async def add_amount_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = extract_amount(update.message.text)
        if amount <= 0: raise ValueError()
        context.user_data['debt_amount'] = amount
        await update.message.reply_text("📝 **Ушбу қарз учун мажбурий изоҳ қолдиринг:**", parse_mode="Markdown", reply_markup=cancel_inline_keyboard())
        return ADD_NOTES
    except ValueError:
        await update.message.reply_text("❌ Илтимос тўғри мусбат сон киритинг:", reply_markup=cancel_inline_keyboard())
        return ADD_AMOUNT

async def add_notes_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    note = update.message.text.strip()
    name = context.user_data['debt_name']
    amount = context.user_data['debt_amount']
    seller = get_seller_username(update.effective_user)
    
    await asyncio.to_thread(add_new_customer_and_debt, name, amount, note, seller)
    await notify_group(context, "ЯНГИ МИЖОЗ ВА ҚАРЗ", name, amount, seller, note, amount)
    
    await update.message.reply_text(f"✅ **Сақланди!**\n\n👤 Мижоз: {name}\n💰 Қарз: {amount:,.0f} сўм\n📝 Изоҳ: {note}\n💼 Сотувчи: {seller}", parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END

# --- FLOW 2: EXISTING CUSTOMER DEBT ---
async def exist_debt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 **Қарз қўшиладиган мизож исмини қидиринг:**", parse_mode="Markdown", reply_markup=cancel_inline_keyboard())
    return EXIST_SEARCH

async def exist_search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    results = await asyncio.to_thread(search_customers, query)
    if not results:
        await update.message.reply_text("❌ Топилмади. Қайтадан қидириб кўринг:", reply_markup=cancel_inline_keyboard())
        return EXIST_SEARCH
        
    buttons = [[InlineKeyboardButton(f"{r['name']} ({r['balance']:,.0f} сўм)", callback_data=f"exist_{r['id']}")] for r in results]
    buttons.append([InlineKeyboardButton("❌ Бекор қилиш", callback_data="cancel_action")])
    await update.message.reply_text("👇 **Қуйидаги рўйхатдан танланг:**", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
    return EXIST_SELECT

async def select_exist_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, cust_id = query.data.split("_")
    context.user_data['selected_cust_id'] = int(cust_id)
    
    cust = await asyncio.to_thread(get_customer, int(cust_id))
    await query.edit_message_text(f"👤 Мижоз: **{cust['name']}**\n📊 Жорий: {cust['balance']:,.0f} сўм\n\n💰 **Қўшиладиган янги қарз суммасини киритинг:**", parse_mode="Markdown")
    return EXIST_AMOUNT

async def exist_amount_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = extract_amount(update.message.text)
        if amount <= 0: raise ValueError()
        context.user_data['add_amount'] = amount
        await update.message.reply_text("📝 **Изоҳ ёзинг (Нима маҳсулот олинди?):**", parse_mode="Markdown", reply_markup=cancel_inline_keyboard())
        return EXIST_NOTES
    except ValueError:
        await update.message.reply_text("❌ Илтимос тўғри сон киритинг:", reply_markup=cancel_inline_keyboard())
        return EXIST_AMOUNT

async def exist_notes_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    note = update.message.text.strip()
    cust_id = context.user_data['selected_cust_id']
    amount = context.user_data['add_amount']
    seller = get_seller_username(update.effective_user)
    
    cust = await asyncio.to_thread(get_customer, cust_id)
    new_bal = await asyncio.to_thread(process_ledger_transaction, cust_id, 'debt', amount, note, seller)
    await notify_group(context, "ҚАРЗ ҚЎШИЛДИ", cust['name'], amount, seller, note, new_bal)
    
    await update.message.reply_text(f"✅ **Қўшилди!**\n\n👤 Мижоз: {cust['name']}\n➕ Қўшилди: {amount:,.0f} сўм\n📝 Изоҳ: {note}\n📊 Янги баланс: {new_bal:,.0f} сўм", parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END

# --- FLOW 3: PAYMENTS ---
async def pay_debt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 **Тўлов қилаётган мизож исмини қидиринг:**", parse_mode="Markdown", reply_markup=cancel_inline_keyboard())
    return PAY_SEARCH

async def pay_search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    results = await asyncio.to_thread(search_customers, query)
    if not results:
        await update.message.reply_text("❌ Топилмади. Қайтадан қидириб кўринг:", reply_markup=cancel_inline_keyboard())
        return PAY_SEARCH
        
    buttons = [[InlineKeyboardButton(f"{r['name']} ({r['balance']:,.0f} сўм)", callback_data=f"pay_{r['id']}")] for r in results]
    buttons.append([InlineKeyboardButton("❌ Бекор қилиш", callback_data="cancel_action")])
    await update.message.reply_text("👇 **Тўлов қилувчини танланг:**", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
    return PAY_SELECT

async def select_pay_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, cust_id = query.data.split("_")
    context.user_data['selected_cust_id'] = int(cust_id)
    
    cust = await asyncio.to_thread(get_customer, int(cust_id))
    buttons = [
        [InlineKeyboardButton(f"💵 Тўлиқ ёпиш ({cust['balance']:,.0f})", callback_data=f"payfull_{cust['balance']}_{cust_id}")],
        [InlineKeyboardButton("❌ Бекор қилиш", callback_data="cancel_action")]
    ]
    await query.edit_message_text(f"👤 Мижоз: **{cust['name']}**\n💸 Умумий қарзи: {cust['balance']:,.0f} сўм\n\n💵 **Тўланаётган суммани киритинг ёки пастдаги тугмани босинг:**", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
    return PAY_AMOUNT

async def pay_amount_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = extract_amount(update.message.text)
        cust_id = context.user_data['selected_cust_id']
        cust = await asyncio.to_thread(get_customer, cust_id)
        
        if amount <= 0: raise ValueError()
        context.user_data['pay_amount'] = amount
        await update.message.reply_text("📝 **Тўлов усулини ёзинг (Нақд, Карта):**", parse_mode="Markdown", reply_markup=cancel_inline_keyboard())
        return PAY_NOTES
    except ValueError:
        await update.message.reply_text("❌ Илтимос тўғри мусбат сон киритинг:", reply_markup=cancel_inline_keyboard())
        return PAY_AMOUNT

async def pay_full_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    _, amount_str, cust_id_str = query.data.split("_")
    context.user_data['selected_cust_id'] = int(cust_id_str)
    context.user_data['pay_amount'] = float(amount_str)
    
    await query.edit_message_text("📝 **Тўлиқ тўлов усулини ёзинг (Нақд, Карта):**", reply_markup=cancel_inline_keyboard())
    return PAY_NOTES

async def pay_notes_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    note = update.message.text.strip()
    cust_id = context.user_data['selected_cust_id']
    amount = context.user_data['pay_amount']
    seller = get_seller_username(update.effective_user)
    
    cust = await asyncio.to_thread(get_customer, cust_id)
    new_bal = await asyncio.to_thread(process_ledger_transaction, cust_id, 'payment', amount, note, seller)
    await notify_group(context, "ТЎЛОВ ҚАБУЛ ҚИЛИНДИ", cust['name'], amount, seller, note, new_bal)
    
    await update.message.reply_text(f"✅ **Тўлов қабул қилинди!**\n\n👤 Мижоз: {cust['name']}\n🟢 Тўланди: {amount:,.0f} сўм\n📝 Изоҳ: {note}\n📊 Қолган қарздорлик: {new_bal:,.0f} сўм\n💼 Сотувчи: {seller}", parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END

# --- FLOW 4: SEARCH DETAILED DEBT ---
async def search_debt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 **Тарихини кўрмоқчи бўлган мижоз исмини ёзинг:**", parse_mode="Markdown", reply_markup=cancel_inline_keyboard())
    return SEARCH_QUERY

async def search_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.message.text.strip()
    customers = await asyncio.to_thread(search_customers, query)
    if not customers:
        await update.message.reply_text("❌ Бундай мижоз топилмади.")
        return ConversationHandler.END
        
    for c in customers:
        history = await asyncio.to_thread(get_customer_history, c['id'], 15)
        msg = f"👤 **Мижоз:** {c['name']}\n📊 **Жорий Қарздорлик:** `{c['balance']:,.0f}` сўм\n\n📜 **Тарих (охирги 15 та):**\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
        
        if not history:
            msg += "_Тарих бўш._"
        else:
            for h in history:
                date_str = h['created_at'].strftime('%d.%m.%Y %H:%M')
                sign = "🔴 Қарз:" if h['t_type'] == 'debt' else "🟢 Тўлов:"
                msg += f"{sign} `{h['amount']:,.0f}`\n📝 {h['note'] or '-'}\n💼 {h['seller_username']} | 🕒 {date_str}\n⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯⎯\n"
                
        await update.message.reply_text(msg, parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END

# --- SYSTEM STATS ---
async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total, daily_debt, daily_pay, largest = await asyncio.to_thread(get_daily_stats)
    
    msg = f"📊 **Бухгалтерия Статистикаси** 📊\n\n"
    msg += f"🗓 **БУГУНГИ ҲИСОБОТ:**\n"
    msg += f"➕ Берилган янги қарзлар: `{daily_debt:,.0f}` сўм\n"
    msg += f"💵 Қабул қилинган тўловлар: `{daily_pay:,.0f}` сўм\n\n"
    
    msg += f"🏢 **УМУМИЙ:**\n"
    msg += f"💰 Фаол қарзлар йиғиндиси: `{total:,.0f}` сўм\n\n"
    
    msg += "🔥 **Энг катта қарздорлар бешлиги:**\n"
    for idx, d in enumerate(largest, 1):
        msg += f"{idx}. {d['name']} — `{d['balance']:,.0f}` сўм\n"
        
    await update.message.reply_text(msg, parse_mode="Markdown")

# ==========================================
# 8. TELEGRAM BOT ENGINE INITIALIZATION
# ==========================================
def run_telegram_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = Application.builder().token(BOT_TOKEN).request(HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)).build()

    # Filter out navigation commands so text handlers don't catch them
    nav_filter = ~(filters.Regex("^(➕ Янги мизож ва қарз|➕ Мавжуд мизожга қарз|💰 Тўлов қабул қилиш|🔍 Қарзларни излаш|📊 Статистика|❌ Амални бекор қилиш)$") | filters.COMMAND)

    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^➕ Янги мизож ва қарз$"), add_debt_start),
            MessageHandler(filters.Regex("^➕ Мавжуд мизожга қарз$"), exist_debt_start),
            MessageHandler(filters.Regex("^💰 Тўлов қабул қилиш$"), pay_debt_start),
            MessageHandler(filters.Regex("^🔍 Қарзларни излаш$"), search_debt_start),
            MessageHandler(filters.Regex("^📊 Статистика$"), stats_handler),
        ],
        states={
            ADD_NAME: [MessageHandler(filters.TEXT & nav_filter, add_name_handler)],
            ADD_AMOUNT: [MessageHandler(filters.TEXT & nav_filter, add_amount_handler)],
            ADD_NOTES: [MessageHandler(filters.TEXT & nav_filter, add_notes_text_handler)],
            
            EXIST_SEARCH: [MessageHandler(filters.TEXT & nav_filter, exist_search_handler)],
            EXIST_SELECT: [CallbackQueryHandler(select_exist_callback, pattern="^exist_")],
            EXIST_AMOUNT: [MessageHandler(filters.TEXT & nav_filter, exist_amount_handler)],
            EXIST_NOTES: [MessageHandler(filters.TEXT & nav_filter, exist_notes_text_handler)],
            
            PAY_SEARCH: [MessageHandler(filters.TEXT & nav_filter, pay_search_handler)],
            PAY_SELECT: [CallbackQueryHandler(select_pay_callback, pattern="^pay_")],
            PAY_AMOUNT: [
                MessageHandler(filters.TEXT & nav_filter, pay_amount_text_handler),
                CallbackQueryHandler(pay_full_callback_handler, pattern="^payfull_")
            ],
            PAY_NOTES: [MessageHandler(filters.TEXT & nav_filter, pay_notes_text_handler)],
            
            SEARCH_QUERY: [MessageHandler(filters.TEXT & nav_filter, search_query_handler)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_action), 
            MessageHandler(filters.Regex("^❌ Амални бекор қилиш$"), cancel_action),
            CallbackQueryHandler(cancel_action, pattern="^cancel_action$"),
            
            # Universal fallback for main menu buttons pressed mid-flow
            MessageHandler(filters.Regex("^➕ Янги мизож ва қарз$"), add_debt_start),
            MessageHandler(filters.Regex("^➕ Мавжуд мизожга қарз$"), exist_debt_start),
            MessageHandler(filters.Regex("^💰 Тўлов қабул қилиш$"), pay_debt_start),
            MessageHandler(filters.Regex("^🔍 Қарзларни излаш$"), search_debt_start),
            MessageHandler(filters.Regex("^📊 Статистика$"), stats_handler),
        ],
        allow_reentry=True
    )
    
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("start", start))
    
    logging.info("Starting Telegram Bot Engine...")
    app.run_polling(stop_signals=None)

# ==========================================
# 9. EXECUTION ENTRY POINT
# ==========================================
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    
    # Initialize the Database Schema
    init_db()
    
    # Run Telegram Bot in Background Thread
    bot_thread = threading.Thread(target=run_telegram_bot, daemon=True)
    bot_thread.start()
    
    # Run Flask Web Server on Main Thread (required by Render)
    port = int(os.environ.get('PORT', 5000))
    flask_app.run(host='0.0.0.0', port=port)
