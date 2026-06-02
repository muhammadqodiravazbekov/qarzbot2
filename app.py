import os
import sqlite3
import threading
import asyncio
from flask import Flask, render_template_string, jsonify
from telegram.ext import ApplicationBuilder, CommandHandler
from werkzeug.middleware.proxy_fix import ProxyFix

# --- Flask Setup ---
app = Flask(__name__)
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# --- Bot Command ---
async def start(update, context):
    await update.message.reply_text("Assalomu alaykum! Bot ishlamoqda.")

# --- API Routes ---
@app.route('/webapp')
def webapp():
    return render_template_string("""
    <!DOCTYPE html>
    <html>
    <body>
        <h1>Boshqaruv Paneli</h1>
        <div id="data">Yuklanmoqda...</div>
        <script>
            fetch('/api/dashboard')
                .then(res => res.json())
                .then(data => document.getElementById('data').innerText = "Status: " + data.status)
                .catch(err => document.getElementById('data').innerText = "Xato!");
        </script>
    </body>
    </html>
    """)

@app.route('/api/dashboard')
def api_dashboard():
    return jsonify({"status": "Bazaga ulanish tayyor"})

# --- Bot Thread Function ---
def run_bot():
    token = os.environ.get('BOT_TOKEN')
    if not token:
        print("CRITICAL: BOT_TOKEN is missing!")
        return
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    bot_app = ApplicationBuilder().token(token).build()
    bot_app.add_handler(CommandHandler("start", start))
    
    print("Bot polling started...")
    bot_app.run_polling(drop_pending_updates=True)

# --- Main Entry Point ---
if __name__ == '__main__':
    # Start bot in a background thread
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    
    # Start Flask in the main thread
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
