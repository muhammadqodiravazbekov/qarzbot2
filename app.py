import sqlite3
import logging
import csv
import io
import re
import unicodedata
import threading
import os
from datetime import datetime
from typing import List, Dict, Optional
from flask import Flask, request, jsonify, render_template_string
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (
    Application, CommandHandler, ConversationHandler,
    MessageHandler, CallbackQueryHandler, filters, ContextTypes
)
from telegram.request import HTTPXRequest

# ---------- Flask Web Server & Mini App Frontend ----------
flask_app = Flask(__name__)

# Premium Minimalist UI Design using Tailwind CSS
MINI_APP_HTML = """
<!DOCTYPE html>
<html lang="uz">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Qarz Nazorati</title>
    <script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>
        body { -webkit-tap-highlight-color: transparent; }
        .no-scrollbar::-webkit-scrollbar { display: none; }
    </style>
</head>
<body class="bg-slate-50 text-slate-900 font-sans antialiased pb-24 selection:bg-indigo-100">

    <div class="sticky top-0 z-30 bg-white/80 backdrop-blur-md border-b border-slate-100 px-4 py-4 flex items-center justify-between">
        <div>
            <h1 class="text-lg font-bold tracking-tight text-slate-900" id="user-greeting">Qarz Nazorati</h1>
            <p class="text-xs text-slate-400 font-medium" id="current-date">Yuklanmoqda...</p>
        </div>
        <button onclick="openModal('add-debt-modal')" class="bg-indigo-600 hover:bg-indigo-700 active:scale-95 text-white px-3.5 py-1.5 rounded-xl text-xs font-semibold tracking-wide shadow-sm shadow-indigo-100 transition-all flex items-center gap-1">
            <span class="text-sm font-bold">+</span> Yangi Qarz
        </button>
    </div>

    <div class="max-w-md mx-auto p-4 space-y-5">
        
        <div class="grid grid-cols-2 gap-3">
            <div class="bg-white border border-slate-100 p-4 rounded-2xl shadow-sm">
                <span class="text-[10px] font-bold text-slate-400 uppercase tracking-wider block">Umumiy Qarz</span>
                <span id="total-amount" class="text-xl font-extrabold text-slate-900 block mt-1">0.00 UZS</span>
            </div>
            <div class="bg-white border border-slate-100 p-4 rounded-2xl shadow-sm">
                <span class="text-[10px] font-bold text-slate-400 uppercase tracking-wider block">Faol Mijozlar</span>
                <span id="total-debtors" class="text-xl font-extrabold text-indigo-600 block mt-1">0 ta</span>
            </div>
        </div>

        <div class="space-y-2.5">
            <div class="relative">
                <input type="text" id="search-input" oninput="handleSearch()" placeholder="Mijoz ismi yoki telefon raqami..." 
                       class="w-full bg-white border border-slate-200 focus:border-indigo-500 focus:ring-1 focus:ring-indigo-500 rounded-xl pl-3 pr-10 py-2.5 text-sm outline-none transition-all placeholder:text-slate-400 shadow-sm">
                <span class="absolute right-3.5 top-3.5 text-slate-400 pointer-events-none">
                    <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2.5" d="M21 21l-6-6m2-5a7 7 0 11-14 0 7 7 0 0114 0z"></path></svg>
                </span>
            </div>
            
            <div class="flex gap-1.5 overflow-x-auto no-scrollbar py-0.5">
                <button onclick="filterDebts('all')" id="filter-all" class="px-3 py-1.5 rounded-lg text-xs font-semibold bg-slate-900 text-white shadow-sm transition-all whitespace-nowrap">Hammasi</button>
                <button onclick="filterDebts('active')" id="filter-active" class="px-3 py-1.5 rounded-lg text-xs font-medium bg-white border border-slate-200 text-slate-600 hover:bg-slate-50 transition-all whitespace-nowrap">Qarzdorlar</button>
                <button onclick="filterDebts('settled')" id="filter-settled" class="px-3 py-1.5 rounded-lg text-xs font-medium bg-white border border-slate-200 text-slate-600 hover:bg-slate-50 transition-all whitespace-nowrap">To'langanlar</button>
            </div>
        </div>

        <div class="space-y-3">
            <h3 class="text-xs font-bold text-slate-400 uppercase tracking-wider px-1">Tizimdagi yozuvlar</h3>
            
            <div id="loading-spinner" class="text-center py-12 text-slate-400 text-sm font-medium">
                Ma'lumotlar yuklanmoqda...
            </div>

            <div id="records-container" class="space-y-2.5 hidden">
                </div>
        </div>
    </div>

    <div id="add-debt-modal" class="fixed inset-0 bg-slate-900/40 backdrop-blur-sm z-50 flex items-end justify-center hidden opacity-0 transition-opacity duration-200">
        <div class="bg-white w-full max-w-md rounded-t-3xl p-5 space-y-4 shadow-xl transform translate-y-full transition-transform duration-200">
            <div class="flex justify-between items-center border-b border-slate-100 pb-3">
                <h3 class="font-bold text-slate-900 text-base">Yangi qarz yozuvi</h3>
                <button onclick="closeModal('add-debt-modal')" class="text-slate-400 hover:text-slate-600 text-lg font-bold px-2">&times;</button>
            </div>
            <form id="add-debt-form" onsubmit="submitAddDebt(event)" class="space-y-3">
                <div>
                    <label class="block text-xs font-semibold text-slate-500 mb-1">Mijoz to'liq ismi *</label>
                    <input type="text" id="form-name" required class="w-full border border-slate-200 rounded-xl px-3 py-2 text-sm outline-none focus:border-indigo-500">
                </div>
                <div>
                    <label class="block text-xs font-semibold text-slate-500 mb-1">Telefon raqami</label>
                    <input type="text" id="form-phone" placeholder="+998" class="w-full border border-slate-200 rounded-xl px-3 py-2 text-sm outline-none focus:border-indigo-500">
                </div>
                <div>
                    <label class="block text-xs font-semibold text-slate-500 mb-1">Qarz miqdori (UZS) *</label>
                    <input type="number" id="form-amount" required min="1" class="w-full border border-slate-200 rounded-xl px-3 py-2 text-sm outline-none focus:border-indigo-500">
                </div>
                <div>
                    <label class="block text-xs font-semibold text-slate-500 mb-1">Qisqacha izoh</label>
                    <input type="text" id="form-notes" class="w-full border border-slate-200 rounded-xl px-3 py-2 text-sm outline-none focus:border-indigo-500">
                </div>
                <button type="submit" class="w-full bg-indigo-600 hover:bg-indigo-700 text-white font-semibold py-2.5 rounded-xl text-sm transition-all mt-2">Saqlash</button>
            </form>
        </div>
    </div>

    <div id="payment-modal" class="fixed inset-0 bg-slate-900/40 backdrop-blur-sm z-50 flex items-end justify-center hidden opacity-0 transition-opacity duration-200">
        <div class="bg-white w-full max-w-md rounded-t-3xl p-5 space-y-4 shadow-xl transform translate-y-full transition-transform duration-200">
            <div class="flex justify-between items-center border-b border-slate-100 pb-3">
                <h3 class="font-bold text-slate-900 text-base" id="payment-title">Tizimli to'lov</h3>
                <button onclick="closeModal('payment-modal')" class="text-slate-400 hover:text-slate-600 text-lg font-bold px-2">&times;</button>
            </div>
            <form id="payment-form" onsubmit="submitPayment(event)" class="space-y-3">
                <input type="hidden" id="payment-debt-id">
                <div>
                    <label class="block text-xs font-semibold text-slate-500 mb-1">Maksimal to'lov</label>
                    <input type="text" id="payment-max-display" readonly class="w-full bg-slate-50 border border-slate-100 rounded-xl px-3 py-2 text-sm text-slate-400 outline-none">
                </div>
                <div>
                    <label class="block text-xs font-semibold text-slate-500 mb-1">To'lov miqdori (UZS) *</label>
                    <input type="number" id="payment-amount" required min="1" class="w-full border border-slate-200 rounded-xl px-3 py-2 text-sm outline-none focus:border-indigo-500">
                </div>
                <div>
                    <label class="block text-xs font-semibold text-slate-500 mb-1">To'lov izohi</label>
                    <input type="text" id="payment-notes" placeholder="Naqd, karta orqali..." class="w-full border border-slate-200 rounded-xl px-3 py-2 text-sm outline-none focus:border-indigo-500">
                </div>
                <div class="grid grid-cols-2 gap-2 mt-2">
                    <button type="button" onclick="submitDeleteDebt()" class="bg-rose-50 hover:bg-rose-100 text-rose-600 font-semibold py-2.5 rounded-xl text-sm transition-all border border-rose-100">O'chirish</button>
                    <button type="submit" class="bg-indigo-600 hover:bg-indigo-700 text-white font-semibold py-2.5 rounded-xl text-sm transition-all">To'lovni kiritish</button>
                </div>
            </form>
        </div>
    </div>

    <script>
        const tg = window.Telegram.WebApp;
        tg.ready();
        tg.expand();

        let rawDebtsData = [];
        let activeFilter = 'all';
        let currentUserId = 0;

        // Display Greeting Based on Execution Context
        const user = tg.initDataUnsafe?.user;
        if (user) {
            currentUserId = user.id;
            document.getElementById('user-greeting').innerText = user.first_name;
        }
        document.getElementById('current-date').innerText = new Date().toLocaleDateString('uz-UZ', { weekday: 'long', month: 'long', day: 'numeric' });

        // Functional Window Modals Controls
        function openModal(id) {
            const modal = document.getElementById(id);
            modal.classList.remove('hidden');
            setTimeout(() => {
                modal.classList.remove('opacity-0');
                modal.querySelector('div').classList.remove('translate-y-full');
            }, 10);
        }

        function closeModal(id) {
            const modal = document.getElementById(id);
            modal.classList.add('opacity-0');
            modal.querySelector('div').classList.add('translate-y-full');
            setTimeout(() => modal.classList.add('hidden'), 200);
        }

        function openPaymentModal(id, name, remaining) {
            if (remaining <= 0) return; // already paid
            document.getElementById('payment-debt-id').value = id;
            document.getElementById('payment-title').innerText = `${name} - To'lov`;
            document.getElementById('payment-max-display').value = new Intl.NumberFormat('uz-UZ').format(remaining) + ' UZS';
            document.getElementById('payment-amount').max = remaining;
            document.getElementById('payment-amount').value = '';
            openModal('payment-modal');
        }

        // Fetch Operational Context Records From Core Engine
        async function loadDataStream() {
            try {
                const response = await fetch('/api/dashboard');
                const data = await response.json();
                
                rawDebtsData = data.debts;
                
                document.getElementById('total-amount').innerText = new Intl.NumberFormat('uz-UZ').format(data.total_outstanding) + ' UZS';
                document.getElementById('total-debtors').innerText = data.debts.filter(d => d.remaining_balance > 0).length + ' ta';
                
                renderRecordsList(rawDebtsData);
                
                document.getElementById('loading-spinner').classList.add('hidden');
                document.getElementById('records-container').classList.remove('hidden');
            } catch (err) {
                console.error("Critical Stream Interruption:", err);
                document.getElementById('loading-spinner').innerText = "Aloqa xatosi yuz berdi.";
            }
        }

        // Display Render Loop Filtering
        function renderRecordsList(items) {
            const container = document.getElementById('records-container');
            container.innerHTML = '';
            
            if(items.length === 0) {
                container.innerHTML = '<div class="text-center py-10 text-slate-400 text-sm font-medium">Hech qanday ma\'lumot topilmadi</div>';
                return;
            }

            items.forEach(item => {
                const isSettled = item.remaining_balance <= 0;
                const element = document.createElement('div');
                element.className = `bg-white border ${isSettled ? 'border-slate-100 opacity-60' : 'border-slate-200/60'} p-4 rounded-xl shadow-sm flex justify-between items-center transition-all active:scale-[0.99] cursor-pointer`;
                element.setAttribute('onclick', `openPaymentModal(${item.id}, "${item.customer_name}", ${item.remaining_balance})`);
                
                element.innerHTML = `
                    <div class="space-y-1">
                        <h4 class="font-bold text-slate-800 text-sm tracking-tight">${item.customer_name}</h4>
                        <p class="text-xs text-slate-400 font-medium">${item.phone ? '📞 ' + item.phone : '📞 Kiritilmagan'}</p>
                        ${item.notes ? `<p class="text-xs text-slate-500 bg-slate-50 inline-block px-2 py-0.5 rounded-md border border-slate-100">${item.notes}</p>` : ''}
                    </div>
                    <div class="text-right">
                        <span class="text-sm font-black ${isSettled ? 'text-emerald-600' : 'text-rose-600'}">
                            ${isSettled ? "Yopilgan" : new Intl.NumberFormat('uz-UZ').format(item.remaining_balance) + ' UZS'}
                        </span>
                        <p class="text-[10px] text-slate-400 font-bold mt-0.5 uppercase tracking-wider">Mas'ul: ${item.seller_name}</p>
                    </div>
                `;
                container.appendChild(element);
            });
        }

        // Realtime Unified Data Processing
        function handleSearch() {
            const query = document.getElementById('search-input').value.toLowerCase();
            applyFiltersAndSearch(query, activeFilter);
        }

        function filterDebts(type) {
            activeFilter = type;
            ['all', 'active', 'settled'].forEach(t => {
                const btn = document.getElementById(`filter-${t}`);
                if(t === type) {
                    btn.className = "px-3 py-1.5 rounded-lg text-xs font-semibold bg-slate-900 text-white shadow-sm transition-all whitespace-nowrap";
                } else {
                    btn.className = "px-3 py-1.5 rounded-lg text-xs font-medium bg-white border border-slate-200 text-slate-600 hover:bg-slate-50 transition-all whitespace-nowrap";
                }
            });
            const query = document.getElementById('search-input').value.toLowerCase();
            applyFiltersAndSearch(query, type);
        }

        function applyFiltersAndSearch(query, filter) {
            let filtered = rawDebtsData;
            
            if (filter === 'active') {
                filtered = filtered.filter(d => d.remaining_balance > 0);
            } else if (filter === 'settled') {
                filtered = filtered.filter(d => d.remaining_balance <= 0);
            }
            
            if (query) {
                filtered = filtered.filter(d => 
                    d.customer_name.toLowerCase().includes(query) || 
                    (d.phone && d.phone.includes(query))
                );
            }
            renderRecordsList(filtered);
        }

        // Submissions Operations Endpoints Routing
        async function submitAddDebt(e) {
            e.preventDefault();
            const payload = {
                customer_name: document.getElementById('form-name').value,
                phone: document.getElementById('form-phone').value,
                amount: parseFloat(document.getElementById('form-amount').value),
                notes: document.getElementById('form-notes').value,
                seller_id: currentUserId || 123456789 // Fallback dummy if testing outside Telegram
            };

            try {
                const res = await fetch('/api/add_debt', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                if (res.ok) {
                    closeModal('add-debt-modal');
                    document.getElementById('add-debt-form').reset();
                    loadDataStream();
                    tg.HapticFeedback.notificationOccurred('success');
                }
            } catch (err) {
                alert("Xatolik yuz berdi");
            }
        }

        async function submitPayment(e) {
            e.preventDefault();
            const payload = {
                debt_id: parseInt(document.getElementById('payment-debt-id').value),
                amount: parseFloat(document.getElementById('payment-amount').value),
                notes: document.getElementById('payment-notes').value
            };

            try {
                const res = await fetch('/api/pay_debt', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                if (res.ok) {
                    closeModal('payment-modal');
                    loadDataStream();
                    tg.HapticFeedback.notificationOccurred('success');
                } else {
                    const errData = await res.json();
                    alert(errData.error || "Xatolik");
                }
            } catch (err) {
                alert("Xatolik yuz berdi");
            }
        }

        async function submitDeleteDebt() {
            const debtId = document.getElementById('payment-debt-id').value;
            if(!confirm("Haqiqatan ham ushbu qarzni butunlay o'chirmoqchimisiz?")) return;
            
            try {
                const res = await fetch(`/api/delete_debt/${debtId}`, { method: 'DELETE' });
                if(res.ok) {
                    closeModal('payment-modal');
                    loadDataStream();
                    tg.HapticFeedback.notificationOccurred('warning');
                }
            } catch(err) {
                alert("O'chirishda xatolik");
            }
        }

        // Initialize Context Data Initialization
        loadDataStream();
    </script>
</body>
</html>
"""

@flask_app.route('/')
@flask_app.route('/health')
def health():
    return jsonify({"status": "alive", "message": "Server online!"}), 200

@flask_app.route('/webapp')
def webapp_interface():
    """Serves the highly responsive functional Mini App view."""
    return render_template_string(MINI_APP_HTML)

@flask_app.route('/api/dashboard')
def api_dashboard_metrics():
    try:
        total = get_total_outstanding()
        debts = get_all_debts()
        return jsonify({"total_outstanding": total, "debts": debts})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@flask_app.route('/api/add_debt', methods=['POST'])
def api_add_debt():
    data = request.json or {}
    try:
        # Validate that the context user exists before creating the record.
        # If the context execution came via web interface outside a /start flow, 
        # seamlessly register the user implicitly as a fallback admin.
        seller_id = int(data.get('seller_id', 0))
        if seller_id and not get_user(seller_id):
            create_user(seller_id, "webapp_user", "Mini App foydalanuvchisi", "admin")
            
        debt_id = add_debt(
            customer_name=data.get('customer_name', '').strip(),
            phone=data.get('phone', '').strip(),
            amount=float(data.get('amount', 0)),
            notes=data.get('notes', '').strip(),
            seller_telegram_id=seller_id
        )
        return jsonify({"success": True, "debt_id": debt_id}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@flask_app.route('/api/pay_debt', methods=['POST'])
def api_pay_debt():
    data = request.json or {}
    try:
        success = add_payment(
            debt_id=int(data.get('debt_id', 0)),
            amount=float(data.get('amount', 0)),
            notes=data.get('notes', '').strip()
        )
        if success:
            return jsonify({"success": True}), 200
        return jsonify({"error": "Noto'g'ri to'lov summasi yoki qarz allaqachon yopilgan."}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@flask_app.route('/api/delete_debt/<int:debt_id>', methods=['DELETE'])
def api_delete_debt(debt_id):
    try:
        if delete_debt(debt_id):
            return jsonify({"success": True}), 200
        return jsonify({"error": "Qarz topilmadi"}), 404
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ---------- Persistent Storage Configuration ----------
BOT_TOKEN = os.environ.get('BOT_TOKEN')
if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required!")
DATABASE_PATH = os.environ.get('DATABASE_PATH', '/data/debts.db')

# ---------- Text Engine Parser ----------
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

# ---------- Thread-Safe SQLite Isolation Context ----------
def get_db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS users (
            telegram_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            role TEXT CHECK(role IN ('admin','seller','viewer')) NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS debts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_name TEXT NOT NULL,
            customer_name_normalized TEXT,
            phone TEXT,
            amount_owed REAL NOT NULL,
            remaining_balance REAL NOT NULL,
            notes TEXT,
            seller_telegram_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (seller_telegram_id) REFERENCES users(telegram_id)
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_debt_name_normalized ON debts(customer_name_normalized)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_debt_phone ON debts(phone)')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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

# ---------- Business Logic Controllers ----------
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
        cursor.execute("INSERT INTO users (telegram_id, username, first_name, role) VALUES (?, ?, ?, ?)",
                       (telegram_id, username, first_name, role))
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        return False
    finally:
        conn.close()

def delete_user(telegram_id: int) -> bool:
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM users WHERE telegram_id = ?", (telegram_id,))
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
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (customer_name, norm_name, phone, amount, amount, notes, seller_telegram_id)
    )
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
    if not debt or amount <= 0 or amount > debt["remaining_balance"]:
        return False
    new_balance = debt["remaining_balance"] - amount
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("INSERT INTO payments (debt_id, amount_paid, notes) VALUES (?, ?, ?)", (debt_id, amount, notes))
    cursor.execute("UPDATE debts SET remaining_balance = ?, updated_at = ? WHERE id = ?", (new_balance, datetime.now().isoformat(), debt_id))
    conn.commit()
    conn.close()
    return True

def get_all_debts() -> List[Dict]:
    query = """
        SELECT d.id, d.customer_name, d.phone, d.amount_owed, d.remaining_balance, d.notes,
               d.seller_telegram_id, d.created_at, u.username, u.first_name
        FROM debts d
        JOIN users u ON d.seller_telegram_id = u.telegram_id
        ORDER BY d.remaining_balance DESC, d.created_at DESC
    """
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(query)
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

# ---------- Unified Core Keyboards & Handlers ----------
def get_main_keyboard(role: str):
    # Dynamic secure domain injection
    app_host = os.environ.get('RENDER_EXTERNAL_HOSTNAME', 'qarzbot2-1.onrender.com')
    webapp_url = f"https://{app_host}/webapp"
    
    keyboard = [
        [InlineKeyboardButton("📱 Ilovani ochish (Mini App)", web_app=WebAppInfo(url=webapp_url))]
    ]
    if role == "admin":
        keyboard.append([InlineKeyboardButton("👥 Foydalanuvchilar boshqaruvi", callback_data="menu_users")])
    return InlineKeyboardMarkup(keyboard)

def get_users_menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Foydalanuvchi qo'shish", callback_data="menu_adduser")],
        [InlineKeyboardButton("📋 Ro'yxatni ko'rish", callback_data="menu_listusers")],
        [InlineKeyboardButton("🔙 Orqaga", callback_data="menu_back")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    db_user = get_user(user.id)
    if not db_user:
        admins = [u for u in get_admins_and_sellers() if u["role"] == "admin"]
        if not admins:
            create_user(user.id, user.username or "", user.first_name or "", "admin")
            db_user = get_user(user.id)
            await update.message.reply_text(
                f"✅ Tizim faollashtirildi!\nSiz birinchi foydalanuvchi bo'lganingiz sababli **ADMIN** etib belgilandingiz.\n\n"
                f"Boshqarish uchun quyidagi tugmani bosing:",
                reply_markup=get_main_keyboard("admin")
            )
        else:
            await update.message.reply_text("❌ Kirish taqiqlangan. Siz ro'yxatdan o'tmagansiz. Admin bilan bog'laning.")
        return
    await update.message.reply_text(
        f"✅ Assalomu alaykum {user.first_name}!\nTizimdagi rolingiz: **{db_user['role'].upper()}**\n\n"
        f"Ilovani ochish uchun pastdagi tugmani bosing:",
        reply_markup=get_main_keyboard(db_user['role'])
    )

NAME, PHONE, AMOUNT, NOTES, DEBT_ID, PAY_AMOUNT, EDIT_FIELD, EDIT_VALUE, SEARCH_QUERY, USER_ID, USER_ROLE = range(11)

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    db_user = get_user(query.from_user.id)
    if not db_user or db_user['role'] != 'admin':
        await query.edit_message_text("Taqiqlangan amal.")
        return
    
    if query.data == "menu_users":
        await query.edit_message_text("👥 **Foydalanuvchilarni boshqarish paneli**", reply_markup=get_users_menu())
    elif query.data == "menu_listusers":
        users = get_all_users()
        msg = "📋 **Tizim foydalanuvchilari:**\n\n"
        for u in users:
            msg += f"• {u['first_name']} (@{u['username']}) - {u['role']} (ID: `{u['telegram_id']}`)\n"
        await query.edit_message_text(msg, parse_mode="Markdown", reply_markup=get_users_menu())
    elif query.data == "menu_adduser":
        context.user_data['action'] = 'adduser'
        await query.edit_message_text("➕ Yangi foydalanuvchining **Telegram ID** raqamini yuboring:")
        return USER_ID
    elif query.data == "menu_back":
        await query.edit_message_text("Asosiy boshqaruv menyusi", reply_markup=get_main_keyboard(db_user['role']))

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    action = context.user_data.get('action')
    if action == 'adduser':
        try:
            telegram_id = int(update.message.text.strip())
            context.user_data['new_user_id'] = telegram_id
            context.user_data['action'] = 'adduser_role'
            await update.message.reply_text("Foydalanuvchi rolini tanlang (admin / seller / viewer):")
            return USER_ROLE
        except ValueError:
            await update.message.reply_text("Noto'g'ri ID. Raqam yuboring:")
            return USER_ID
            
    elif action == 'adduser_role':
        role = update.message.text.strip().lower()
        if role not in ("admin", "seller", "viewer"):
            await update.message.reply_text("Noto'g'ri rol. Qayta urinib ko'ring:")
            return USER_ROLE
        tid = context.user_data['new_user_id']
        create_user(tid, "user", "Xodim", role)
        await update.message.reply_text(f"✅ Xodim muvaffaqiyatli qo'shildi.")
        context.user_data.clear()
        return ConversationHandler.END

def run_telegram_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    req = HTTPXRequest(connect_timeout=30.0, read_timeout=30.0)
    app = Application.builder().token(BOT_TOKEN).request(req).build()

    conv_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(menu_handler)],
        states={
            USER_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input)],
            USER_ROLE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input)],
        },
        fallbacks=[]
    )
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.run_polling(stop_signals=None)

# ---------- Thread-Safe Application Orchestration ----------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    init_db()
    
    bot_thread = threading.Thread(target=run_telegram_bot)
    bot_thread.daemon = True
    bot_thread.start()
    
    port = int(os.environ.get('PORT', 5000))
    flask_app.run(host='0.0.0.0', port=port)
