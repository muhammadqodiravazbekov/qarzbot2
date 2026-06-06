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
from datetime import datetime
from typing import List, Dict, Optional
from flask import Flask, jsonify
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ConversationHandler,
    MessageHandler, CallbackQueryHandler, filters, ContextTypes
)
from telegram.request import HTTPXRequest

# ---------- Flask Веб Сервер (Render узилиб қолмаслиги учун) ----------
flask_app = Flask(__name__)

@flask_app.route('/')
@flask_app.route('/health')
def health():
    return jsonify({"status": "alive", "message": "Бот муваффақиятли ишлаяпти!"}), 200

# ---------- Конфигурация ва Муҳит Ўзгарувчилари ----------
BOT_TOKEN = os.environ.get('BOT_TOKEN')
DATABASE_URL = os.environ.get('DATABASE_URL')
BACKUP_GROUP_ID = os.environ.get('BACKUP_GROUP_ID')
BACKUP_TOPIC_ID = os.environ.get('BACKUP_TOPIC_ID')

if not BOT_TOKEN or not DATABASE_URL:
    raise ValueError("Жиддий хато: BOT_TOKEN ёки DATABASE_URL муҳит ўзгарувчилари топилмади!")

# ---------- Тезкор уланиш учун Connection Pool ----------
db_pool = ThreadedConnectionPool(1, 20, dsn=DATABASE_URL)

def get_db_connection():
    return db_pool.getconn()

def release_db_connection(conn):
    db_pool.putconn(conn)

# ---------- Суҳбат Ҳолатлари (Conversation States) ----------
(
    ADD_NAME, ADD_PHONE, ADD_AMOUNT, ADD_NOTES,
    EXIST_SEARCH, EXIST_SELECT, EXIST_AMOUNT,
    PAY_DEBT_ID, PAY_AMOUNT,
    SEARCH_QUERY
) = range(10)

# ---------- Матнни Нормализация қилиш (Қидирув осон бўлиши учун) ----------
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

# ---------- Тезкор хабар бэкапини гуруҳга юбориш функцияси ----------
async def send_backup_message(context: ContextTypes.DEFAULT_TYPE, message: str):
    if BACKUP_GROUP_ID:
        try:
            kwargs = {"chat_id": int(BACKUP_GROUP_ID), "text": message, "parse_mode": "Markdown"}
            if BACKUP_TOPIC_ID:
                kwargs["message_thread_id"] = int(BACKUP_TOPIC_ID)
            await context.bot.send_message(**kwargs)
        except Exception as e:
            logging.error(f"Гуруҳга хабар юборишда хатолик: {e}")

# ---------- Маълумотлар Базаси билан Ишлаш ----------
def init_db():
    conn = get_db_connection()
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
            cursor.execute("INSERT INTO users (telegram_id, username, first_name, role) VALUES (%s, %s, %s, %s)",
                           (telegram_id, username, first_name, role))
            conn.commit()
            return True
    except psycopg2.IntegrityError:
        return False
    finally:
        release_db_connection(conn)

def get_admins_and_sellers() -> List[Dict]:
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT telegram_id, username, first_name, role FROM users WHERE role IN ('admin','seller')")
            rows = cursor.fetchall()
            return [{"telegram_id": r[0], "username": r[1], "first_name": r[2], "role": r[3]} for r in rows]
    finally:
        release_db_connection(conn)

def add_debt(customer_name: str, phone: str, amount: float, notes: str, seller_telegram_id: int) -> int:
    norm_name = normalize_text(customer_name)
    conn = get_db_connection()
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
        release_db_connection(conn)

def get_debt(debt_id: int) -> Optional[Dict]:
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT id, customer_name, phone, amount_owed, remaining_balance, notes, seller_telegram_id FROM debts WHERE id = %s", (debt_id,))
            row = cursor.fetchone()
            return {"id": row[0], "customer_name": row[1], "phone": row[2], "amount_owed": row[3], "remaining_balance": row[4], "notes": row[5], "seller_telegram_id": row[6]} if row else None
    finally:
        release_db_connection(conn)

def update_debt(debt_id: int, **kwargs) -> bool:
    allowed_fields = {"customer_name", "phone", "amount_owed", "remaining_balance", "notes"}
    updates = {k: v for k, v in kwargs.items() if k in allowed_fields}
    if not updates: return False
    if "customer_name" in updates:
        updates["customer_name_normalized"] = normalize_text(updates["customer_name"])
    updates["updated_at"] = datetime.now()
    
    set_clause = ", ".join([f"{key} = %s" for key in updates.keys()])
    values = list(updates.values()) + [debt_id]
    
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"UPDATE debts SET {set_clause} WHERE id = %s", values)
            conn.commit()
            return cursor.rowcount > 0
    finally:
        release_db_connection(conn)

def add_payment(debt_id: int, amount: float, notes: str = "") -> bool:
    debt = get_debt(debt_id)
    if not debt or amount <= 0 or amount > debt["remaining_balance"]:
        return False
    new_balance = debt["remaining_balance"] - amount
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("INSERT INTO payments (debt_id, amount_paid, notes) VALUES (%s, %s, %s)", (debt_id, amount, notes))
            cursor.execute("UPDATE debts SET remaining_balance = %s, updated_at = %s WHERE id = %s", (new_balance, datetime.now(), debt_id))
            conn.commit()
            return True
    finally:
        release_db_connection(conn)

def search_debts(query: str) -> List[Dict]:
    norm_query = normalize_text(query)
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT d.id, d.customer_name, d.phone, d.amount_owed, d.remaining_balance, d.notes, d.seller_telegram_id, u.username, u.first_name
                FROM debts d JOIN users u ON d.seller_telegram_id = u.telegram_id
                WHERE d.phone LIKE %s OR d.customer_name_normalized LIKE %s ORDER BY d.created_at DESC
            """, (f"%{query}%", f"%{norm_query}%"))
            rows = cursor.fetchall()
            return [{"id": r[0], "customer_name": r[1], "phone": r[2], "amount_owed": r[3], "remaining_balance": r[4], "notes": r[5], "seller_telegram_id": r[6], "seller_name": r[7] or r[8] or str(r[6])} for r in rows]
    finally:
        release_db_connection(conn)

def get_all_debts() -> List[Dict]:
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT d.id, d.customer_name, d.phone, d.amount_owed, d.remaining_balance, d.notes, d.seller_telegram_id, d.created_at, u.username, u.first_name
                FROM debts d JOIN users u ON d.seller_telegram_id = u.telegram_id ORDER BY d.created_at DESC
            """)
            rows = cursor.fetchall()
            return [{"id": r[0], "customer_name": r[1], "phone": r[2], "amount_owed": r[3], "remaining_balance": r[4], "notes": r[5], "seller_telegram_id": r[6], "created_at": r[7], "seller_name": r[8] or r[9] or str(r[6])} for r in rows]
    finally:
        release_db_connection(conn)

def get_total_outstanding() -> float:
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("SELECT COALESCE(SUM(remaining_balance), 0) FROM debts")
            return cursor.fetchone()[0]
    finally:
        release_db_connection(conn)

def get_outstanding_by_seller() -> List[Dict]:
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT u.telegram_id, u.username, u.first_name, COALESCE(SUM(d.remaining_balance), 0)
                FROM users u LEFT JOIN debts d ON u.telegram_id = d.seller_telegram_id
                WHERE u.role IN ('admin','seller') GROUP BY u.telegram_id, u.username, u.first_name ORDER BY SUM(d.remaining_balance) DESC
            """)
            rows = cursor.fetchall()
            return [{"seller_id": r[0], "name": r[1] or r[2] or str(r[0]), "total": r[3]} for r in rows]
    finally:
        release_db_connection(conn)

def get_largest_debtors(limit: int = 5) -> List[Dict]:
    conn = get_db_connection()
    try:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT customer_name, phone, SUM(remaining_balance) FROM debts
                GROUP BY customer_name, phone ORDER BY SUM(remaining_balance) DESC LIMIT %s
            """, (limit,))
            rows = cursor.fetchall()
            return [{"name": r[0], "phone": r[1], "total": r[2]} for r in rows]
    finally:
        release_db_connection(conn)

# ---------- Пастки Доимий Меню (ReplyKeyboardMarkup - "Тўртта Тўртбурчак") ----------
def get_main_reply_keyboard(role: str) -> ReplyKeyboardMarkup:
    keyboard = []
    
    # Фақат админ ва сотувчилар кўрадиган тугмалар
    if role in ("admin", "seller"):
        keyboard.append([KeyboardButton("➕ Янги мизож ва қарз"), KeyboardButton("➕ Мавжуд мизожга қарз")])
        keyboard.append([KeyboardButton("💰 Тўлов қабул қилиш"), KeyboardButton("❌ Амални бекор қилиш")])
    
    # Ҳамма кўра оладиган тугмалар
    keyboard.append([KeyboardButton("🔍 Qарзларни излаш"), KeyboardButton("📋 Барча қарзлар рўйхати")])
    
    # CSV экспорт ўрнига тўғридан-тўғри гуруҳга бэкап юбориш тугмаси қўшилди
    keyboard.append([KeyboardButton("📊 Статистика"), KeyboardButton("📢 Гуруҳга Бэкап юбориш")])
    
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

# ---------- Бот Старт Функцияси ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = await asyncio.to_thread(get_user, user.id)
    
    if not db_user:
        admins_sellers = await asyncio.to_thread(get_admins_and_sellers)
        if not admins_sellers:
            # Агар база бўм-бўш бўлса, биринчи старт берган одам автоматик АДМИН бўлади
            await asyncio.to_thread(create_user, user.id, user.username or "", user.first_name or "", "admin")
            await update.message.reply_text(f"✅ {user.first_name}, сиз тизимга АДМИН этиб тайинландингиз.", reply_markup=get_main_reply_keyboard("admin"))
        else:
            await update.message.reply_text("❌ Кириш тақиқланган. Тизимдан фойдаланиш учун Админ рухсати зарур.")
        return
        
    await update.message.reply_text("Тизим тайёр. Пастки менюдан фойдаланишингиз мумкин:", reply_markup=get_main_reply_keyboard(db_user['role']))

# ---------- Пастки Меню Амаллари Бошланиши (Entry Points) ----------
async def add_debt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("👤 Мизож исми ва фамилиясини киритинг:")
    return ADD_NAME

async def exist_debt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Қарзи кўпайтириладиган мизож исми ёки телефонини киритинг:")
    return EXIST_SEARCH

async def pay_debt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("💰 Тўлов қабул қилиш учун қарздорликнинг **ID рақамини** киритинг:")
    return PAY_DEBT_ID

async def search_debt_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Қидирилаётган мизож исми ёки телефонини ёзинг:")
    return SEARCH_QUERY

# ---------- Тезкор Тугмалар Ишловчилари (Non-blocking Handlers) ----------
async def list_debts_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    debts = await asyncio.to_thread(get_all_debts)
    if not debts:
        await update.message.reply_text("📋 Қарздорлар топилмади.")
    else:
        msg = "📋 **Қарздорлар рўйхати (Охирги 15 та):**\n\n"
        for d in debts[:15]:
            msg += f"🆔 `ID: {d['id']}` | {d['customer_name']} | 💰 Қолдиқ: **{d['remaining_balance']:.2f}**\n"
        await update.message.reply_text(msg, parse_mode="Markdown")

async def stats_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    total = await asyncio.to_thread(get_total_outstanding)
    by_seller = await asyncio.to_thread(get_outstanding_by_seller)
    largest = await asyncio.to_thread(get_largest_debtors, 5)
    
    msg = f"📊 **Умумий Статистика**\n\n💸 Умумий жамланган қарз: **{total:.2f}**\n\n"
    msg += "**Сотувчилар кесимида:**\n"
    for s in by_seller: msg += f"• {s['name']}: {s['total']:.2f}\n"
    msg += "\n**Энг йирик қарздорлар:**\n"
    for d in largest: msg += f"• {d['name']}: **{d['total']:.2f}**\n"
    
    await update.message.reply_text(msg, parse_mode="Markdown")

# 📢 Тўғридан-тўғри Гуруҳга файл юборувчи функция (Сиз сўраган янгиланиш)
async def send_backup_to_group_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    debts = await asyncio.to_thread(get_all_debts)
    if not debts:
        await update.message.reply_text("Базада маълумот мавжуд эмас.")
        return
        
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["ID", "Name", "Phone", "Balance"])
    for d in debts: 
        writer.writerow([d["id"], d["customer_name"], d["phone"], d["remaining_balance"]])
    output.seek(0)
    
    if BACKUP_GROUP_ID:
        try:
            kwargs = {
                "chat_id": int(BACKUP_GROUP_ID),
                "document": io.BytesIO(output.getvalue().encode()),
                "filename": f"qarzlar_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                "caption": f"📢 **БАЗА ТЎЛИҚ БЭКАПИ**\n👤 Масъул: {update.effective_user.first_name}\n📅 Сана: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                "parse_mode": "Markdown"
            }
            if BACKUP_TOPIC_ID:
                kwargs["message_thread_id"] = int(BACKUP_TOPIC_ID)
                
            await context.bot.send_document(**kwargs)
            await update.message.reply_text("📢 Тўлиқ CSV бэкап файли белгиланган масъул гуруҳга муваффақиятли юборилди!")
        except Exception as e:
            logging.error(f"Гуруҳга бэкап юборишда хатолик: {e}")
            await update.message.reply_text("❌ Гуруҳга бэкап юборишда хатолик юз берди.")
    else:
        await update.message.reply_text("❌ Бэкап гуруҳ ID си (BACKUP_GROUP_ID) муҳит ўзгарувчиларига созланмаган.")

# ---------- Суҳбат Қадамлари Ишловчилари ----------
async def add_name_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['debt_name'] = update.message.text.strip()
    await update.message.reply_text("📞 Телефон рақамини киритинг (ўтказиб юбориш учун /skip):")
    return ADD_PHONE

async def add_phone_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    context.user_data['debt_phone'] = "" if text == "/skip" else text
    await update.message.reply_text("💰 Қарз суммасини киритинг:")
    return ADD_AMOUNT

async def add_amount_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data['debt_amount'] = float(update.message.text.strip())
        await update.message.reply_text("📝 Қўшимча изоҳ ёзинг (ёки /skip):")
        return ADD_NOTES
    except ValueError:
        await update.message.reply_text("❌ Илтимос фақат сон киритинг:")
        return ADD_AMOUNT

async def add_notes_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    notes = "" if text == "/skip" else text
    
    debt_id = await asyncio.to_thread(
        add_debt, context.user_data['debt_name'], context.user_data['debt_phone'],
        context.user_data['debt_amount'], notes, update.effective_user.id
    )
    await update.message.reply_text(f"✅ Муваффақиятли базага ёзилди! ID: `{debt_id}`", parse_mode="Markdown")
    await send_backup_message(context, f"➕ **ЯНГИ ҚАРЗ**\nМизож: {context.user_data['debt_name']}\nСумма: {context.user_data['debt_amount']:.2f}")
    context.user_data.clear()
    return ConversationHandler.END

async def exist_search_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    results = await asyncio.to_thread(search_debts, text)
    if not map or not results:
        await update.message.reply_text("❌ Бундай мизож топилмади. Қайта қидириб кўринг:")
        return EXIST_SEARCH
        
    buttons = [[InlineKeyboardButton(f"{r['customer_name']} (🆔 {r['id']})", callback_data=f"sel_{r['id']}")] for r in results[:8]]
    await update.message.reply_text("👇 Рўйхатдан кераклисини танланг:", reply_markup=InlineKeyboardMarkup(buttons))
    return EXIST_SELECT

async def exist_select_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    debt_id = int(query.data.split("_")[1])
    context.user_data['selected_debt_id'] = debt_id
    debt = await asyncio.to_thread(get_debt, debt_id)
    await query.edit_message_text(f"👤 Мизож: *{debt['customer_name']}*\n\nҚўшиладиган янги суммани киритинг:", parse_mode="Markdown")
    return EXIST_AMOUNT

async def exist_amount_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip())
        debt_id = context.user_data['selected_debt_id']
        debt = await asyncio.to_thread(get_debt, debt_id)
        if debt:
            new_owed = debt['amount_owed'] + amount
            new_balance = debt['remaining_balance'] + amount
            await asyncio.to_thread(update_debt, debt_id, amount_owed=new_owed, remaining_balance=new_balance)
            await update.message.reply_text(f"✅ Қарз суммаси оширилди. Янги қолдиқ: **{new_balance:.2f}**", parse_mode="Markdown")
            await send_backup_message(context, f"🔄 **ҚАРЗ ОШИРИЛДИ**\nМизож: {debt['customer_name']}\nҚўшилди: {amount:.2f}\nЖорий қолдиқ: {new_balance:.2f}")
    except ValueError:
        await update.message.reply_text("Илтимос, тўғри сон киритинг:")
        return EXIST_AMOUNT
    context.user_data.clear()
    return ConversationHandler.END

async def pay_debt_id_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        debt_id = int(update.message.text.strip())
        debt = await asyncio.to_thread(get_debt, debt_id)
        if not debt:
            await update.message.reply_text("❌ Бундай ID билан ҳеч қандай қарздорлик топилмади. Қайта киритинг:")
            return PAY_DEBT_ID
        context.user_data['pay_debt_id'] = debt_id
        await update.message.reply_text(f"👤 Мизож: {debt['customer_name']}\n💸 Жорий қарз қолдиғи: **{debt['remaining_balance']:.2f}**\n\nОлинган тўлов суммасини киритинг:")
        return PAY_AMOUNT
    except ValueError:
        await update.message.reply_text("Хато: ID фақат рақамдан иборат бўлади:")
        return PAY_DEBT_ID

async def pay_amount_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        amount = float(update.message.text.strip())
        debt_id = context.user_data['pay_debt_id']
        if await asyncio.to_thread(add_payment, debt_id, amount):
            debt = await asyncio.to_thread(get_debt, debt_id)
            await update.message.reply_text(f"✅ Тўлов қабул қилинди. Янги қолдиқ: **{debt['remaining_balance']:.2f}**", parse_mode="Markdown")
            await send_backup_message(context, f"💰 **ТЎЛОВ ОЛИНДИ**\nМизож: {debt['customer_name']}\nТўлов суммаси: {amount:.2f}\nҚолдиқ қарз: {debt['remaining_balance']:.2f}")
        else:
            await update.message.reply_text("❌ Хато: Тўлов суммаси жорий умумий қарздан катта бўлиши мумкин эмас.")
    except ValueError:
        await update.message.reply_text("Илтимос, тўғри сумма киритинг:")
        return PAY_AMOUNT
    context.user_data.clear()
    return ConversationHandler.END

async def search_query_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    debts = await asyncio.to_thread(search_debts, text)
    if not debts:
        await update.message.reply_text("🔍 Мос келувчи мизож топилмади.")
    else:
        msg = "🔍 **Қидирув натижалари:**\n\n"
        for d in debts[:15]:
            msg += f"🆔 `ID: {d['id']}` | {d['customer_name']} | 📞 {d['phone'] or '-'} | 💰 Қолдиқ: **{d['remaining_balance']:.2f}**\n"
        await update.message.reply_text(msg, parse_mode="Markdown")
    context.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    user_id = update.effective_user.id
    db_user = await asyncio.to_thread(get_user, user_id)
    role = db_user['role'] if db_user else "viewer"
    await update.message.reply_text("🚫 Жорий амал бекор қилинди.", reply_markup=get_main_reply_keyboard(role))
    return ConversationHandler.END

# ---------- Ботни Алоҳида Оқимда Юрғизиш ----------
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
            ADD_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_phone_handler)],
            ADD_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_amount_handler)],
            ADD_NOTES: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_notes_handler)],
            EXIST_SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, exist_search_handler)],
            EXIST_SELECT: [CallbackQueryHandler(exist_select_callback, pattern="^sel_")],
            EXIST_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, exist_amount_handler)],
            PAY_DEBT_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, pay_debt_id_handler)],
            PAY_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, pay_amount_handler)],
            SEARCH_QUERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, search_query_handler)],
        },
        fallbacks=[
            CommandHandler("cancel", cancel), 
            MessageHandler(filters.Regex("^❌ Амални бекор қилиш$"), cancel)
        ]
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("start", start))
    
    app.run_polling(stop_signals=None)

# ---------- Дастурни Ишга Тушириш Нуқтаси ----------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
    
    # Таблицаларни автоматик текшириш ва яратиш
    init_db()
    
    # Ботни орқа фонда ишга тушириш
    bot_thread = threading.Thread(target=run_telegram_bot)
    bot_thread.daemon = True
    bot_thread.start()
    
    # Flask веб серверни асосий оқимда юрғизиш (Render тизими учун мажбурий)
    port = int(os.environ.get('PORT', 5000))
    flask_app.run(host='0.0.0.0', port=port)
