import sqlite3
import logging
import asyncio
import os
from flask import Flask, request, jsonify, render_template_string
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from telegram.ext import (
    Application, CommandHandler, ConversationHandler,
    MessageHandler, CallbackQueryHandler, filters
)

# ---------- Configuration ----------
BOT_TOKEN = os.environ.get('BOT_TOKEN')
WEBHOOK_URL = os.environ.get('WEBHOOK_URL') # e.g., https://your-app.onrender.com
DATABASE_PATH = 'debts.db'

# ---------- Flask App ----------
flask_app = Flask(__name__)

# ---------- Database Logic ----------
def init_db():
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    cursor.execute('CREATE TABLE IF NOT EXISTS users (telegram_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT, role TEXT)')
    cursor.execute('CREATE TABLE IF NOT EXISTS debts (id INTEGER PRIMARY KEY AUTOINCREMENT, customer_name TEXT, remaining_balance REAL, amount_owed REAL, seller_telegram_id INTEGER, notes TEXT, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)')
    cursor.execute('CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY AUTOINCREMENT, debt_id INTEGER, amount_paid REAL, payment_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP, notes TEXT)')
    conn.commit()
    conn.close()

# ---------- UI ----------
MINI_APP_HTML = """
<!DOCTYPE html>
<html>
<head><title>Qarz Boshqaruvi</title></head>
<body>
    <h1>Qarz Boshqaruvi</h1>
    <div id="data">Yuklanmoqda...</div>
    <script>
        fetch('/api/dashboard').then(r => r.json()).then(data => {
            document.getElementById('data').innerText = "Jami qarz: " + data.total_outstanding + " UZS";
        });
    </script>
</body>
</html>
"""

# ---------- Bot Handlers ----------
async def start(update, context):
    await update.message.reply_text(
        "Xush kelibsiz! Ilovani ochish:",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Mini App", web_app=WebAppInfo(url=f"{WEBHOOK_URL}/webapp"))]])
    )

# ---------- Bot Setup ----------
# Build the application
bot_app = Application.builder().token(BOT_TOKEN).build()
bot_app.add_handler(CommandHandler("start", start))
# (Add your other handlers here if needed)

# ---------- Routes ----------
@flask_app.route('/webapp')
def webapp_interface():
    return render_template_string(MINI_APP_HTML)

@flask_app.route('/api/dashboard')
def api_dashboard():
    conn = sqlite3.connect(DATABASE_PATH)
    total = conn.execute("SELECT SUM(remaining_balance) FROM debts").fetchone()[0] or 0
    conn.close()
    return jsonify({"total_outstanding": total})

@flask_app.route('/' + BOT_TOKEN, methods=['POST'])
def webhook():
    # Process update synchronously inside the Flask route
    async def process():
        update = Update.de_json(request.get_json(force=True), bot_app.bot)
        await bot_app.process_update(update)
    
    asyncio.run(process())
    return 'OK', 200

# ---------- Startup Initialization ----------
async def startup():
    init_db()
    # THIS INITIALIZES THE APP SO THE RUNTIME ERROR IS GONE
    await bot_app.initialize()
    await bot_app.bot.set_webhook(url=f"{WEBHOOK_URL}/{BOT_TOKEN}")

if __name__ == "__main__":
    # Initialize bot before starting Flask
    asyncio.run(startup())
    
    port = int(os.environ.get('PORT', 5000))
    flask_app.run(host='0.0.0.0', port=port)
