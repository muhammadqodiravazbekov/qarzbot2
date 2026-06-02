import os
import sqlite3
from flask import Flask, render_template_string, jsonify
from telegram.ext import ApplicationBuilder, CommandHandler

app = Flask(__name__)
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# --- 1. Bot Initialization ---
BOT_TOKEN = os.environ.get('BOT_TOKEN')

async def start(update, context):
    await update.message.reply_text("Bot ishlamoqda! Ilovani ochish uchun pastdagi tugmani bosing.")

# --- 2. Routes ---
@app.route('/')
def home():
    return "Bot and WebApp are online!"

@app.route('/webapp')
def webapp():
    # This is your dashboard. I added a simple 'Success' check here.
    return render_template_string("""
    <h1>Boshqaruv Paneli</h1>
    <div id="data">Yuklanmoqda...</div>
    <script>
        fetch('/api/dashboard')
            .then(res => res.json())
            .then(data => document.getElementById('data').innerText = "Muvaffaqiyatli: " + data.status)
            .catch(err => document.getElementById('data').innerText = "Xatolik yuz berdi!");
    </script>
    """)

@app.route('/api/dashboard')
def api_dashboard():
    # Simple check to make sure the API returns JSON
    return jsonify({"status": "ishlayapti", "data_count": 0})

# --- 3. Run Both ---
if __name__ == '__main__':
    # Start the bot in the background
    if BOT_TOKEN:
        bot_app = ApplicationBuilder().token(BOT_TOKEN).build()
        bot_app.add_handler(CommandHandler("start", start))
        bot_app.run_polling(drop_pending_updates=True)
    
    # Start Flask
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
