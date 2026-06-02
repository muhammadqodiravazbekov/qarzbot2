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
from telegram.request import HTTPXRequest

# ---------- Configuration ----------
BOT_TOKEN = os.environ.get('BOT_TOKEN')
DATABASE_PATH = os.environ.get('DATABASE_PATH', 'debts.db')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL') # e.g. https://your-app.onrender.com/

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN environment variable is required!")

# ---------- Flask Web Server ----------
flask_app = Flask(__name__)

# ---------- UI Template ----------
MINI_APP_HTML = """
<!DOCTYPE html>
<html lang="uz">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Qarz Kontrol</title>
    <script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
    <script src="https://telegram.org/js/telegram-web-app.js"></script>
</head>
<body class="bg-[#f8fafc] text-[#0f172a] font-sans antialiased pb-24">
    <div class="sticky top-0 z-30 bg-white/80 backdrop-blur-md border-b border-slate-100 px-4 py-3.5 flex items-center justify-between">
        <h1 class="text-base font-bold text-slate-900" id="user-greeting">Boshqaruv Paneli</h1>
    </div>
    <div class="max-w-md mx-auto p-4 space-y-4">
        <div class="grid grid-cols-3 gap-2">
            <div class="bg-white p-3 rounded-2xl shadow-sm border border-slate-100">
                <span class="text-[9px] font-bold text-slate-400 uppercase">Jami Qarz</span>
                <span id="total-amount" class="text-sm font-extrabold text-slate-900 block">0 UZS</span>
            </div>
            <div class="bg-white p-3 rounded-2xl shadow-sm border border-slate-100">
                <span class="text-[9px] font-bold text-slate-400 uppercase">Faol</span>
                <span id="total-debtors" class="text-sm font-extrabold text-indigo-600 block">0 ta</span>
            </div>
            <div class="bg-white p-3 rounded-2xl shadow-sm border border-slate-100">
                <span class="text-[9px] font-bold text-slate-400 uppercase">Yopilgan</span>
                <span id="total-settled" class="text-sm font-extrabold text-emerald-600 block">0 ta</span>
            </div>
        </div>
        <div id="loading-spinner" class="text-center py-10 text-slate-400 text-xs">Ma'lumotlar yuklanmoqda...</div>
        <div id="records-container" class="space-y-2"></div>
    </div>
    <script>
        const tg = window.Telegram.WebApp; tg.ready(); tg.expand();
        async function loadDataStream() {
            try {
                const response = await fetch('/api/dashboard');
                const data = await response.json();
                document.getElementById('total-amount').innerText = new Intl.NumberFormat('uz-UZ').format(data.total_outstanding) + ' UZS';
                document.getElementById('total-debtors').innerText = data.debts.filter(d => d.remaining_balance > 0).length + ' ta';
                document.getElementById('total-settled').innerText = data.debts.filter(d => d.remaining_balance <= 0).length + ' ta';
                document.getElementById('loading-spinner').classList.add('hidden');
                // (Render your records here - simplified for brevity)
                console.log(data);
            } catch (err) { document.getElementById('loading-spinner').innerText = "Xatolik!"; }
        }
        loadDataStream();
    </script>
</body>
</html>
"""

# ---------- Database Helpers ----------
def get_db():
    # timeout=30 prevents "Database is locked" errors
    conn = sqlite3.connect(DATABASE_PATH, timeout=30)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS users (telegram_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, role TEXT)')
    cursor.execute('CREATE TABLE IF NOT EXISTS debts (id INTEGER PRIMARY KEY AUTOINCREMENT, customer_name TEXT, remaining_balance REAL, amount_owed REAL, seller_telegram_id INTEGER, notes TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
    cursor.execute('CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY AUTOINCREMENT, debt_id INTEGER, amount_paid REAL, payment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP, notes TEXT)')
    conn.commit(); conn.close()

# ---------- API Routes ----------
@flask_app.route('/webapp')
def webapp_interface(): return render_template_string(MINI_APP_HTML)

@flask_app.route('/api/dashboard')
def api_dashboard_metrics():
    conn = get_db()
    # Simplified query
    cursor = conn.cursor()
    cursor.execute("SELECT id, customer_name, remaining_balance, amount_owed, notes FROM debts")
    rows = cursor.fetchall()
    debts = [{"id": r[0], "customer_name": r[1], "remaining_balance": r[2], "amount_owed": r[3], "notes": r[4]} for r in rows]
    cursor.execute("SELECT SUM(remaining_balance) FROM debts")
    total = cursor.fetchone()[0] or 0
    conn.close()
    return jsonify({"total_outstanding": total, "debts": debts})

# ---------- Bot Logic ----------
async def start(update, context):
    await update.message.reply_text("Xush kelibsiz! Ilovani ochish uchun pastdagi tugmani bosing.", 
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Mini App", web_app=WebAppInfo(url=WEBHOOK_URL + "/webapp"))]]))

# ---------- Setup ----------
# Webhook Processing
bot_app = Application.builder().token(BOT_TOKEN).build()
bot_app.add_handler(CommandHandler("start", start))

@flask_app.route('/' + BOT_TOKEN, methods=['POST'])
def webhook():
    async def process():
        update = Update.de_json(request.get_json(force=True), bot_app.bot)
        await bot_app.process_update(update)
    asyncio.run(process())
    return 'OK', 200

if __name__ == "__main__":
    init_db()
    # Register webhook with Telegram
    asyncio.run(bot_app.bot.set_webhook(url=WEBHOOK_URL + "/" + BOT_TOKEN))
    # Start Server
    port = int(os.environ.get('PORT', 5000))
    flask_app.run(host='0.0.0.0', port=port)
