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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (
    Application, CommandHandler, ConversationHandler,
    MessageHandler, CallbackQueryHandler, filters, ContextTypes
)
from telegram.request import HTTPXRequest

# ---------- Configuration ----------
BOT_TOKEN = os.environ.get('BOT_TOKEN')
DATABASE_PATH = os.environ.get('DATABASE_PATH', 'debts.db')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL')

if not BOT_TOKEN or not WEBHOOK_URL:
    raise ValueError("BOT_TOKEN and WEBHOOK_URL environment variables are required!")

flask_app = Flask(__name__)

# ---------- UI ----------
MINI_APP_HTML = """
<!DOCTYPE html>
<html lang="uz">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Qarz Kontrol</title>
    <script src="https://cdn.jsdelivr.net/npm/@tailwindcss/browser@4"></script>
</head>
<body class="bg-slate-50 p-4">
    <div class="max-w-md mx-auto">
        <h1 class="text-xl font-bold mb-4">Qarz Boshqaruvi</h1>
        <div id="loading" class="text-center p-10 text-slate-500">Yuklanmoqda...</div>
        <div id="data"></div>
    </div>
    <script>
        fetch('/api/dashboard').then(r => r.json()).then(data => {
            document.getElementById('loading').classList.add('hidden');
            document.getElementById('data').innerText = "Jami qarz: " + data.total_outstanding + " UZS";
        });
    </script>
</body>
</html>
"""

# ---------- Database ----------
def get_db():
    # Timeout fixes the "database is locked" error
    return sqlite3.connect(DATABASE_PATH, timeout=30)

def init_db():
    conn = get_db()
    conn.execute('CREATE TABLE IF NOT EXISTS users (telegram_id INTEGER PRIMARY KEY, username TEXT, role TEXT)')
    conn.execute('CREATE TABLE IF NOT EXISTS debts (id INTEGER PRIMARY KEY AUTOINCREMENT, customer_name TEXT, remaining_balance REAL)')
    conn.commit(); conn.close()

# ---------- Bot Logic ----------
bot_app = Application.builder().token(BOT_TOKEN).build()

async def start(update, context):
    url = f"{WEBHOOK_URL}/webapp"
    await update.message.reply_text(
        "Tizim faol! Ilovani ochish:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Mini App", web_app=WebAppInfo(url=url))]])
    )

bot_app.add_handler(CommandHandler("start", start))

# ---------- Routes ----------
@flask_app.route('/webapp')
def webapp(): return render_template_string(MINI_APP_HTML)

@flask_app.route('/api/dashboard')
def dashboard():
    conn = get_db()
    total = conn.execute("SELECT SUM(remaining_balance) FROM debts").fetchone()[0] or 0
    conn.close()
    return jsonify({"total_outstanding": total})

@flask_app.route('/' + BOT_TOKEN, methods=['POST'])
def webhook():
    # Receive update from Telegram
    async def process():
        update = Update.de_json(request.get_json(force=True), bot_app.bot)
        await bot_app.process_update(update)
    asyncio.run(process())
    return 'OK', 200

# ---------- Startup ----------
if __name__ == "__main__":
    init_db()
    # Set the webhook to tell Telegram where to send messages
    asyncio.run(bot_app.bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}"))
    port = int(os.environ.get('PORT', 5000))
    flask_app.run(host='0.0.0.0', port=port)
