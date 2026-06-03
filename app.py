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
        'ф': 'f', 'х': 'x', 'ц': 'ts', 'ч': 'ch', 'ш': 'sh', 'щ': 'shch',
        'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
        'ғ': 'g', 'қ': 'q', 'ҳ': 'h', 'ў': 'o'
    }
    text = text.strip().lower()
    text = "".join(cyrillic_to_latin.get(char, char) for char in text)
    return "".join(
        c for c in unicodedata.normalize('NFD', text)
        if unicodedata.category(c) != 'Mn'
    )

def format_amount(amount: float) -> str:
    """Format float amount to human-readable currency format string."""
    try:
        return f"{int(amount):,}".replace(",", " ") + " so'm"
    except (ValueError, TypeError):
        return "0 so'm"

# ---------- Database Layer ----------
def get_db_connection():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    # Create users table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            telegram_id BIGINT PRIMARY KEY,
            username VARCHAR(255),
            full_name VARCHAR(255),
            role VARCHAR(50) NOT NULL DEFAULT 'xodim_user'
        );
    """)
    # Create clients table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS clients (
            id SERIAL PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            phone VARCHAR(50),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    # Create debts table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS debts (
            id SERIAL PRIMARY KEY,
            client_id INTEGER REFERENCES clients(id) ON DELETE CASCADE,
            amount NUMERIC(15, 2) NOT NULL,
            remaining_amount NUMERIC(15, 2) NOT NULL,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """)
    # Create transactions table
    cur.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id SERIAL PRIMARY KEY,
            debt_id INTEGER REFERENCES debts(id) ON DELETE CASCADE,
            amount_paid NUMERIC(15, 2) NOT NULL,
            payment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            collected_by BIGINT REFERENCES users(telegram_id)
        );
    """)
    
    # Ensure at least one super_admin exists from env if available
    admin_id = os.environ.get('ADMIN_TELEGRAM_ID')
    if admin_id:
        try:
            cur.execute("""
                INSERT INTO users (telegram_id, username, full_name, role)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (telegram_id) DO UPDATE SET role = 'super_admin';
            """, (int(admin_id), 'admin', 'Asosiy Admin', 'super_admin'))
        except Exception as e:
            logging.error(f"Error seeding primary admin: {e}")

    conn.commit()
    cur.close()
    conn.close()

# Database operations helpers
def get_user(telegram_id: int) -> Optional[Dict]:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT telegram_id, username, full_name, role FROM users WHERE telegram_id = %s;", (telegram_id,))
    row = cur.fetchone()
    cur.close()
    conn.close()
    if row:
        return {"telegram_id": row[0], "username": row[1], "full_name": row[2], "role": row[3]}
    return None

def create_user(telegram_id: int, username: str, full_name: str, role: str = 'xodim_user'):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO users (telegram_id, username, full_name, role)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (telegram_id) DO UPDATE SET username = %s, full_name = %s, role = %s;
    """, (telegram_id, username, full_name, role, username, full_name, role))
    conn.commit()
    cur.close()
    conn.close()

def add_client(name: str, phone: str) -> int:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("INSERT INTO clients (name, phone) VALUES (%s, %s) RETURNING id;", (name, phone))
    client_id = cur.fetchone()[0]
    conn.commit()
    cur.close()
    conn.close()
    return client_id

def add_debt(client_id: int, amount: float, notes: str):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO debts (client_id, amount, remaining_amount, notes)
        VALUES (%s, %s, %s, %s);
    """, (client_id, amount, amount, notes))
    conn.commit()
    cur.close()
    conn.close()

def search_clients_with_debts(query: str) -> List[Dict]:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, name, phone FROM clients;")
    rows = cur.fetchall()
    
    normalized_query = normalize_text(query)
    matched_client_ids = []
    
    for r in rows:
        if normalized_query in normalize_text(r[1]) or (r[2] and normalized_query in normalize_text(r[2])):
            matched_client_ids.append(r[0])
            
    if not matched_client_ids:
        cur.close()
        conn.close()
        return []
        
    cur.execute("""
        SELECT c.id, c.name, c.phone, COALESCE(SUM(d.remaining_amount), 0)
        FROM clients c
        LEFT JOIN debts d ON c.id = d.client_id
        WHERE c.id ANY(%s)
        GROUP BY c.id, c.name, c.phone
        ORDER BY c.name ASC;
    """, (matched_client_ids,))
    
    results = []
    for r in cur.fetchall():
        results.append({"id": r[0], "name": r[1], "phone": r[2], "total_debt": float(r[3])})
        
    cur.close()
    conn.close()
    return results

def get_client_debts_details(client_id: int) -> List[Dict]:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, amount, remaining_amount, notes, created_at
        FROM debts
        WHERE client_id = %s AND remaining_amount > 0
        ORDER BY created_at ASC;
    """, (client_id,))
    debts = []
    for r in cur.fetchall():
        debts.append({
            "id": r[0],
            "amount": float(r[1]),
            "remaining_amount": float(r[2]),
            "notes": r[3],
            "created_at": r[4]
        })
    cur.close()
    conn.close()
    return debts

def register_payment(debt_id: int, pay_amount: float, user_id: int) -> bool:
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT remaining_amount FROM debts WHERE id = %s FOR UPDATE;", (debt_id,))
    row = cur.fetchone()
    if not row:
        cur.close()
        conn.close()
        return False
        
    rem = float(row[0])
    if pay_amount > rem:
        pay_amount = rem # capped at remaining
        
    new_rem = rem - pay_amount
    cur.execute("""
        UPDATE debts
        SET remaining_amount = %s, updated_at = CURRENT_TIMESTAMP
        WHERE id = %s;
    """, (new_rem, debt_id))
    
    cur.execute("""
        INSERT INTO transactions (debt_id, amount_paid, collected_by)
        VALUES (%s, %s, %s);
    """, (debt_id, pay_amount, user_id))
    
    conn.commit()
    cur.close()
    conn.close()
    return True

# ---------- Telegram Bot State Definitions ----------
(
    NAME, PHONE, AMOUNT, NOTES, 
    DEBT_ID, PAY_AMOUNT, 
    EDIT_FIELD, EDIT_VALUE, 
    SEARCH_QUERY, USER_ID, USER_ROLE
) = range(11)

# ---------- Inline Keyboards Layout Builders ----------
def get_main_keyboard(role: str) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton("➕ Yangi Qarz Qo'shish", callback_data="menu_add")],
        [InlineKeyboardButton("🔍 Qarz Qidirish & To'lov", callback_data="menu_search")],
        [InlineKeyboardButton("📊 Qarzlar Hisoboti (CSV)", callback_data="menu_report")]
    ]
    if role in ['admin', 'super_admin']:
        buttons.append([InlineKeyboardButton("⚙️ Xodim Boshqaruvi", callback_data="menu_admin")])
    return InlineKeyboardMarkup(buttons)

def get_cancel_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Bekor qilish", callback_data="cancel_op")]])

# ---------- Telegram Bot Core Handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or ""
    full_name = update.effective_user.full_name or "Foydalanuvchi"
    
    db_user = get_user(user_id)
    if not db_user:
        # Check if this user should be auto super_admin
        role = 'xodim_user'
        if str(user_id) == os.environ.get('ADMIN_TELEGRAM_ID'):
            role = 'super_admin'
        create_user(user_id, username, full_name, role)
        db_user = get_user(user_id)
        
    await update.message.reply_text(
        f"Assalomu alaykum, {db_user['full_name']}!\n"
        f"Sizning tizimdagi rolingiz: *{db_user['role']}*\n\n"
        f"Quyidagi menyudan kerakli amalni tanlang:",
        parse_mode="Markdown",
        reply_markup=get_main_keyboard(db_user['role'])
    )
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    msg = "Amal bekor qilindi."
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.message.reply_text(msg)
    else:
        await update.message.reply_text(msg)
    return ConversationHandler.END

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    choice = query.data
    user_id = update.effective_user.id
    db_user = get_user(user_id)
    
    if not db_user:
        await query.message.reply_text("Tizimda xatolik yuz berdi. Qayta yuklash uchun /start bosing.")
        return ConversationHandler.END

    if choice == "menu_add":
        await query.message.reply_text("Yangi mijoz ism-familiyasini kiriting:", reply_markup=get_cancel_keyboard())
        return NAME
        
    elif choice == "menu_search":
        await query.message.reply_text("Qidirilayotgan mijoz ismi yoki telefon raqamini kiriting:", reply_markup=get_cancel_keyboard())
        return SEARCH_QUERY
        
    elif choice == "menu_report":
        await query.message.reply_text("Hisobot shakllantirilmoqda, iltimos kuting...")
        # Generate CSV report
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            SELECT c.name, c.phone, d.amount, d.remaining_amount, d.notes, d.created_at
            FROM debts d
            JOIN clients c ON d.client_id = c.id
            WHERE d.remaining_amount > 0
            ORDER BY d.created_at DESC;
        """)
        rows = cur.fetchall()
        cur.close()
        conn.close()
        
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["Mijoz", "Telefon", "Asl Qarz Miqdori", "Qolgan Qarz", "Izoh", "Sana"])
        for r in rows:
            writer.writerow([r[0], r[1], float(r[2]), float(r[3]), r[4], r[5].strftime("%Y-%m-%d %H:%M")])
            
        csv_bytes = output.getvalue().encode('utf-8')
        output.close()
        
        await query.message.reply_document(
            document=io.BytesIO(csv_bytes),
            filename=f"faol_qarzlar_{datetime.now().strftime('%Y%md_%H%M')}.csv",
            caption="📂 Barcha faol va to'liq yopilmagan qarzlar hisoboti."
        )
        return ConversationHandler.END
        
    elif choice == "menu_admin":
        if db_user['role'] not in ['admin', 'super_admin']:
            await query.message.reply_text("Ushbu bo'limga kirish huquqingiz yo'q.")
            return ConversationHandler.END
            
        keyboard = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Yangi Xodim Qo'shish", callback_data="admin_add_user")],
            [InlineKeyboardButton("🔙 Orqaga", callback_data="admin_back")]
        ])
        await query.message.reply_text("⚙️ Administrator boshqaruv paneli:", reply_markup=keyboard)
        return ConversationHandler.END

    elif choice == "admin_add_user":
        await query.message.reply_text("Yangi xodimning Telegram ID raqamini kiriting:", reply_markup=get_cancel_keyboard())
        return USER_ID
        
    elif choice == "admin_back":
        await query.message.reply_text("Asosiy menyu:", reply_markup=get_main_keyboard(db_user['role']))
        return ConversationHandler.END
        
    elif choice == "cancel_op":
        return await cancel(update, context)
        
    elif choice.startswith("pay_debt_"):
        debt_id = int(choice.split("_")[2])
        context.user_data['target_debt_id'] = debt_id
        await query.message.reply_text("Ushbu qarz hisobiga amalga oshirilgan to'lov miqdorini kiriting (faqat raqamlarda):", reply_markup=get_cancel_keyboard())
        return PAY_AMOUNT
        
    elif choice.startswith("client_detail_"):
        client_id = int(choice.split("_")[2])
        debts = get_client_debts_details(client_id)
        if not debts:
            await query.message.reply_text("Ushbu mijozning faol qarzlari mavjud emas.")
            return ConversationHandler.END
            
        for d in debts:
            text = (
                f"📝 *Qarz tafsilotlari (ID: {d['id']})*\n"
                f"💰 Asl miqdor: {format_amount(d['amount'])}\n"
                f"📉 Qolgan qarz: {format_amount(d['remaining_amount'])}\n"
                f"ℹ️ Izoh: {d['notes'] or 'Mavjud emas'}\n"
                f"📅 Sana: {d['created_at'].strftime('%Y-%m-%d %H:%M')}\n"
            )
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("💰 To'lov Qabul Qilish", callback_data=f"pay_debt_{d['id']}")]
            ])
            await query.message.reply_text(text, parse_mode="Markdown", reply_markup=keyboard)
        return ConversationHandler.END

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.user_data.get('state') # fallback tracking or direct flow checking via conversational handler states
    current_state = context.application.handlers[0][0].states # conversational metadata
    
    text = update.message.text.strip()
    user_id = update.effective_user.id
    db_user = get_user(user_id)
    
    # Identify active state manually or contextually inside conversation
    # We read state from standard flow logic
    # 1. New Client Registration Flow
    if 'new_client_name' not in context.user_data and current_state:
        # Check matching context
        pass

    # Better to control state transitions cleanly based on expected sequential keys:
    if context.user_data.get('flow') == 'add_debt' or ('new_client_name' not in context.user_data and not context.user_data.get('search_flow') and not context.user_data.get('admin_flow')):
        # Client name step
        if 'new_client_name' not in context.user_data:
            context.user_data['new_client_name'] = text
            await update.message.reply_text("Mijoz telefon raqamini kiriting (masalan, +998901234567):", reply_markup=get_cancel_keyboard())
            return PHONE
            
        elif 'new_client_phone' not in context.user_data:
            context.user_data['new_client_phone'] = text
            await update.message.reply_text("Qarz miqdorini kiriting (faqat raqamlar bilan):", reply_markup=get_cancel_keyboard())
            return AMOUNT
            
        elif 'new_client_amount' not in context.user_data:
            clean_amount = re.sub(r'[^\d.]', '', text)
            if not clean_amount:
                await update.message.reply_text("Noto'g'ri qiymat. Iltimos qarz miqdorini raqamlarda kiriting:")
                return AMOUNT
            context.user_data['new_client_amount'] = float(clean_amount)
            await update.message.reply_text("Ushbu qarz uchun izoh yoki mahsulotlar ro'yxatini yozing:", reply_markup=get_cancel_keyboard())
            return NOTES
            
        elif 'new_client_notes' not in context.user_data:
            context.user_data['new_client_notes'] = text
            
            # Save to Database
            c_id = add_client(context.user_data['new_client_name'], context.user_data['new_client_phone'])
            add_debt(c_id, context.user_data['new_client_amount'], context.user_data['new_client_notes'])
            
            await update.message.reply_text(
                f"✅ Qarz muvaffaqiyatli saqlandi!\n\n"
                f"👤 Mijoz: {context.user_data['new_client_name']}\n"
                f"📞 Tel: {context.user_data['new_client_phone']}\n"
                f"💰 Miqdor: {format_amount(context.user_data['new_client_amount'])}\n"
                f"📝 Izoh: {context.user_data['new_client_notes']}",
                reply_markup=get_main_keyboard(db_user['role'])
            )
            context.user_data.clear()
            return ConversationHandler.END

    # 2. Search Client Flow
    if context.user_data.get('search_flow') or ('target_debt_id' not in context.user_data and not context.user_data.get('admin_flow')):
        if 'search_query_str' not in context.user_data:
            context.user_data['search_query_str'] = text
            results = search_clients_with_debts(text)
            if not results:
                await update.message.reply_text("Hech qanday mos mijoz topilmadi. Qayta qidirish uchun ism kiriting yoki bekor qiling:", reply_markup=get_cancel_keyboard())
                context.user_data.pop('search_query_str', None)
                return SEARCH_QUERY
                
            await update.message.reply_text("Topilgan mijozlar ro'yxati:")
            for c in results:
                btn_txt = f"{c['name']} | Qarz: {format_amount(c['total_debt'])}"
                keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("📄 Batafsil ko'rish", callback_data=f"client_detail_{c['id']}")]])
                await update.message.reply_text(f"👤 {c['name']}\n📞 Tel: {c['phone'] or 'yoq'}\n💰 Umumiy balansi: {format_amount(c['total_debt'])}", reply_markup=keyboard)
                
            context.user_data.clear()
            return ConversationHandler.END

    # 3. Pay Debt Flow
    if 'target_debt_id' in context.user_data:
        clean_pay = re.sub(r'[^\d.]', '', text)
        if not clean_pay:
            await update.message.reply_text("Noto'g'ri format. Iltimos to'lov miqdorini raqamlarda kiriting:")
            return PAY_AMOUNT
            
        debt_id = context.user_data['target_debt_id']
        pay_amt = float(clean_pay)
        
        success = register_payment(debt_id, pay_amt, user_id)
        if success:
            await update.message.reply_text("✅ To'lov muvaffaqiyatli qabul qilindi va qarz balansidan chegirildi.", reply_markup=get_main_keyboard(db_user['role']))
        else:
            await update.message.reply_text("Xatolik: Qarz topilmadi yoki allaqachon yopilgan.", reply_markup=get_main_keyboard(db_user['role']))
            
        context.user_data.clear()
        return ConversationHandler.END

    # 4. Admin Add User Flow
    if context.user_data.get('admin_flow'):
        if 'new_user_id' not in context.user_data:
            if not text.isdigit():
                await update.message.reply_text("Telegram ID faqat raqamlardan iborat bo'lishi kerak:")
                return USER_ID
            context.user_data['new_user_id'] = int(text)
            
            keyboard = InlineKeyboardMarkup([
                [InlineKeyboardButton("Xodim (User)", callback_data="role_xodim_user")],
                [InlineKeyboardButton("Administrator (Admin)", callback_data="role_admin")]
            ])
            await update.message.reply_text("Yangi xodim uchun tizim rolingizni tanlang:", reply_markup=keyboard)
            return USER_ROLE

    return ConversationHandler.END

async def edit_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Fallback placeholder for inline actions
    await update.callback_query.answer()
    return ConversationHandler.END

async def edit_value_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return ConversationHandler.END

async def handle_admin_roles(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    role = query.data.replace("role_", "")
    
    if 'new_user_id' in context.user_data:
        tid = context.user_data['new_user_id']
        create_user(tid, "xodim_user", "Do'kon xodimi", role)
        await query.message.reply_text(f"✅ Yangi xodim tizimga muvaffaqiyatli muhrlandi.")
        context.user_data.clear()
    return ConversationHandler.END

# Set exact flow state routing markers before execution injection
async def pre_handler_routing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Route manually inside unified catch triggers if conversation handler slips states
    query = update.callback_query
    if query and query.data.startswith("role_"):
        return await handle_admin_roles(update, context)
    elif query and query.data.startswith("client_detail_"):
        return await menu_handler(update, context)
    elif query and query.data.startswith("pay_debt_"):
        return await menu_handler(update, context)
    elif query and query.data == "admin_add_user":
        context.user_data['admin_flow'] = True
        await query.message.reply_text("Yangi xodimning Telegram ID raqamini kiriting:", reply_markup=get_cancel_keyboard())
        return USER_ID
    return await menu_handler(update, context)

async def main_bot_async():
    """Asynchronous entry point that manages the bot lifecycle in a single event loop."""
    req = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    app = Application.builder().token(BOT_TOKEN).request(req).build()

    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(pre_handler_routing)],
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
            USER_ROLE: [CallbackQueryHandler(handle_admin_roles)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CallbackQueryHandler(cancel, pattern="cancel_op")]
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
    """This function runs the Telegram bot in a separate thread safely."""
    asyncio.run(main_bot_async())

# ---------- Main Entry Point ----------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    # Initialize connection to PostgreSQL database
    init_db()
    
    bot_thread = threading.Thread(target=run_telegram_bot)
    bot_thread.daemon = True
    bot_thread.start()
    
    port = int(os.environ.get('PORT', 5000))
    flask_app.run(host='0.0.0.0', port=port, debug=False, use_reloader=False)
