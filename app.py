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
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Optional
from flask import Flask, jsonify
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ConversationHandler,
    MessageHandler, CallbackQueryHandler, filters, ContextTypes
)
from telegram.request import HTTPXRequest

# ---------- Configuration & Timezone ----------
BOT_TOKEN = os.environ.get('BOT_TOKEN')
DATABASE_URL = os.environ.get('DATABASE_URL')
BACKUP_GROUP_ID = os.environ.get('BACKUP_GROUP_ID')
BACKUP_TOPIC_ID = os.environ.get('BACKUP_TOPIC_ID')

if not BOT_TOKEN or not DATABASE_URL:
    raise ValueError("CRITICAL ERROR: BOT_TOKEN or DATABASE_URL variables are missing!")

UZB_TZ = timezone(timedelta(hours=5))
def get_current_time(): 
    return datetime.now(UZB_TZ)

# ---------- Flask Web Server ----------
flask_app = Flask(__name__)
@flask_app.route('/')
@flask_app.route('/health')
def health(): 
    return jsonify({"status": "alive"}), 200

# ---------- Database Setup ----------
db_pool = ThreadedConnectionPool(1, 20, dsn=DATABASE_URL)

@contextmanager
def get_db(commit=False):
    conn = db_pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            yield cursor
        if commit:
            conn.commit()
    finally:
        db_pool.putconn(conn)

# ---------- Conversation States ----------
(
    ADD_NAME, ADD_AMOUNT, ADD_NOTES,
    EXIST_SEARCH, EXIST_AMOUNT, EXIST_NOTES,
    PAY_SEARCH, PAY_AMOUNT,
    SEARCH_QUERY, USER_ID, USER_ROLE
) = range(11)

# ---------- Helper Functions ----------
def normalize_text(text: str) -> str:
    if not text: return ""
    cyrillic_to_latin = {
        'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo', 'ж': 'j', 'з': 'z', 'и': 'i', 'й': 'y', 
        'к': 'k', 'л': 'l', 'м': 'm', 'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u', 'ф': 'f', 
        'х': 'x', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'sh', 'ъ': '', 'ы': 'i', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
        'ў': 'o', 'қ': 'k', 'ғ': 'g', 'ҳ': 'x', 'нг': 'ng'
    }
    normalized = text.lower()
    for cyr, lat in cyrillic_to_latin.items(): normalized = normalized.replace(cyr, lat)
    normalized = normalized.replace("'", "")
    normalized = unicodedata.normalize('NFKD', normalized).encode('ASCII', 'ignore').decode('ASCII')
    return re.sub(r'[^a-z0-9]', '', normalized)

def get_seller_identifier(user):
    """Gets @username if available, otherwise first name."""
    return f"@{user.username}" if user.username else user.first_name

async def notify_group(context: ContextTypes.DEFAULT_TYPE, action: str, customer: str, amount: float, seller: str, note: str = "", new_bal: float = None):
    if not BACKUP_GROUP_ID: return
    msg = f"📢 **{action}**\n\n👤 Мижоз: {customer}\n💰 Сумма: {amount:,.2f} сўм\n"
    if new_bal is not None: 
        msg += f"📊 Янги Қолдиқ: {new_bal:,.2f} сўм\n"
    msg += f"📝 Изоҳ: {note or '-'}\n💼 Сотувчи: {seller}\n🕒 Вақт: {get_current_time().strftime('%d.%m.%Y %H:%M')}"
    
    try:
        kwargs = {"chat_id": int(BACKUP_GROUP_ID), "text": msg, "parse_mode": "Markdown"}
        if BACKUP_TOPIC_ID: kwargs["message_thread_id"] = int(BACKUP_TOPIC_ID)
        await context.bot.send_message(**kwargs)
    except Exception as e: 
        logging.error(f"Group notification failed: {e}")

# ---------- Core Ledger Database Logic ----------
def init_db():
    with get_db(commit=True) as cursor:
        cursor.execute('''CREATE TABLE IF NOT EXISTS users (
            telegram_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT,
            role TEXT CHECK(role IN ('admin','seller','viewer')) NOT NULL, created_at TIMESTAMP)''')
        
        # Customers Table (Holds current balance)
        cursor.execute('''CREATE TABLE IF NOT EXISTS customers (
            id SERIAL PRIMARY KEY, name TEXT NOT NULL, name_normalized TEXT,
            balance REAL NOT NULL DEFAULT 0, created_at TIMESTAMP, updated_at TIMESTAMP)''')
        
        # Transactions Table (The Ledger: Holds every individual debt taken or paid)
        cursor.execute('''CREATE TABLE IF NOT EXISTS transactions (
            id SERIAL PRIMARY KEY, customer_id INTEGER REFERENCES customers(id) ON DELETE CASCADE,
            t_type TEXT CHECK(t_type IN ('debt', 'payment')) NOT NULL,
            amount REAL NOT NULL, note TEXT, seller_identifier TEXT, created_at TIMESTAMP)''')

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

def add_new_customer_and_debt(name: str, amount: float, note: str, seller_identifier: str):
    now = get_current_time()
    norm_name = normalize_text(name)
    with get_db(commit=True) as cursor:
        cursor.execute("INSERT INTO customers (name, name_normalized, balance, created_at, updated_at) VALUES (%s, %s, %s, %s, %s) RETURNING id",
                       (name, norm_name, amount, now, now))
        cust_id = cursor.fetchone()['id']
        cursor.execute("INSERT INTO transactions (customer_id, t_type, amount, note, seller_identifier, created_at) VALUES (%s, 'debt', %s, %s, %s, %s)",
                       (cust_id, amount, note, seller_identifier, now))
        return cust_id

def process_ledger_transaction(customer_id: int, t_type: str, amount: float, note: str, seller_identifier: str):
    now = get_current_time()
    with get_db(commit=True) as cursor:
        cursor.execute("SELECT balance FROM customers WHERE id = %s", (customer_id,))
        current_bal = cursor.fetchone()['balance']
        
        if t_type == 'debt': new_bal = current_bal + amount
        else: new_bal = current_bal - amount # payment
        
        # Create transaction record
        cursor.execute("INSERT INTO transactions (customer_id, t_type, amount, note, seller_identifier, created_at) VALUES (%s, %s, %s, %s, %s, %s)",
                       (customer_id, t_type, amount, note, seller_identifier, now))
        
        # Update customer global balance
        cursor.execute("UPDATE customers SET balance = %s, updated_at = %s WHERE id = %s", (new_bal, now, customer_id))
        return new_bal

def get_customer(customer_id: int):
    with get_db() as cursor:
        cursor.execute("SELECT * FROM customers WHERE id = %s", (customer_id,))
        return cursor.fetchone()

def search_customers(query: str):
    norm_query = normalize_text(query)
    with get_db() as cursor:
        cursor.execute("""
            SELECT * FROM customers 
            WHERE (name ILIKE %s OR name_normalized LIKE %s)
            AND balance > 0.01 ORDER BY updated_at DESC
        """, (f"%{query}%", f"%{norm_query}%"))
        return cursor.fetchall()

def get_customer_history(customer_id: int, limit: int = 15):
    with get_db() as cursor:
        cursor.execute("SELECT * FROM transactions WHERE customer_id = %s ORDER BY created_at DESC LIMIT %s", (customer_id, limit))
        return cursor.fetchall()

def get_all_active_customers():
    with get_db() as cursor:
        cursor.execute("SELECT * FROM customers WHERE balance > 0.01 ORDER BY updated_at DESC")
        return cursor.fetchall()

def get_stats():
    with get_db() as cursor:
        cursor.execute("SELECT COALESCE(SUM(balance), 0) as total FROM customers WHERE balance > 0.01")
        total = cursor.fetchone()['total']
        cursor.execute("SELECT name, balance FROM customers WHERE balance > 0.01 ORDER BY balance DESC LIMIT 5")
        return total, cursor.fetchall()

# ---------- UI & Menus ----------
def get_main_reply_keyboard(role: str) -> ReplyKeyboardMarkup:
    kb = [
        [KeyboardButton("➕ Янги мизож ва қарз"), KeyboardButton("➕ Мавжуд мизожга қарз")],
        [KeyboardButton("💰 Тўлов қабул қилиш"), KeyboardButton("❌ Амални бекор қилиш")],
        [KeyboardButton("🔍 Qарзларни излаш")]
    ]
    if role == "admin": kb[2].append(KeyboardButton("👥 Фойдаланувчилар"))
    kb.append([KeyboardButton("📊 Статистика"), KeyboardButton("📢 Гуруҳга Бэкап юбориш")])
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)

def cancel_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Бекор қилиш", callback_data="cancel_action")]])

# ---------- Initialization & Global Callbacks ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = await asyncio.to_thread(get_user, user.id)
    if not db_user:
        if not await asyncio.to_thread(get_all_users):
            await asyncio.to_thread(create_user, user.id, user.username or "", user.first_name or "", "admin")
            await update.message.reply_text("✅ АДМИН этиб тайинландингиз.", reply_markup=get_main_reply_keyboard("admin"))
        else:
            await update.message.reply_text("❌ Кириш тақиқланган.")
        return
    await update.message.reply_text("Тизим тайёр:", reply_markup=get_main_reply_keyboard(db_user['role']))

async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    context.user_data.clear()
    await update.callback_query.edit_message_text("🚫 Амал бекор қилинди.")
    return ConversationHandler.END

# ---------- FLOW 1: NEW DEBT ----------
async def add_debt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👤 **Янги мижоз исмини киритинг:**", parse_mode="Markdown", reply_markup=cancel_inline_keyboard())
    return ADD_NAME

async def add_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['debt_name'] = update.message.text.strip()
    await update.message.reply_text("💰 **Қарз суммаси:**", parse_mode="Markdown", reply_markup=cancel_inline_keyboard())
    return ADD_AMOUNT

async def add_amount_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['debt_amount'] = float(update.message.text.strip())
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Изоҳсиз сақлаш", callback_data="skip_notes")], 
                                   [InlineKeyboardButton("❌ Бекор қилиш", callback_data="cancel_action")]])
        await update.message.reply_text("📝 **Изоҳ ёзинг (Нима олди?):**", parse_mode="Markdown", reply_markup=kb)
        return ADD_NOTES
    except ValueError:
        await update.message.reply_text("❌ Фақат сон киритинг:", reply_markup=cancel_inline_keyboard())
        return ADD_AMOUNT

async def process_new_debt_save(context: ContextTypes.DEFAULT_TYPE, update: Update, note: str):
    name, amount = context.user_data['debt_name'], context.user_data['debt_amount']
    seller = get_seller_identifier(update.effective_user)
    
    await asyncio.to_thread(add_new_customer_and_debt, name, amount, note, seller)
    await notify_group(context, "ЯНГИ МИЖОЗ ВА ҚАРЗ", name, amount, seller, note, amount)
    
    text = f"✅ **Сақланди!**\nМижоз: {name}\nСумма: {amount:,.2f} сўм"
    if update.callback_query: await update.callback_query.edit_message_text(text, parse_mode="Markdown")
    else: await update.message.reply_text(text, parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END

async def add_notes_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_new_debt_save(context, update, update.message.text.strip())

async def add_notes_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    if update.callback_query.data == "skip_notes": 
        return await process_new_debt_save(context, update, "")

# ---------- FLOW 2: ADD TO EXISTING DEBT ----------
async def exist_debt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 **Кимга қарз қўшамиз? (Исм ёзинг):**", parse_mode="Markdown", reply_markup=cancel_inline_keyboard())
    return EXIST_SEARCH

async def search_and_select(update: Update, context: ContextTypes.DEFAULT_TYPE, next_state, action_type):
    results = await asyncio.to_thread(search_customers, update.message.text.strip())
    if not results:
        await update.message.reply_text("❌ Топилмади. Бошқа исм ёзиб кўринг:", reply_markup=cancel_inline_keyboard())
        return next_state - 1 
        
    buttons = [[InlineKeyboardButton(f"{r['name']} | {r['balance']:,.0f} сўм", callback_data=f"{action_type}_{r['id']}")] for r in results[:8]]
    buttons.append([InlineKeyboardButton("❌ Бекор қилиш", callback_data="cancel_action")])
    await update.message.reply_text("👇 **Танланг:**", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
    return next_state

async def exist_search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    return await search_and_select(update, context, EXIST_AMOUNT, "exist")

async def select_debt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, cust_id = query.data.split("_")
    context.user_data['selected_cust_id'] = int(cust_id)
    cust = await asyncio.to_thread(get_customer, int(cust_id))
    
    if action == "exist":
        msg = f"👤 **{cust['name']}**\n📊 Умумий қарз: {cust['balance']:,.2f} сўм\n\n💰 **Қўшиладиган сумма:**"
        kb = cancel_inline_keyboard()
        state = EXIST_AMOUNT
    else:
        msg = f"👤 **{cust['name']}**\n💸 Умумий қарз: {cust['balance']:,.2f} сўм\n\n💵 **Тўлов суммаси:**"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"💰 Тўлиқ ёпиш ({cust['balance']:,.0f} сўм)", callback_data=f"payfull_{cust['balance']}_{cust_id}")],
            [InlineKeyboardButton("❌ Бекор қилиш", callback_data="cancel_action")]
        ])
        state = PAY_AMOUNT
        
    await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=kb)
    return state

async def exist_amount_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['add_amount'] = float(update.message.text.strip())
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("➡️ Изоҳсиз сақлаш", callback_data="skip_exist_notes")], 
                                   [InlineKeyboardButton("❌ Бекор қилиш", callback_data="cancel_action")]])
        await update.message.reply_text("📝 **Изоҳ ёзинг (Бу сафар нима олди?):**", parse_mode="Markdown", reply_markup=kb)
        return EXIST_NOTES
    except ValueError: 
        await update.message.reply_text("❌ Илтимос, тўғри сон киритинг:", reply_markup=cancel_inline_keyboard())
        return EXIST_AMOUNT

async def process_exist_debt_save(context: ContextTypes.DEFAULT_TYPE, update: Update, note: str):
    cust_id = context.user_data['selected_cust_id']
    amount = context.user_data['add_amount']
    seller = get_seller_identifier(update.effective_user)
    
    cust = await asyncio.to_thread(get_customer, cust_id)
    new_bal = await asyncio.to_thread(process_ledger_transaction, cust_id, 'debt', amount, note, seller)
    await notify_group(context, "ҚАРЗ ҚЎШИЛДИ", cust['name'], amount, seller, note, new_bal)
    
    text = f"✅ **Қўшилди!**\n📊 Янги умумий қолдиқ: {new_bal:,.2f} сўм"
    if update.callback_query: await update.callback_query.edit_message_text(text, parse_mode="Markdown")
    else: await update.message.reply_text(text, parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END

async def exist_notes_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_exist_debt_save(context, update, update.message.text.strip())

async def exist_notes_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    if update.callback_query.data == "skip_exist_notes": 
        return await process_exist_debt_save(context, update, "")

# ---------- FLOW 3: PAYMENTS ----------
async def pay_debt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 **Тўлов қилаётган мизож исми:**", parse_mode="Markdown", reply_markup=cancel_inline_keyboard())
    return PAY_SEARCH

async def pay_search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    return await search_and_select(update, context, PAY_AMOUNT, "pay")

async def process_payment(context: ContextTypes.DEFAULT_TYPE, update: Update, amount: float, cust_id: int):
    cust = await asyncio.to_thread(get_customer, cust_id)
    if not cust or amount <= 0 or amount > cust['balance']:
        text = "❌ Хато: Тўлов суммаси қарздан кўп ёки нотўғри!"
        if update.callback_query: await update.callback_query.edit_message_text(text, reply_markup=cancel_inline_keyboard())
        else: await update.message.reply_text(text, reply_markup=cancel_inline_keyboard())
        return PAY_AMOUNT
        
    seller = get_seller_identifier(update.effective_user)
    new_bal = await asyncio.to_thread(process_ledger_transaction, cust_id, 'payment', amount, "Тўлов", seller)
    
    if new_bal <= 0.01:
        text = "🎉 ✅ **Қарз тўлиқ ёпилди!**"
        await notify_group(context, "ҚАРЗ ТЎЛИҚ ЁПИЛДИ", cust['name'], amount, seller, "Тўлов", 0.0)
    else:
        text = f"✅ **Тўлов қабул қилинди!**\n📊 Қолдиқ: {new_bal:,.2f} сўм"
        await notify_group(context, "ТЎЛОВ ҚАБУЛ ҚИЛИНДИ", cust['name'], amount, seller, "Тўлов", new_bal)

    if update.callback_query: await update.callback_query.edit_message_text(text, parse_mode="Markdown")
    else: await update.message.reply_text(text, parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END

async def pay_amount_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: return await process_payment(context, update, float(update.message.text.strip()), context.user_data['selected_cust_id'])
    except ValueError: return PAY_AMOUNT

async def pay_full_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    _, amount, cust_id = update.callback_query.data.split("_")
    return await process_payment(context, update, float(amount), int(cust_id))

# ---------- Search, Stats & Users ----------
async def search_debt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 **Мизож исмини ёзинг:**", parse_mode="Markdown", reply_markup=cancel_inline_keyboard())
    return SEARCH_QUERY

async def search_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    customers = await asyncio.to_thread(search_customers, update.message.text.strip())
    if not customers: 
        await update.message.reply_text("🔍 Топилмади.")
        return ConversationHandler.END
        
    # We take the first match to show the detailed ledger
    cust = customers[0]
    history = await asyncio.to_thread(get_customer_history, cust['id'])
    
    msg = f"👤 **{cust['name']}**\n📊 **Умумий қарз:** {cust['balance']:,.2f} сўм\n\n📅 **Тарих (Охирги амалиётлар):**\n"
    for h in history:
        date_str = h['created_at'].strftime('%d.%m %H:%M')
        sign = "🔴 +" if h['t_type'] == 'debt' else "🟢 -"
        note_str = f" | 📝 {h['note']}" if h['note'] else ""
        msg += f"{sign}{h['amount']:,.0f} | 💼 {h['seller_identifier']}{note_str} | 🕒 {date_str}\n"
        
    await update.message.reply_text(msg, parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END

async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total, largest = await asyncio.to_thread(get_stats)
    msg = f"📊 **Умумий қарз:** {total:,.2f} сўм\n\n**Топ қарздорлар:**\n" + "".join([f"• {d['name']}: {d['balance']:,.2f}\n" for d in largest])
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("🚫 Бекор қилинди.")
    return ConversationHandler.END

# ---------- Bot Runner ----------
def run_telegram_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    app = Application.builder().token(BOT_TOKEN).request(HTTPXRequest(connect_timeout=30.0)).build()

    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^➕ Янги мизож ва қарз$"), add_debt_start),
            MessageHandler(filters.Regex("^➕ Мавжуд мизожга қарз$"), exist_debt_start),
            MessageHandler(filters.Regex("^💰 Тўлов қабул қилиш$"), pay_debt_start),
            MessageHandler(filters.Regex("^🔍 Qарзларни излаш$"), search_debt_start),
            MessageHandler(filters.Regex("^📊 Статистика$"), stats_handler),
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
            EXIST_NOTES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, exist_notes_text_handler),
                CallbackQueryHandler(exist_notes_callback_handler, pattern="^skip_exist_notes$")
            ],
            PAY_SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, pay_search_handler)],
            PAY_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, pay_amount_text_handler),
                CallbackQueryHandler(pay_full_callback_handler, pattern="^payfull_")
            ],
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
    threading.Thread(target=run_telegram_bot, daemon=True).start()
    flask_app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)))
