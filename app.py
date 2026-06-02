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
from flask import Flask, request, jsonify, render_template_string
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (
    Application, CommandHandler, ConversationHandler,
    MessageHandler, CallbackQueryHandler, filters, ContextTypes
)
from telegram.request import HTTPXRequest

# ---------- Flask Web Server & Mini App ----------
flask_app = Flask(__name__)

# Embedded HTML Dashboard for the Mini App
MINI_APP_HTML = """
<!DOCTYPE html>
<html lang="uz">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Qarz Назорат Тизими</title>
    <script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
</head>
<body class="bg-slate-50 text-slate-800 font-sans antialiased pb-10">
    
    <div class="bg-blue-600 text-white p-5 shadow-md rounded-b-2xl">
        <div class="flex justify-between items-center">
            <div>
                <h1 class="text-xl font-bold tracking-wide">Qarzlar Назорати</h1>
                <p id="user-greeting" class="text-sm text-blue-100 mt-0.5">Юкланмоқда...</p>
            </div>
            <span class="bg-blue-500 text-xs px-2.5 py-1 rounded-full border border-blue-400 font-medium uppercase tracking-wider">Mini App</span>
        </div>
    </div>

    <div class="p-4">
        <div class="bg-white p-5 rounded-2xl shadow-sm border border-slate-100 flex flex-col items-center justify-center">
            <span class="text-sm font-semibold text-slate-400 uppercase tracking-wider">Умумий Қарздорлик</span>
            <span id="total-amount" class="text-3xl font-black text-rose-600 mt-1">0.00 сўм</span>
        </div>
    </div>

    <div class="px-4 mt-2">
        <h2 class="text-base font-bold text-slate-500 mb-3 px-1 flex items-center gap-2">
            📋 Амалдаги қарздорлар рўйхати
        </h2>
        
        <div id="loading-state" class="text-center py-10 text-slate-400 font-medium">
            Маълумотлар юкланмоқда...
        </div>

        <div id="debt-list" class="space-y-3 hidden">
            </div>
    </div>

    <script>
        // Initialize Telegram Web App
        const tg = window.Telegram.WebApp;
        tg.ready();
        tg.expand(); // Open to full height

        // Get user details from Telegram context
        const user = tg.initDataUnsafe?.user;
        if (user) {
            document.getElementById('user-greeting').innerText = `👋 Салом, ${user.first_name}!`;
        } else {
            document.getElementById('user-greeting').innerText = `👋 Салом, Меҳмон!`;
        }

        // Fetch Data from our Flask API
        async function loadDashboardData() {
            try {
                const response = await fetch('/api/dashboard');
                const data = await response.json();
                
                // Set total
                document.getElementById('total-amount').innerText = new Intl.NumberFormat('uz-UZ').format(data.total_outstanding) + ' сўм';
                
                const listContainer = document.getElementById('debt-list');
                const loadingState = document.getElementById('loading-state');
                listContainer.innerHTML = '';

                if (data.debts.length === 0) {
                    listContainer.innerHTML = '<div class="text-center py-8 text-slate-400">Ҳозирча ҳеч қандай қарз йўқ 🎉</div>';
                } else {
                    data.debts.forEach(debt => {
                        const card = document.createElement('div');
                        card.className = "bg-white p-4 rounded-xl shadow-sm border border-slate-100 flex justify-between items-center transition-all active:scale-[0.98]";
                        card.innerHTML = `
                            <div>
                                <h3 class="font-bold text-slate-800 text-base">${debt.customer_name}</h3>
                                <p class="text-xs text-slate-400 mt-0.5">${debt.phone ? '📞 ' + debt.phone : '📞 Киритилмаган'}</p>
                                <span class="inline-block mt-2 text-[10px] bg-slate-100 text-slate-500 px-2 py-0.5 rounded font-medium">ID: ${debt.id} | Масъул: ${debt.seller_name}</span>
                            </div>
                            <div class="text-right">
                                <span class="text-base font-extrabold text-rose-600">${new Intl.NumberFormat('uz-UZ').format(debt.remaining_balance)}</span>
                                <p class="text-[10px] text-slate-400 mt-0.5">сўм қолдиқ</p>
                            </div>
                        `;
                        listContainer.appendChild(card);
                    });
                }
                
                loadingState.classList.add('hidden');
                listContainer.classList.remove('hidden');

            } catch (error) {
                console.error("Data load error:", error);
                document.getElementById('loading-state').innerText = "Хатолик юз берди. Илтимос қайта уриниб кўринг.";
            }
        }

        // Trigger on load
        loadDashboardData();
    </script>
</body>
</html>
"""

@flask_app.route('/')
@flask_app.route('/health')
def health():
    return jsonify({"status": "alive", "message": "Bot is running!"}), 200

@flask_app.route('/webapp')
def webapp_dashboard():
    """Serves the visually dynamic Mini App page."""
    return render_template_string(MINI_APP_HTML)

@flask_app.route('/api/dashboard')
def api_dashboard_data():
    """API endpoint providing metrics and records safely to our interface."""
    try:
        total = get_total_outstanding()
        debts = get_all_debts()
        # Formatting datetimes to strings to prevent JSON encoder crashes
        for d in debts:
            if isinstance(d.get('created_at'), datetime):
                d['created_at'] = d['created_at'].isoformat()
            if isinstance(d.get('updated_at'), datetime):
                d['updated_at'] = d['updated_at'].isoformat()
        return jsonify({
            "total_outstanding": total,
            "debts": debts
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
    """Set up the cloud PostgreSQL database schemas."""
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
    # Embedded WebApp URL directly inside the command menu
    webapp_url = f"https://{os.environ.get('RENDER_EXTERNAL_HOSTNAME', '')}.onrender.com/webapp"
    
    keyboard = [
        [InlineKeyboardButton("📱 Очиш (Mini App)", web_app=WebAppInfo(url=webapp_url))]
    ]
    
    if role == "admin":
        keyboard.extend([
            [InlineKeyboardButton("➕ Қарз қўшиш", callback_data="menu_adddebt"), InlineKeyboardButton("💰 Тўлов қабул қилиш", callback_data="menu_pay")],
            [InlineKeyboardButton("🔍 Қарзларни излаш", callback_data="menu_search"), InlineKeyboardButton("👥 Фойдаланувчилар", callback_data="menu_users")],
            [InlineKeyboardButton("❌ Бекор қилиш", callback_data="menu_cancel")]
        ])
    elif role == "seller":
        keyboard.extend([
            [InlineKeyboardButton("➕ Қарз қўшиш", callback_data="menu_adddebt"), InlineKeyboardButton("💰 Тўлов қабул қилиш", callback_data="menu_pay")],
            [InlineKeyboardButton("🔍 Қарзларни излаш", callback_data="menu_search")],
            [InlineKeyboardButton("❌ Бекор қилиш", callback_data="menu_cancel")]
        ])
    else:
        keyboard.extend([
            [InlineKeyboardButton("🔍 Қарзларни излаш", callback_data="menu_search")],
            [InlineKeyboardButton("❌ Бекор қилиш", callback_data="menu_cancel")]
        ])
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
                f"Қарзларни бошқариш учун қуйидаги тугмадан Mini App'ни очинг:",
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
        f"Тизимга кириш учун тугмани босинг:",
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
    
    elif action == "menu_search":
        context.user_data['action'] = 'search'
        await query.edit_message_text("🔍 **Қарзларни излаш**\n\nМизож **исми** ёки **телефон рақамини** ёзинг:", parse_mode="Markdown")
        return SEARCH_QUERY
    
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

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Амал бекор қилинди.")
    context.user_data.clear()
    db_user = get_user(update.effective_user.id)
    if db_user:
        await update.message.reply_text("Асосий меню:", reply_markup=get_main_keyboard(db_user['role']))
    return ConversationHandler.END

def run_telegram_bot():
    """This function runs the Telegram bot in a separate thread."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

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
            SEARCH_QUERY: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input)],
            USER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input)],
            USER_ROLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))

    logging.info("Telegram bot started.")
    app.run_polling(stop_signals=None)

# ---------- Main Entry Point ----------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    
    # Initialize connection to PostgreSQL database
    init_db()
    
    bot_thread = threading.Thread(target=run_telegram_bot)
    bot_thread.daemon = True
    bot_thread.start()
    
    port = int(os.environ.get('PORT', 5000))
    logging.info(f"Starting Flask web server on port {port}")
    flask_app.run(host='0.0.0.0', port=port)
