import os
import sqlite3
import threading
from flask import Flask, render_template_string, jsonify
from telegram.ext import ApplicationBuilder, CommandHandler

app = Flask(__name__)

# --- Flask Routes ---
@app.route('/webapp')
def webapp():
    return render_template_string("""
    <h1>Boshqaruv Paneli</h1>
    <div id="data">Yuklanmoqda...</div>
    <script>
        fetch('/api/dashboard')
            .then(res => res.json())
            .then(data => document.getElementById('data').innerText = "Status: " + data.status)
            .catch(err => document.getElementById('data').innerText = "Xato!");
    </script>
    """)

@app.route('/api/dashboard')
def api_dashboard():
    return jsonify({"status": "Bazaga ulanish tayyor"})

# --- Bot Functions ---
async def start(update, context):
    await update.message.reply_text("Bot ishlamoqda!")

def run_bot():
    token = os.environ.get('BOT_TOKEN')
    if not token: 
        print("Error: BOT_TOKEN not found!")
        return
    
    # 1. Create a new event loop for this thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    # 2. Build the application
    bot_app = ApplicationBuilder().token(token).build()
    bot_app.add_handler(CommandHandler("start", start))
    
    # 3. Run the application
    print("Bot is starting...")
    bot_app.run_polling(drop_pending_updates=True)

# ADD THIS TO THE VERY TOP OF YOUR FILE with your other imports
import asyncio

# --- Main Initialization ---
if __name__ == '__main__':
    # Start Bot in a background thread
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    
    # Start Flask in the main thread
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
