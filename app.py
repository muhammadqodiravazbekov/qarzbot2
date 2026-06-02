import sqlite3
import logging
import csv
import io
import re
import unicodedata
import os
import asyncio
from datetime import datetime
from typing import List, Dict, Optional
from flask import Flask, request, jsonify, render_template_string, Response
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo, Bot
from telegram.ext import (
    Application, CommandHandler, ConversationHandler,
    MessageHandler, CallbackQueryHandler, filters, ContextTypes
)

# ---------- Flask Web Server & Mini App Frontend ----------
flask_app = Flask(__name__)

MINI_APP_HTML = """
<!DOCTYPE html>
<html lang="uz">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Qarz Kontrol</title>
    <script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
    <style>body { -webkit-tap-highlight-color: transparent; } .no-scrollbar::-webkit-scrollbar { display: none; }</style>
</head>
<body class="bg-[#f8fafc] text-[#0f172a] font-sans antialiased pb-24 selection:bg-indigo-50">
    <div class="sticky top-0 z-30 bg-white/80 backdrop-blur-md border-b border-slate-100 px-4 py-3.5 flex items-center justify-between">
        <div>
            <h1 class="text-base font-bold tracking-tight text-slate-900 flex items-center gap-1.5">
                <span class="w-2 h-2 rounded-full bg-emerald-500 animate-pulse"></span>
                <span id="user-greeting">Boshqaruv Paneli</span>
            </h1>
            <p class="text-[11px] text-slate-400 font-medium" id="current-date">Yuklanmoqda...</p>
        </div>
        <div class="flex items-center gap-2">
            <a href="/api/export_csv" target="_blank" class="p-2 text-slate-500 hover:text-slate-700 bg-slate-100 rounded-xl transition-all active:scale-95">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M12 10v6m0 0l-3-3m3 3l3-3m2 8H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z"></path></svg>
            </a>
            <button onclick="openModal('add-debt-modal')" class="bg-indigo-600 hover:bg-indigo-700 active:scale-95 text-white px-3.5 py-1.5 rounded-xl text-xs font-semibold shadow-sm transition-all">+ Yangi Qarz</button>
        </div>
    </div>
    <div class="max-w-md mx-auto p-4 space-y-4">
        <div class="grid grid-cols-3 gap-2">
            <div class="bg-white border border-slate-100 p-3 rounded-2xl shadow-sm"><span class="text-[9px] font-bold text-slate-400 uppercase tracking-wider block">Jami</span><span id="total-amount" class="text-sm font-extrabold text-slate-900 block mt-0.5 truncate">0 UZS</span></div>
            <div class="bg-white border border-slate-100 p-3 rounded-2xl shadow-sm"><span class="text-[9px] font-bold text-slate-400 uppercase tracking-wider block">Qarzdorlar</span><span id="total-debtors" class="text-sm font-extrabold text-indigo-600 block mt-0.5">0 ta</span></div>
            <div class="bg-white border border-slate-100 p-3 rounded-2xl shadow-sm"><span class="text-[9px] font-bold text-slate-400 uppercase tracking-wider block">Yopilgan</span><span id="total-settled" class="text-sm font-extrabold text-emerald-600 block mt-0.5">0 ta</span></div>
        </div>
        <div id="records-container" class="space-y-2"></div>
    </div>
    <script>
        const tg = window.Telegram.WebApp; tg.ready(); tg.expand();
        async function loadData() {
            const res = await fetch('/api/dashboard');
            const data = await res.json();
            document.getElementById('total-amount').innerText = new Intl.NumberFormat('uz-UZ').format(data.total_outstanding) + ' UZS';
            const container = document.getElementById('records-container');
            container.innerHTML = data.debts.map(d => `<div class="bg-white p-4 rounded-xl border border-slate-200">${d.customer_name} - ${d.remaining_balance} UZS</div>`).join('');
        }
        loadData();
    </script>
</body>
</html>
"""

# ---------- Configuration ----------
BOT_TOKEN = os.environ.get('BOT_TOKEN')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL')
DATABASE_PATH = os.environ.get('DATABASE_PATH', 'debts.db')

# ---------- Database Helpers ----------
def get_db():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('''CREATE TABLE IF NOT EXISTS users (telegram_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, role TEXT CHECK(role IN ('admin','seller','viewer')) NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS debts (id INTEGER PRIMARY KEY AUTOINCREMENT, customer_name TEXT NOT NULL, customer_name_normalized TEXT, phone TEXT, amount_owed REAL NOT NULL, remaining_balance REAL NOT NULL, notes TEXT, seller_telegram_id INTEGER NOT NULL, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, FOREIGN KEY (seller_telegram_id) REFERENCES users(telegram_id))''')
    cursor.execute('''CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY AUTOINCREMENT, debt_id INTEGER NOT NULL, amount_paid REAL NOT NULL, payment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP, notes TEXT, FOREIGN KEY (debt_id) REFERENCES debts(id) ON DELETE CASCADE)''')
    conn.commit(); conn.close()

def normalize_text(text: str) -> str:
    if not text: return ""
    return re.sub(r'[^a-z0-9]', '', unicodedata.normalize('NFKD', text.lower()).encode('ASCII', 'ignore').decode('ASCII'))

def get_user(telegram_id: int):
    conn = get_db(); cursor = conn.cursor()
    cursor.execute("SELECT telegram_id, username, first_name, role FROM users WHERE telegram_id = ?", (telegram_id,))
    row = cursor.fetchone(); conn.close()
    return {"telegram_id": row[0], "username": row[1], "first_name": row[2], "role": row[3]} if row else None

def create_user(telegram_id, username, first_name, role):
    conn = get_db(); cursor = conn.cursor()
    try: cursor.execute("INSERT INTO users (telegram_id, username, first_name, role) VALUES (?, ?, ?, ?)", (telegram_id, username, first_name, role)); conn.commit()
    finally: conn.close()

def get_all_debts():
    conn = get_db(); cursor = conn.cursor()
    cursor.execute("SELECT d.id, d.customer_name, d.phone, d.amount_owed, d.remaining_balance, d.notes, u.first_name FROM debts d JOIN users u ON d.seller_telegram_id = u.telegram_id")
    rows = cursor.fetchall(); conn.close()
    return [{"id": r[0], "customer_name": r[1], "phone": r[2], "amount_owed": r[3], "remaining_balance": r[4], "notes": r[5], "seller_name": r[6]} for r in rows]

# ---------- Bot Handlers ----------
async def start(update, context):
    user = update.effective_user
    db_user = get_user(user.id)
    if not db_user:
        create_user(user.id, user.username or "", user.first_name or "", "admin")
    await update.message.reply_text("Xush kelibsiz!", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Ilovani ochish", web_app=WebAppInfo(url=f"{WEBHOOK_URL}/webapp"))]]))

# ---------- Bot Setup ----------
bot_app = Application.builder().token(BOT_TOKEN).build()
bot_app.add_handler(CommandHandler("start", start))

# ---------- Flask Routes ----------
@flask_app.route('/webapp')
def webapp(): return render_template_string(MINI_APP_HTML)

@flask_app.route('/api/dashboard')
def dashboard():
    debts = get_all_debts()
    return jsonify({"total_outstanding": sum(d['remaining_balance'] for d in debts), "debts": debts})

@flask_app.route('/' + BOT_TOKEN, methods=['POST'])
def webhook():
    async def process():
        update = Update.de_json(request.get_json(force=True), bot_app.bot)
        await bot_app.process_update(update)
    asyncio.run(process())
    return 'OK', 200

# ---------- Startup Initialization ----------
async def startup():
    init_db()
    await bot_app.initialize()
    await bot_app.bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")

if __name__ == "__main__":
    asyncio.run(startup())
    port = int(os.environ.get('PORT', 5000))
    flask_app.run(host='0.0.0.0', port=port)
