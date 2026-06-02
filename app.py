import os
import asyncio
from flask import Flask, request, jsonify
from telegram import Bot, Update
from telegram.ext import Application, CommandHandler

app = Flask(__name__)
BOT_TOKEN = os.environ.get('BOT_TOKEN')
bot = Bot(token=BOT_TOKEN)

# Initialize PTB Application for handling commands
ptb = Application.builder().token(BOT_TOKEN).build()

async def start(update, context):
    await update.message.reply_text("Bot ishlamoqda!")

ptb.add_handler(CommandHandler("start", start))

# --- Webhook Route ---
@app.route('/' + BOT_TOKEN, methods=['POST'])
def webhook():
    # Pass the Telegram update directly to the bot application
    data = request.get_json(force=True)
    update = Update.de_json(data, bot)
    # Run the update processor in a new task
    asyncio.run(ptb.process_update(update))
    return 'OK', 200

@app.route('/webapp')
def webapp():
    return "<h1>Boshqaruv Paneli</h1><p>Status: Ishlamoqda</p>"

@app.route('/api/dashboard')
def api_dashboard():
    return jsonify({"status": "Bazaga ulanish tayyor"})

if __name__ == '__main__':
    # Set the webhook URL on Telegram's side
    webhook_url = f"https://qarzbot2-1.onrender.com/{BOT_TOKEN}"
    asyncio.run(bot.set_webhook(url=webhook_url))
    
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
