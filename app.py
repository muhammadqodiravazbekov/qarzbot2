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
    raise ValueError("Жиддий хато: BOT_TOKEN ёки DATABASE_URL муҳит ўзгарувчилари топилмади!")

# Ўзбекистон вақти (UTC+5)
UZB_TZ = timezone(timedelta(hours=5))
def get_current_time(): 
    return datetime.now(UZB_TZ)

# ---------- Flask Web Server ----------
flask_app = Flask(__name__)
@flask_app.route('/')
@flask_app.route('/health')
def health(): 
    return jsonify({"status": "alive", "message": "Bot is running optimally!"}), 200

# ---------- Database Optimization (Context Manager) ----------
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
    EXIST_SEARCH, EXIST_AMOUNT,
    PAY_SEARCH, PAY_AMOUNT,
    SEARCH_QUERY, USER_ID, USER_ROLE
) = range(10)

# ---------- Helper Functions (Fixing the Search Logic) ----------
def normalize_text(text: str) -> str:
    """Нормализация: Фақат кирилл-лотин ўгирилиши ва кичик ҳарфларга ўтказиш"""
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

async def notify_group(context: ContextTypes.DEFAULT_TYPE, action: str, customer: str, amount: float, seller: str, note: str = "", old_bal: float = None, new_bal: float = None):
    if not BACKUP_GROUP_ID: return
    msg = f"📢 **{action}**\n\n👤 Мижоз: {customer}\n💰 Сумма: {amount:,.2f} сўм\n"
    if old_bal is not None and new_bal is not None: 
        msg += f"📊 Баланс: {old_bal:,.2f} ➡️ {new_bal:,.2f} сўм\n"
    msg += f"📝 Изоҳ: {note or '-'}\n💼 Сотувчи: {seller}\n🕒 Вақт: {get_current_time().strftime('%d.%m.%Y %H:%M')}"
    
    try:
        kwargs = {"chat_id": int(BACKUP_GROUP_ID), "text": msg, "parse_mode": "Markdown"}
        if BACKUP_TOPIC_ID: kwargs["message_thread_id"] = int(BACKUP_TOPIC_ID)
        await context.bot.send_message(**kwargs)
    except Exception as e: 
        logging.error(f"Group notification failed: {e}")

# ---------- Core Database Logic ----------
def init_db():
    with get_db(commit=True) as cursor:
        cursor.execute('''CREATE TABLE IF NOT EXISTS users (
            telegram_id BIGINT PRIMARY KEY, username TEXT, first_name TEXT,
            role TEXT CHECK(role IN ('admin','seller','viewer')) NOT NULL, created_at TIMESTAMP)''')
        cursor.execute('''CREATE TABLE IF NOT EXISTS debts (
            id SERIAL PRIMARY KEY, customer_name TEXT NOT NULL, customer_name_normalized TEXT,
            phone TEXT, amount_owed REAL NOT NULL, remaining_balance REAL NOT NULL, notes TEXT,
            seller_telegram_id BIGINT NOT NULL, created_at TIMESTAMP, updated_at TIMESTAMP,
            FOREIGN KEY (seller_telegram_id) REFERENCES users(telegram_id))''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_debt_name_normalized ON debts(customer_name_normalized)')
        cursor.execute('''CREATE TABLE IF NOT EXISTS payments (
            id SERIAL PRIMARY KEY, debt_id INTEGER NOT NULL, amount_paid REAL NOT NULL,
            payment_date TIMESTAMP, notes TEXT, FOREIGN KEY (debt_id) REFERENCES debts(id) ON DELETE CASCADE)''')

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

def delete_user(telegram_id: int) -> bool:
    with get_db(commit=True) as cursor:
        cursor.execute("DELETE FROM users WHERE telegram_id = %s", (telegram_id,))
        return cursor.rowcount > 0

def get_all_users():
    with get_db() as cursor:
        cursor.execute("SELECT * FROM users ORDER BY created_at")
        return cursor.fetchall()

def get_admins_and_sellers():
    with get_db() as cursor:
        cursor.execute("SELECT * FROM users WHERE role IN ('admin','seller')")
        return cursor.fetchall()

def add_debt(customer_name: str, amount: float, notes: str, seller_telegram_id: int) -> int:
    now = get_current_time()
    with get_db(commit=True) as cursor:
        cursor.execute(
            """INSERT INTO debts (customer_name, customer_name_normalized, phone, amount_owed, remaining_balance, notes, seller_telegram_id, created_at, updated_at) 
               VALUES (%s, %s, '', %s, %s, %s, %s, %s, %s) RETURNING id""",
            (customer_name, normalize_text(customer_name), amount, amount, notes, seller_telegram_id, now, now)
        )
        return cursor.fetchone()['id']

def get_debt(debt_id: int):
    with get_db() as cursor:
        cursor.execute("SELECT * FROM debts WHERE id = %s", (debt_id,))
        return cursor.fetchone()

def update_debt(debt_id: int, amount_owed: float, remaining_balance: float):
    with get_db(commit=True) as cursor:
        cursor.execute("UPDATE debts SET amount_owed = %s, remaining_balance = %s, updated_at = %s WHERE id = %s", 
                       (amount_owed, remaining_balance, get_current_time(), debt_id))

def add_payment(debt_id: int, amount: float, notes: str = "") -> str:
    debt = get_debt(debt_id)
    if not debt or amount <= 0 or amount > debt['remaining_balance']: return "error"
    
    new_balance = debt['remaining_balance'] - amount
    now = get_current_time()
    with get_db(commit=True) as cursor:
        cursor.execute("INSERT INTO payments (debt_id, amount_paid, payment_date, notes) VALUES (%s, %s, %s, %s)", (debt_id, amount, now, notes))
        if new_balance <= 0.01:
            cursor.execute("DELETE FROM debts WHERE id = %s", (debt_id,))
            return "paid_off"
        else:
            cursor.execute("UPDATE debts SET remaining_balance = %s, updated_at = %s WHERE id = %s", (new_balance, now, debt_id))
            return "updated"

def search_debts(query: str):
    norm_query = normalize_text(query)
    with get_db() as cursor:
        # Энг муҳим тузатиш: Ҳам оригинал матндан (ILIKE), ҳам нормал матндан (LIKE) қидиради.
        cursor.execute("""
            SELECT d.*, u.first_name as seller_name FROM debts d 
            JOIN users u ON d.seller_telegram_id = u.telegram_id
            WHERE (d.customer_name ILIKE %s OR d.customer_name_normalized LIKE %s)
            AND d.remaining_balance > 0.01 ORDER BY d.created_at DESC
        """, (f"%{query}%", f"%{norm_query}%"))
        return cursor.fetchall()

def get_all_debts():
    with get_db() as cursor:
        cursor.execute("SELECT * FROM debts WHERE remaining_balance > 0.01 ORDER BY created_at DESC")
        return cursor.fetchall()

def get_stats():
    with get_db() as cursor:
        cursor.execute("SELECT COALESCE(SUM(remaining_balance), 0) as total FROM debts WHERE remaining_balance > 0.01")
        total = cursor.fetchone()['total']
        cursor.execute("""SELECT customer_name, SUM(remaining_balance) as total FROM debts 
                          WHERE remaining_balance > 0.01 GROUP BY customer_name ORDER BY total DESC LIMIT 5""")
        return total, cursor.fetchall()

# ---------- UI & Menus ----------
def get_main_reply_keyboard(role: str) -> ReplyKeyboardMarkup:
    kb = [
        [KeyboardButton("➕ Янги мизож ва қарз"), KeyboardButton("➕ Мавжуд мизожга қарз")],
        [KeyboardButton("💰 Тўлов қабул қилиш"), KeyboardButton("❌ Амални бекор қилиш")],
        [KeyboardButton("🔍 Qарзларни излаш")]
    ]
    if role == "admin": 
        kb[2].append(KeyboardButton("👥 Фойдаланувчилар"))
    kb.append([KeyboardButton("📊 Статистика"), KeyboardButton("📢 Гуруҳга Бэкап юбориш")])
    return ReplyKeyboardMarkup(kb, resize_keyboard=True)

def cancel_inline_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Бекор қилиш", callback_data="cancel_action")]])

# ---------- Initialization & Callbacks ----------
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
    await update.message.reply_text("👤 **Мижоз исми:**", parse_mode="Markdown", reply_markup=cancel_inline_keyboard())
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
        await update.message.reply_text("📝 **Изоҳ:**", parse_mode="Markdown", reply_markup=kb)
        return ADD_NOTES
    except ValueError:
        await update.message.reply_text("❌ Фақат сон киритинг:", reply_markup=cancel_inline_keyboard())
        return ADD_AMOUNT

async def process_new_debt_save(context: ContextTypes.DEFAULT_TYPE, update: Update, note: str):
    name, amount = context.user_data['debt_name'], context.user_data['debt_amount']
    await asyncio.to_thread(add_debt, name, amount, note, update.effective_user.id)
    await notify_group(context, "ЯНГИ ҚАРЗ", name, amount, update.effective_user.first_name, note)
    
    text = f"✅ **Сақланди!**\nМижоз: {name}\nСумма: {amount:,.2f} сўм"
    if update.callback_query: 
        await update.callback_query.edit_message_text(text, parse_mode="Markdown")
    else: 
        await update.message.reply_text(text, parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END

async def add_notes_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return await process_new_debt_save(context, update, update.message.text.strip())

async def add_notes_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    if update.callback_query.data == "skip_notes": 
        return await process_new_debt_save(context, update, "")

# ---------- FLOW 2 & 3: SEARCH, ADD, PAY ----------
async def exist_debt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 **Қарзи кўпайтириладиган мизож исми:**", parse_mode="Markdown", reply_markup=cancel_inline_keyboard())
    return EXIST_SEARCH

async def pay_debt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 **Тўлов қилаётган мизож исми:**", parse_mode="Markdown", reply_markup=cancel_inline_keyboard())
    return PAY_SEARCH

async def search_and_select(update: Update, context: ContextTypes.DEFAULT_TYPE, next_state, action_type):
    results = await asyncio.to_thread(search_debts, update.message.text.strip())
    if not results:
        await update.message.reply_text("❌ Топилмади. Бошқа исм ёзиб кўринг:", reply_markup=cancel_inline_keyboard())
        return next_state - 1 
        
    buttons = [[InlineKeyboardButton(f"{r['customer_name']} | {r['remaining_balance']:,.0f} сўм", callback_data=f"{action_type}_{r['id']}")] for r in results[:8]]
    buttons.append([InlineKeyboardButton("❌ Бекор қилиш", callback_data="cancel_action")])
    await update.message.reply_text("👇 **Танланг:**", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(buttons))
    return next_state

async def exist_search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    return await search_and_select(update, context, EXIST_AMOUNT, "exist")

async def pay_search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE): 
    return await search_and_select(update, context, PAY_AMOUNT, "pay")

async def select_debt_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action, debt_id = query.data.split("_")
    context.user_data['selected_debt_id'] = int(debt_id)
    debt = await asyncio.to_thread(get_debt, int(debt_id))
    
    if action == "exist":
        msg = f"👤 **{debt['customer_name']}**\n📊 Қарз: {debt['remaining_balance']:,.2f} сўм\n\n💰 **Қўшиладиган сумма:**"
        kb = cancel_inline_keyboard()
        state = EXIST_AMOUNT
    else:
        msg = f"👤 **{debt['customer_name']}**\n💸 Қарз: {debt['remaining_balance']:,.2f} сўм\n\n💵 **Тўлов суммаси:**"
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton(f"💰 Тўлиқ ёпиш ({debt['remaining_balance']:,.0f} сўм)", callback_data=f"payfull_{debt['remaining_balance']}_{debt_id}")],
            [InlineKeyboardButton("❌ Бекор қилиш", callback_data="cancel_action")]
        ])
        state = PAY_AMOUNT
        
    await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=kb)
    return state

async def exist_amount_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount, debt_id = float(update.message.text.strip()), context.user_data['selected_debt_id']
        debt = await asyncio.to_thread(get_debt, debt_id)
        if debt:
            new_bal = debt['remaining_balance'] + amount
            await asyncio.to_thread(update_debt, debt_id, debt['amount_owed'] + amount, new_bal)
            await notify_group(context, "ҚАРЗ ОШИРИЛДИ", debt['customer_name'], amount, update.effective_user.first_name, "", debt['remaining_balance'], new_bal)
            await update.message.reply_text(f"✅ **Қўшилди!** Янги қолдиқ: {new_bal:,.2f} сўм", parse_mode="Markdown")
    except ValueError: 
        return EXIST_AMOUNT
    context.user_data.clear()
    return ConversationHandler.END

async def process_payment(context: ContextTypes.DEFAULT_TYPE, update: Update, amount: float, debt_id: int):
    debt = await asyncio.to_thread(get_debt, debt_id)
    if not debt: return
    old_bal = debt['remaining_balance']
    status = await asyncio.to_thread(add_payment, debt_id, amount)
    
    if status == "paid_off":
        text = "🎉 ✅ **Қарз тўлиқ ёпилди!**"
        await notify_group(context, "ҚАРЗ ЁПИЛДИ", debt['customer_name'], amount, update.effective_user.first_name, "", old_bal, 0.0)
    elif status == "updated":
        text = f"✅ **Тўлов қабул қилинди!** Қолдиқ: {old_bal - amount:,.2f} сўм"
        await notify_group(context, "ТЎЛОВ", debt['customer_name'], amount, update.effective_user.first_name, "", old_bal, old_bal - amount)
    else:
        text = "❌ Тўлов суммаси қарздан кўп!"
        if update.callback_query: 
            await update.callback_query.edit_message_text(text, reply_markup=cancel_inline_keyboard())
        else: 
            await update.message.reply_text(text, reply_markup=cancel_inline_keyboard())
        return PAY_AMOUNT

    if update.callback_query: 
        await update.callback_query.edit_message_text(text, parse_mode="Markdown")
    else: 
        await update.message.reply_text(text, parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END

async def pay_amount_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try: 
        return await process_payment(context, update, float(update.message.text.strip()), context.user_data['selected_debt_id'])
    except ValueError: 
        return PAY_AMOUNT

async def pay_full_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    _, amount, debt_id = update.callback_query.data.split("_")
    return await process_payment(context, update, float(amount), int(debt_id))

# ---------- Search, Stats, Backup & Users ----------
async def search_debt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 **Мизож исми:**", parse_mode="Markdown", reply_markup=cancel_inline_keyboard())
    return SEARCH_QUERY

async def search_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    debts = await asyncio.to_thread(search_debts, update.message.text.strip())
    if not debts: 
        await update.message.reply_text("🔍 Топилмади.")
    else:
        msg = "".join([f"👤 **{d['customer_name']}** | {d['remaining_balance']:,.2f} сўм\n📝 Изоҳ: {d['notes'] or '-'}\n\n" for d in debts[:15]])
        await update.message.reply_text(msg, parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END

async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total, largest = await asyncio.to_thread(get_stats)
    msg = f"📊 **Умумий қарз:** {total:,.2f} сўм\n\n**Топ қарздорлар:**\n" + "".join([f"• {d['customer_name']}: {d['total']:,.2f}\n" for d in largest])
    await update.message.reply_text(msg, parse_mode="Markdown")

async def send_backup_to_group_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    debts = await asyncio.to_thread(get_all_debts)
    if not debts or not BACKUP_GROUP_ID: 
        return await update.message.reply_text("Хато: База бўш ёки гуруҳ йўқ.")
    
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Name", "Notes", "Current Debt Status", "Date Added"])
    for d in debts: 
        writer.writerow([d['id'], d['customer_name'], d['notes'] or "-", f"{d['remaining_balance']:.2f}", d['created_at'].strftime('%d.%m.%Y')])
    output.seek(0)
    
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

# Users Management Flow
async def users_management_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db_user = await asyncio.to_thread(get_user, update.effective_user.id)
    if not db_user or db_user['role'] != 'admin':
        await update.message.reply_text("⛔ Бу бўлим фақат Админлар учун.")
        return ConversationHandler.END
        
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Фойдаланувчи қўшиш", callback_data="add_user")],
        [InlineKeyboardButton("❌ Фойдаланувчини ўчириш", callback_data="rem_user")],
        [InlineKeyboardButton("📋 Рўйхатни кўриш", callback_data="list_users")],
        [InlineKeyboardButton("❌ Бекор қилиш", callback_data="cancel_action")]
    ])
    await update.message.reply_text("👥 **Фойдаланувчиларни бошқариш:**", parse_mode="Markdown", reply_markup=kb)
    return USER_ID

async def user_management_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    action = query.data
    
    if action == "list_users":
        users = await asyncio.to_thread(get_all_users)
        msg = "👥 **Рўйхатдан ўтганлар:**\n\n"
        for u in users: 
            msg += f"• {u['first_name']} | ID: `{u['telegram_id']}` | Роль: **{u['role'].upper()}**\n"
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=cancel_inline_keyboard())
        return USER_ID
    elif action == "add_user":
        context.user_data['user_action'] = 'add'
        await query.edit_message_text("➕ Янги фойдаланувчининг **Telegram ID рақамини** ёзинг:", parse_mode="Markdown", reply_markup=cancel_inline_keyboard())
        return USER_ID
    elif action == "rem_user":
        context.user_data['user_action'] = 'rem'
        await query.edit_message_text("❌ Ўчириладиган фойдаланувчининг **Telegram ID рақамини** ёзинг:", parse_mode="Markdown", reply_markup=cancel_inline_keyboard())
        return USER_ID
    return USER_ID

async def user_id_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        tg_id = int(update.message.text.strip())
        action = context.user_data.get('user_action')
        
        if action == 'rem':
            if await asyncio.to_thread(delete_user, tg_id): 
                await update.message.reply_text("✅ Фойдаланувчи ўчирилди.")
            else: 
                await update.message.reply_text("❌ Топилмади.")
            context.user_data.clear()
            return ConversationHandler.END
            
        elif action == 'add':
            context.user_data['target_tg_id'] = tg_id
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("Сотувчи (Seller)", callback_data="role_seller")],
                [InlineKeyboardButton("Админ (Admin)", callback_data="role_admin")]
            ])
            await update.message.reply_text("Фойдаланувчи ролини танланг:", reply_markup=kb)
            return USER_ROLE
    except ValueError:
        await update.message.reply_text("❌ Илтимос, рақамли ID киритинг:", reply_markup=cancel_inline_keyboard())
        return USER_ID

async def user_role_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    role = query.data.split("_")[1]
    tg_id = context.user_data['target_tg_id']
    
    try: 
        chat = await context.bot.get_chat(tg_id)
        first_name = chat.first_name or "Foydalanuvchi"
    except: 
        first_name = "Foydalanuvchi"
    
    if await asyncio.to_thread(create_user, tg_id, "", first_name, role):
        await query.edit_message_text(f"✅ **{first_name}** тизимга **{role.upper()}** роли билан қўшилди.", parse_mode="Markdown")
    else:
        await query.edit_message_text("❌ Бу фойдаланувчи аллақачон мавжуд ёки хатолик юз берди.")
    context.user_data.clear()
    return ConversationHandler.END

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
            MessageHandler(filters.Regex("^📢 Гуруҳга Бэкап юбориш$"), send_backup_to_group_handler),
            MessageHandler(filters.Regex("^👥 Фойдаланувчилар$"), users_management_start),
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
            PAY_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, pay_amount_text_handler),
                CallbackQueryHandler(pay_full_callback_handler, pattern="^payfull_")
            ],
            SEARCH_QUERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, search_query_handler)],
            USER_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, user_id_input_handler),
                CallbackQueryHandler(user_management_callback, pattern="^(list_users|add_user|rem_user)$")
            ],
            USER_ROLE: [CallbackQueryHandler(user_role_callback, pattern="^role_")]
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
