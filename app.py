import os
import asyncio
from flask import Flask, request, jsonify
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler

app = Flask(__name__)
BOT_TOKEN = os.environ.get('BOT_TOKEN')

# 1. Build the application
ptb = Application.builder().token(BOT_TOKEN).build()

async def start(update, context):
    await update.message.reply_text("Assalomu alaykum! Bot ishlamoqda.")

ptb.add_handler(CommandHandler("start", start))

# 2. Initialize the application once globally
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
loop.run_until_complete(ptb.initialize())

# --- Webhook Route ---
@app.route('/' + BOT_TOKEN, methods=['POST'])
def webhook():
    data = request.get_json(force=True)
    update = Update.de_json(data, ptb.bot)
    
    # 3. Process the update using the already initialized app
    loop.run_until_complete(ptb.process_update(update))
    return 'OK', 200

@app.route('/api/dashboard')
def api_dashboard():
    return jsonify({"status": "Bazaga ulanish tayyor"})

if __name__ == '__main__':
    # Set the webhook
    webhook_url = f"https://qarzbot2-1.onrender.com/{BOT_TOKEN}"
    loop.run_until_complete(ptb.bot.set_webhook(url=webhook_url))
    
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
