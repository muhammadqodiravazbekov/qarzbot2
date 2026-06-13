import os, re, io, csv, logging, asyncio, threading, unicodedata, psycopg2
from psycopg2.pool import ThreadedConnectionPool
from psycopg2.extras import RealDictCursor
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (Application, CommandHandler, ConversationHandler,
    MessageHandler, CallbackQueryHandler, filters, ContextTypes)
from telegram.request import HTTPXRequest

# ══════════════════════════════════════════
# КОНФИГУРАЦИЯ
# ══════════════════════════════════════════
BOT_TOKEN    = os.environ.get('BOT_TOKEN')
DATABASE_URL = os.environ.get('DATABASE_URL')
BACKUP_CHAT  = os.environ.get('BACKUP_GROUP_ID')
BACKUP_TOPIC = os.environ.get('BACKUP_TOPIC_ID')

if not BOT_TOKEN or not DATABASE_URL:
    raise ValueError("BOT_TOKEN ёки DATABASE_URL топилмади!")

# Psycopg2 да "postgres://" ни қўллаб-қувватламайди, уни "postgresql://" га ўзгартирамиз
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

UZB_TZ = timezone(timedelta(hours=5))
def now(): return datetime.now(UZB_TZ)

# ══════════════════════════════════════════
# FLASK (Render учун)
# ══════════════════════════════════════════
flask_app = Flask(__name__)

@flask_app.route('/')
@flask_app.route('/health')
def health():
    return jsonify({"status": "ishlayapti"}), 200

# ══════════════════════════════════════════
# МАЪЛУМОТЛАР БАЗАСИ
# ══════════════════════════════════════════
pool = ThreadedConnectionPool(1, 20, dsn=DATABASE_URL)

@contextmanager
def db(commit=False):
    conn = pool.getconn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            yield cur
        if commit: 
            conn.commit()
        else:
            conn.rollback()  # Транзакция ҳолатини тозалаш ва "idle in transaction" ни олдини олиш
    except Exception as e:
        conn.rollback()
        logging.error(f"DB хатоси: {e}")
        raise
    finally:
        pool.putconn(conn)

def init_db():
    with db(commit=True) as c:
        # МАЪЛУМОТЛАР ХАВФСИЗЛИГИ УЧУН DROP TABLE БУЙРУҚЛАРИ ОЛИБ ТАШЛАНДИ
        
        # Жадвалларни хавфсиз яратиш (IF NOT EXISTS)
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                telegram_id BIGINT PRIMARY KEY,
                username    TEXT,
                first_name  TEXT,
                role        TEXT CHECK(role IN ('admin','seller','viewer')) NOT NULL,
                created_at  TIMESTAMPTZ DEFAULT NOW()
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS customers (
                id         SERIAL PRIMARY KEY,
                name       TEXT NOT NULL,
                name_norm  TEXT,
                balance    REAL NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            )""")
        c.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id          SERIAL PRIMARY KEY,
                customer_id INTEGER REFERENCES customers(id) ON DELETE CASCADE,
                t_type      TEXT CHECK(t_type IN ('debt','payment')) NOT NULL,
                amount      REAL NOT NULL,
                note        TEXT,
                by_username TEXT NOT NULL DEFAULT '@unknown',
                created_at  TIMESTAMPTZ DEFAULT NOW()
            )""")
        c.execute("CREATE INDEX IF NOT EXISTS idx_cust_norm ON customers(name_norm)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_tx_cust  ON transactions(customer_id)")

# Глобал қисмдаги эрта чақирув олиб ташланди, у фақат __main__ ичида бажарилади.

# ══════════════════════════════════════════
# ЁРДАМЧИ ФУНКЦИЯЛАР
# ══════════════════════════════════════════
def norm(text: str) -> str:
    if not text: return ""
    cyr = {'а':'a','б':'b','в':'v','г':'g','д':'d','е':'e','ё':'yo','ж':'j','з':'z',
           'и':'i','й':'y','к':'k','л':'l','м':'m','н':'n','о':'o','п':'p','р':'r',
           'с':'s','т':'t','у':'u','ф':'f','х':'x','ц':'ts','ч':'ch','ш':'sh',
           'щ':'sh','ъ':'','ы':'i','ь':'','э':'e','ю':'yu','я':'ya',
           'ў':'o','қ':'k','ғ':'g','ҳ':'x'}
    t = text.lower()
    for k, v in cyr.items(): t = t.replace(k, v)
    t = unicodedata.normalize('NFKD', t).encode('ASCII','ignore').decode()
    return re.sub(r'[^a-z0-9]', '', t)

def uname(user) -> str:
    return f"@{user.username}" if user.username else f"@id{user.id}"

def fmt(n: float) -> str:
    return f"{n:,.0f}".replace(',', ' ')

# ══════════════════════════════════════════
# DB ФУНКЦИЯЛАРИ
# ══════════════════════════════════════════
def db_get_user(tid: int):
    with db() as c:
        c.execute("SELECT * FROM users WHERE telegram_id=%s", (tid,))
        return c.fetchone()

def db_all_users():
    with db() as c:
        c.execute("SELECT * FROM users ORDER BY created_at")
        return c.fetchall()

def db_create_user(tid, username, first_name, role) -> bool:
    try:
        with db(commit=True) as c:
            c.execute("INSERT INTO users(telegram_id,username,first_name,role) VALUES(%s,%s,%s,%s)",
                      (tid, username, first_name, role))
        return True
    except psycopg2.IntegrityError: return False

def db_delete_user(tid) -> bool:
    with db(commit=True) as c:
        c.execute("DELETE FROM users WHERE telegram_id=%s", (tid,))
        return c.rowcount > 0

def db_search(query: str):
    nq = norm(query)
    with db() as c:
        c.execute("""
            SELECT * FROM customers
            WHERE (name ILIKE %s OR name_norm LIKE %s) AND balance > 0
            ORDER BY updated_at DESC LIMIT 8
        """, (f"%{query}%", f"%{nq}%"))
        return c.fetchall()

def db_get_cust(cid: int):
    with db() as c:
        c.execute("SELECT * FROM customers WHERE id=%s", (cid,))
        return c.fetchone()

def db_add_customer(name: str, amount: float, note: str, username: str) -> int:
    n = norm(name)
    with db(commit=True) as c:
        c.execute("INSERT INTO customers(name,name_norm,balance) VALUES(%s,%s,%s) RETURNING id",
                  (name, n, amount))
        cid = c.fetchone()['id']
        c.execute("INSERT INTO transactions(customer_id,t_type,amount,note,by_username) VALUES(%s,'debt',%s,%s,%s)",
                  (cid, amount, note, username))
        return cid

def db_add_debt(cid: int, amount: float, note: str, username: str) -> float:
    with db(commit=True) as c:
        c.execute("UPDATE customers SET balance=balance+%s, updated_at=NOW() WHERE id=%s RETURNING balance",
                  (amount, cid))
        new_bal = c.fetchone()['balance']
        c.execute("INSERT INTO transactions(customer_id,t_type,amount,note,by_username) VALUES(%s,'debt',%s,%s,%s)",
                  (cid, amount, note, username))
        return new_bal

def db_add_payment(cid: int, amount: float, note: str, username: str):
    with db(commit=True) as c:
        c.execute("UPDATE customers SET balance=balance-%s, updated_at=NOW() WHERE id=%s RETURNING balance",
                  (amount, cid))
        new_bal = c.fetchone()['balance']
        c.execute("INSERT INTO transactions(customer_id,t_type,amount,note,by_username) VALUES(%s,'payment',%s,%s,%s)",
                  (cid, amount, note, username))
        return new_bal

def db_history(cid: int, limit=20):
    with db() as c:
        c.execute("""
            SELECT * FROM transactions WHERE customer_id=%s
            ORDER BY created_at DESC LIMIT %s
        """, (cid, limit))
        return c.fetchall()

def db_stats():
    with db() as c:
        c.execute("SELECT COALESCE(SUM(balance),0) as total FROM customers WHERE balance>0")
        total = c.fetchone()['total']
        c.execute("SELECT COALESCE(SUM(amount),0) as d FROM transactions WHERE t_type='debt' AND created_at>=CURRENT_DATE")
        today_d = c.fetchone()['d']
        c.execute("SELECT COALESCE(SUM(amount),0) as p FROM transactions WHERE t_type='payment' AND created_at>=CURRENT_DATE")
        today_p = c.fetchone()['p']
        c.execute("""
            SELECT name, balance FROM customers
            WHERE balance>0 ORDER BY balance DESC LIMIT 5
        """)
        top = c.fetchall()
        c.execute("SELECT COUNT(*) as cnt FROM customers WHERE balance>0")
        active = c.fetchone()['cnt']
        return total, today_d, today_p, top, active

def db_all_active():
    with db() as c:
        c.execute("""
            SELECT c.*, STRING_AGG(t.amount::text,'|' ORDER BY t.created_at) as parts
            FROM customers c
            LEFT JOIN transactions t ON c.id=t.customer_id AND t.t_type='debt'
            WHERE c.balance>0
            GROUP BY c.id ORDER BY c.balance DESC
        """)
        return c.fetchall()

def db_jami():
    with db() as c:
        c.execute("SELECT name, balance FROM customers WHERE balance>0 ORDER BY balance DESC")
        rows = c.fetchall()
        c.execute("SELECT COALESCE(SUM(balance),0) as t FROM customers WHERE balance>0")
        total = c.fetchone()['t']
        return rows, total

# ══════════════════════════════════════════
# РОЛЬ ТЕКШИРУВИ
# ══════════════════════════════════════════
async def check_seller(update: Update) -> bool:
    u = await asyncio.to_thread(db_get_user, update.effective_user.id)
    if u and u['role'] in ('admin','seller'): return True
    msg = "⛔ Бу амал фақат сотувчилар учун."
    if update.message: await update.message.reply_text(msg)
    elif update.callback_query: await update.callback_query.answer(msg, show_alert=True)
    return False

async def check_admin(update: Update) -> bool:
    u = await asyncio.to_thread(db_get_user, update.effective_user.id)
    if u and u['role'] == 'admin': return True
    msg = "⛔ Бу амал фақат админлар учун."
    if update.message: await update.message.reply_text(msg)
    elif update.callback_query: await update.callback_query.answer(msg, show_alert=True)
    return False

# ══════════════════════════════════════════
# ГУРУҲГА ХАБАР (HTML формати хавфсизроқ)
# ══════════════════════════════════════════
async def notify(ctx, text: str):
    if not BACKUP_CHAT: return
    try:
        kwargs = {"chat_id": int(BACKUP_CHAT), "text": text, "parse_mode": "HTML"}
        if BACKUP_TOPIC: kwargs["message_thread_id"] = int(BACKUP_TOPIC)
        await ctx.bot.send_message(**kwargs)
    except Exception as e:
        logging.error(f"Notify хатоси: {e}")

# ══════════════════════════════════════════
# КЛАВИАТУРАЛАР
# ══════════════════════════════════════════
def kb_main(role: str) -> ReplyKeyboardMarkup:
    if role in ('admin','seller'):
        rows = [
            [KeyboardButton("➕ Қарз қўшиш"), KeyboardButton("💰 Тўлов қабул қилиш")],
            [KeyboardButton("🔍 Қидириш"),    KeyboardButton("📊 Статистика")],
        ]
        if role == 'admin':
            rows.append([KeyboardButton("👥 Фойдаланувчилар"), KeyboardButton("📢 Бэкап")])
    else:
        rows = [
            [KeyboardButton("🔍 Қидириш"), KeyboardButton("📊 Статистика")],
        ]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("❌ Бекор", callback_data="cancel")]])

def kb_debt_type() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🆕 Янги мижоз",      callback_data="debt_new")],
        [InlineKeyboardButton("👤 Мавжуд мижозга",  callback_data="debt_exist")],
        [InlineKeyboardButton("❌ Бекор",            callback_data="cancel")],
    ])

def kb_note_skip() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➡️ Изоҳсиз давом", callback_data="skip_note")],
        [InlineKeyboardButton("❌ Бекор",           callback_data="cancel")],
    ])

# ══════════════════════════════════════════
# СТЕЙТЛАР
# ══════════════════════════════════════════
(
    S_DEBT_TYPE,
    S_NEW_NAME, S_NEW_AMT, S_NEW_NOTE,
    S_EX_SEARCH, S_EX_AMT, S_EX_NOTE,
    S_PAY_SEARCH, S_PAY_AMT,
    S_SRCH,
    S_USR_ACT, S_USR_ID, S_USR_ROLE
) = range(13)

# ══════════════════════════════════════════
# /start (HTML га ўтказилди)
# ══════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    dbu = await asyncio.to_thread(db_get_user, u.id)
    if not dbu:
        all_u = await asyncio.to_thread(db_all_users)
        if not all_u:
            await asyncio.to_thread(db_create_user, u.id, u.username or "", u.first_name or "", "admin")
            dbu = {'role': 'admin'}
            await update.message.reply_text(
                "✅ Сиз <b>АДМИН</b> этиб тайинландингиз.\nТизим тайёр.", parse_mode="HTML",
                reply_markup=kb_main('admin'))
        else:
            await update.message.reply_text("❌ Кириш тақиқланган. Админга мурожаат қилинг.")
        return
    role = dbu['role']
    emoji = "👑" if role=='admin' else "🛒" if role=='seller' else "👁"
    await update.message.reply_text(
        f"{emoji} Хуш келибсиз, {uname(u)}!\nРолингиз: <b>{role.upper()}</b>", parse_mode="HTML",
        reply_markup=kb_main(role))

# ══════════════════════════════════════════
# /jami (HTML га ўтказилди)
# ══════════════════════════════════════════
async def cmd_jami(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    rows, total = await asyncio.to_thread(db_jami)
    if not rows:
        await update.message.reply_text("📭 Фаол қарздорлар йўқ.")
        return
    lines = []
    for r in rows[:30]:
        lines.append(f"• {r['name']}: <b>{fmt(r['balance'])}</b> сўм")
    text = "📋 <b>Қарздорлар рўйхати:</b>\n\n" + "\n".join(lines)
    if len(rows) > 30:
        text += f"\n\n<i>...ва яна {len(rows)-30} та</i>"
    text += f"\n\n💰 <b>Жами: {fmt(total)} сўм</b>"
    await update.message.reply_text(text, parse_mode="HTML")

# ══════════════════════════════════════════
# СТАТИСТИКА (HTML га ўтказилди)
# ══════════════════════════════════════════
async def handle_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    total, td, tp, top, active = await asyncio.to_thread(db_stats)
    msg = (f"📊 <b>Статистика</b>\n\n"
           f"💰 Умумий қарз: <b>{fmt(total)} сўм</b>\n"
           f"👥 Фаол қарздорлар: <b>{active}</b>\n\n"
           f"📅 <b>Бугун:</b>\n"
           f"➕ Берилган қарзлар: {fmt(td)} сўм\n"
           f"✅ Қабул қилинган тўловлар: {fmt(tp)} сўм\n\n"
           f"🔥 <b>Энг катта қарздорлар:</b>\n")
    for i, r in enumerate(top, 1):
        msg += f"{i}. {r['name']} — {fmt(r['balance'])} сўм\n"
    await update.message.reply_text(msg, parse_mode="HTML")

# ══════════════════════════════════════════
# БЭКАП (HTML га ўтказилди)
# ══════════════════════════════════════════
async def handle_backup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update): return
    rows = await asyncio.to_thread(db_all_active)
    if not rows or not BACKUP_CHAT:
        await update.message.reply_text("❌ Маълумот йўқ ёки гуруҳ созланмаган.")
        return
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Ism", "Balans (so'm)", "Sana"])
    for r in rows:
        w.writerow([r['name'], f"{r['balance']:.0f}", str(r['updated_at'])[:10]])
    out.seek(0)
    fname = f"Qarzlar_{now().strftime('%d_%m_%Y')}.csv"
    try:
        kwargs = {
            "chat_id": int(BACKUP_CHAT),
            "document": io.BytesIO(out.getvalue().encode('utf-8-sig')),
            "filename": fname,
            "caption": f"📢 <b>Бэкап</b>\n👤 {uname(update.effective_user)}\n📅 {now().strftime('%d.%m.%Y %H:%M')}",
            "parse_mode": "HTML"
        }
        if BACKUP_TOPIC: kwargs["message_thread_id"] = int(BACKUP_TOPIC)
        await ctx.bot.send_document(**kwargs)
        await update.message.reply_text("✅ CSV бэкап гуруҳга юборилди.")
    except Exception as e:
        logging.error(e)
        await update.message.reply_text("❌ Бэкап юборишда хатолик.")

# ══════════════════════════════════════════
# ҚАРЗ ҚЎШИШ ОҚИМИ
# ══════════════════════════════════════════
async def debt_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_seller(update): return ConversationHandler.END
    ctx.user_data.clear()
    await update.message.reply_text("Қарз турини танланг:", reply_markup=kb_debt_type())
    return S_DEBT_TYPE

async def debt_type_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "debt_new":
        await q.edit_message_text("👤 Янги мижоз исмини ёзинг:", reply_markup=kb_cancel())
        return S_NEW_NAME
    elif q.data == "debt_exist":
        await q.edit_message_text("🔍 Мижоз исмини қидиринг:", reply_markup=kb_cancel())
        return S_EX_SEARCH
    return ConversationHandler.END

# --- Янги мижоз ---
async def new_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data['name'] = update.message.text.strip()
    await update.message.reply_text("💰 Қарз суммасини киритинг:", reply_markup=kb_cancel())
    return S_NEW_AMT

async def new_amt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        amt = float(re.sub(r'[^\d.]', '', update.message.text.replace(',','.')))
        if amt <= 0: raise ValueError()
        ctx.user_data['amt'] = amt
        await update.message.reply_text(
            "📝 Изоҳ ёзинг (нима сотилди?):", reply_markup=kb_note_skip())
        return S_NEW_NOTE
    except:
        await update.message.reply_text("❌ Тўғри сон киритинг:", reply_markup=kb_cancel())
        return S_NEW_AMT

async def new_note_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    return await _save_new(update, ctx, update.message.text.strip())

async def new_note_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    return await _save_new(update, ctx, "")

async def _save_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE, note: str):
    name, amt = ctx.user_data['name'], ctx.user_data['amt']
    who = uname(update.effective_user)
    await asyncio.to_thread(db_add_customer, name, amt, note, who)
    text = (f"✅ <b>Янги мижоз қўшилди!</b>\n\n"
            f"👤 {name}\n💰 {fmt(amt)} сўм\n"
            f"📝 {note or '—'}\n💼 {who}")
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML")
    else:
        await update.message.reply_text(text, parse_mode="HTML")
    await notify(ctx, text)
    ctx.user_data.clear()
    return ConversationHandler.END

# --- Мавжуд мижоз ---
async def ex_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.message.text.strip()
    results = await asyncio.to_thread(db_search, q)
    if not results:
        await update.message.reply_text("❌ Топилмади. Бошқача ёзиб кўринг:", reply_markup=kb_cancel())
        return S_EX_SEARCH
    btns = [[InlineKeyboardButton(
        f"{r['name']} — {fmt(r['balance'])} сўм", callback_data=f"ex_{r['id']}"
    )] for r in results]
    btns.append([InlineKeyboardButton("❌ Бекор", callback_data="cancel")])
    await update.message.reply_text("👇 Мижозни танланг:", reply_markup=InlineKeyboardMarkup(btns))
    return S_EX_SEARCH

async def ex_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cid = int(q.data.split("_")[1])
    ctx.user_data['cid'] = cid
    cust = await asyncio.to_thread(db_get_cust, cid)
    await q.edit_message_text(
        f"👤 <b>{cust['name']}</b>\n📊 Жорий қарз: {fmt(cust['balance'])} сўм\n\n💰 Қўшиладиган сумма:",
        parse_mode="HTML", reply_markup=kb_cancel())
    return S_EX_AMT

async def ex_amt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        amt = float(re.sub(r'[^\d.]', '', update.message.text.replace(',','.')))
        if amt <= 0: raise ValueError()
        ctx.user_data['amt'] = amt
        await update.message.reply_text("📝 Изоҳ ёзинг (нима олинди?):", reply_markup=kb_note_skip())
        return S_EX_NOTE
    except:
        await update.message.reply_text("❌ Тўғри сон киритинг:", reply_markup=kb_cancel())
        return S_EX_AMT

async def ex_note_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    return await _save_ex(update, ctx, update.message.text.strip())

async def ex_note_skip(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    return await _save_ex(update, ctx, "")

async def _save_ex(update: Update, ctx: ContextTypes.DEFAULT_TYPE, note: str):
    cid, amt = ctx.user_data['cid'], ctx.user_data['amt']
    who = uname(update.effective_user)
    cust = await asyncio.to_thread(db_get_cust, cid)
    new_bal = await asyncio.to_thread(db_add_debt, cid, amt, note, who)
    text = (f"✅ <b>Қарз қўшилди!</b>\n\n"
            f"👤 {cust['name']}\n"
            f"➕ {fmt(amt)} сўм\n"
            f"📊 Янги баланс: {fmt(new_bal)} сўм\n"
            f"📝 {note or '—'}\n💼 {who}")
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML")
    else:
        await update.message.reply_text(text, parse_mode="HTML")
    await notify(ctx, text)
    ctx.user_data.clear()
    return ConversationHandler.END

# ══════════════════════════════════════════
# ТЎЛОВ ОҚИМИ
# ══════════════════════════════════════════
async def pay_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_seller(update): return ConversationHandler.END
    ctx.user_data.clear()
    await update.message.reply_text("🔍 Тўлов қилаётган мижоз исмини ёзинг:", reply_markup=kb_cancel())
    return S_PAY_SEARCH

async def pay_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.message.text.strip()
    results = await asyncio.to_thread(db_search, q)
    if not results:
        await update.message.reply_text("❌ Топилмади:", reply_markup=kb_cancel())
        return S_PAY_SEARCH
    btns = [[InlineKeyboardButton(
        f"{r['name']} — {fmt(r['balance'])} сўм", callback_data=f"pay_{r['id']}"
    )] for r in results]
    btns.append([InlineKeyboardButton("❌ Бекор", callback_data="cancel")])
    await update.message.reply_text("👇 Мижозни танланг:", reply_markup=InlineKeyboardMarkup(btns))
    return S_PAY_SEARCH

async def pay_select(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cid = int(q.data.split("_")[1])
    ctx.user_data['cid'] = cid
    cust = await asyncio.to_thread(db_get_cust, cid)
    btns = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💵 Тўлиқ ёпиш ({fmt(cust['balance'])} сўм)", callback_data=f"payfull_{cust['balance']}")],
        [InlineKeyboardButton("❌ Бекор", callback_data="cancel")]
    ])
    await q.edit_message_text(
        f"👤 <b>{cust['name']}</b>\n💸 Қарзи: {fmt(cust['balance'])} сўм\n\n💵 Тўлов суммасини киритинг:",
        parse_mode="HTML", reply_markup=btns)
    return S_PAY_AMT

async def pay_amt_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        amt = float(re.sub(r'[^\d.]', '', update.message.text.replace(',','.')))
        if amt <= 0: raise ValueError()
        return await _save_pay(update, ctx, amt)
    except:
        await update.message.reply_text("❌ Тўғри сон киритинг:", reply_markup=kb_cancel())
        return S_PAY_AMT

async def pay_full_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    amt = float(update.callback_query.data.split("_")[1])
    return await _save_pay(update, ctx, amt)

async def _save_pay(update: Update, ctx: ContextTypes.DEFAULT_TYPE, amt: float):
    cid = ctx.user_data['cid']
    who = uname(update.effective_user)
    cust = await asyncio.to_thread(db_get_cust, cid)
    if amt > cust['balance'] + 0.01:
        msg = f"❌ Тўлов суммаси ({fmt(amt)}) қарздан ({fmt(cust['balance'])}) катта!"
        if update.callback_query: await update.callback_query.edit_message_text(msg)
        else: await update.message.reply_text(msg)
        return S_PAY_AMT
    new_bal = await asyncio.to_thread(db_add_payment, cid, amt, "", who)
    if new_bal <= 0:
        text = f"🎉 <b>Қарз тўлиқ ёпилди!</b>\n\n👤 {cust['name']}\n✅ {fmt(amt)} сўм тўланди\n💼 {who}"
    else:
        text = f"✅ <b>Тўлов қабул қилинди!</b>\n\n👤 {cust['name']}\n💵 {fmt(amt)} сўм\n📊 Қолган қарз: {fmt(new_bal)} сўм\n💼 {who}"
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML")
    else:
        await update.message.reply_text(text, parse_mode="HTML")
    await notify(ctx, text)
    ctx.user_data.clear()
    return ConversationHandler.END

# ══════════════════════════════════════════
# ҚИДИРИШ ОҚИМИ
# ══════════════════════════════════════════
async def search_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("🔍 Мижоз исмини ёзинг:", reply_markup=kb_cancel())
    return S_SRCH

async def search_query(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    results = await asyncio.to_thread(db_search, update.message.text.strip())
    if not results:
        await update.message.reply_text("❌ Топилмади.")
        return ConversationHandler.END
    for r in results[:5]:
        hist = await asyncio.to_thread(db_history, r['id'], 10)
        msg = f"👤 <b>{r['name']}</b>\n📊 Қарз: <b>{fmt(r['balance'])} сўм</b>\n\n📜 <b>Тарих:</b>\n"
        msg += "─────────────────\n"
        if not hist:
            msg += "<i>Тарих бўш</i>\n"
        else:
            for h in hist:
                d = h['created_at'].strftime('%d.%m %H:%M') if h['created_at'] else ''
                sign = "🔴" if h['t_type']=='debt' else "🟢"
                note = f" • {h['note']}" if h['note'] else ""
                msg += f"{sign} {fmt(h['amount'])} сўм{note}\n   {h['by_username']} | {d}\n"
        await update.message.reply_text(msg, parse_mode="HTML")
    ctx.user_data.clear()
    return ConversationHandler.END

# ══════════════════════════════════════════
# ФОЙДАЛАНУВЧИЛАР БОШҚАРУВИ
# ══════════════════════════════════════════
async def users_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await check_admin(update): return ConversationHandler.END
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Қўшиш",    callback_data="u_add")],
        [InlineKeyboardButton("❌ Ўчириш",   callback_data="u_del")],
        [InlineKeyboardButton("📋 Рўйхат",   callback_data="u_list")],
        [InlineKeyboardButton("❌ Бекор",    callback_data="cancel")],
    ])
    await update.message.reply_text("👥 <b>Фойдаланувчилар:</b>", parse_mode="HTML", reply_markup=kb)
    return S_USR_ACT

async def users_act_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "u_list":
        users = await asyncio.to_thread(db_all_users)
        msg = "👥 <b>Рўйхат:</b>\n\n"
        for u in users:
            role_e = "👑" if u['role']=='admin' else "🛒" if u['role']=='seller' else "👁"
            msg += f"{role_e} @{u['username'] or 'noname'} | <code>{u['telegram_id']}</code> | <b>{u['role']}</b>\n"
        await q.edit_message_text(msg, parse_mode="HTML", reply_markup=kb_cancel())
        return S_USR_ACT
    elif q.data == "u_add":
        ctx.user_data['u_act'] = 'add'
        await q.edit_message_text("➕ Янги фойдаланувчи Telegram ID:", reply_markup=kb_cancel())
        return S_USR_ID
    elif q.data == "u_del":
        ctx.user_data['u_act'] = 'del'
        await q.edit_message_text("❌ Ўчириладиган Telegram ID:", reply_markup=kb_cancel())
        return S_USR_ID
    return S_USR_ACT

async def users_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        tid = int(update.message.text.strip())
        act = ctx.user_data.get('u_act')
        if act == 'del':
            ok = await asyncio.to_thread(db_delete_user, tid)
            await update.message.reply_text("✅ Ўчирилди." if ok else "❌ Топилмади.")
            ctx.user_data.clear()
            return ConversationHandler.END
        elif act == 'add':
            ctx.user_data['u_tid'] = tid
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("🛒 Сотувчи",  callback_data="r_seller")],
                [InlineKeyboardButton("👁 Кўрувчи",  callback_data="r_viewer")],
                [InlineKeyboardButton("👑 Админ",    callback_data="r_admin")],
            ])
            await update.message.reply_text("Роль танланг:", reply_markup=kb)
            return S_USR_ROLE
    except ValueError:
        await update.message.reply_text("❌ Фақат рақам киритинг:", reply_markup=kb_cancel())
        return S_USR_ID

async def users_role_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    role = q.data.split("_")[1]
    tid = ctx.user_data['u_tid']
    try:
        chat = await ctx.bot.get_chat(tid)
        uname_str = chat.username or ""
        fname = chat.first_name or "Foydalanuvchi"
    except:
        uname_str, fname = "", "Foydalanuvchi"
    if await asyncio.to_thread(db_create_user, tid, uname_str, fname, role):
        await q.edit_message_text(f"✅ @{uname_str or tid} — <b>{role}</b> роли билан қўшилди.", parse_mode="HTML")
    else:
        await q.edit_message_text("❌ Бу фойдаланувчи аллақачон мавжуд.")
    ctx.user_data.clear()
    return ConversationHandler.END

# ══════════════════════════════════════════
# БЕКОР ҚИЛИШ
# ══════════════════════════════════════════
async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text("🚫 Бекор қилинди.")
    else:
        await update.message.reply_text("🚫 Бекор қилинди.")
    return ConversationHandler.END

# ══════════════════════════════════════════
# БОТ ИШГА ТУШИРИШ
# ══════════════════════════════════════════
def run_bot():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    app = (Application.builder()
           .token(BOT_TOKEN)
           .request(HTTPXRequest(connect_timeout=30, read_timeout=30))
           .build())

    # Барча матн филтри (навигация тугмаларини ўтказиб юбориш учун)
    nav = filters.Regex(r"^(➕ Қарз қўшиш|💰 Тўлов қабул қилиш|🔍 Қидириш|📊 Статистика|👥 Фойдаланувчилар|📢 Бэкап)$")
    txt = filters.TEXT & ~filters.COMMAND & ~nav

    conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex("^➕ Қарз қўшиш$"),          debt_start),
            MessageHandler(filters.Regex("^💰 Тўлов қабул қилиш$"),   pay_start),
            MessageHandler(filters.Regex("^🔍 Қидириш$"),             search_start),
            MessageHandler(filters.Regex("^👥 Фойдаланувчилар$"),     users_start),
        ],
        states={
            S_DEBT_TYPE: [CallbackQueryHandler(debt_type_cb, pattern="^debt_")],
            S_NEW_NAME:  [MessageHandler(txt, new_name)],
            S_NEW_AMT:   [MessageHandler(txt, new_amt)],
            S_NEW_NOTE:  [
                MessageHandler(txt, new_note_text),
                CallbackQueryHandler(new_note_skip, pattern="^skip_note$"),
            ],
            S_EX_SEARCH: [
                MessageHandler(txt, ex_search),
                CallbackQueryHandler(ex_select, pattern="^ex_\\d+$"),
            ],
            S_EX_AMT:    [MessageHandler(txt, ex_amt)],
            S_EX_NOTE:   [
                MessageHandler(txt, ex_note_text),
                CallbackQueryHandler(ex_note_skip, pattern="^skip_note$"),
            ],
            S_PAY_SEARCH:[
                MessageHandler(txt, pay_search),
                CallbackQueryHandler(pay_select, pattern="^pay_\\d+$"),
            ],
            S_PAY_AMT:   [
                MessageHandler(txt, pay_amt_text),
                CallbackQueryHandler(pay_full_cb, pattern="^payfull_"),
            ],
            S_SRCH:      [MessageHandler(txt, search_query)],
            S_USR_ACT:   [CallbackQueryHandler(users_act_cb, pattern="^u_")],
            S_USR_ID:    [MessageHandler(txt, users_id)],
            S_USR_ROLE:  [CallbackQueryHandler(users_role_cb, pattern="^r_")],
        },
        fallbacks=[
            CommandHandler("cancel", cancel),
            CallbackQueryHandler(cancel, pattern="^cancel$"),
            # Навигация тугмаси босилса — оқимдан чиқиш
            MessageHandler(filters.Regex("^➕ Қарз қўшиш$"),         debt_start),
            MessageHandler(filters.Regex("^💰 Тўлов қабул қилиш$"),  pay_start),
            MessageHandler(filters.Regex("^🔍 Қидириш$"),            search_start),
            MessageHandler(filters.Regex("^👥 Фойдаланувчилар$"),    users_start),
            MessageHandler(filters.Regex("^📢 Бэкап$"),              handle_backup),
        ],
        allow_reentry=True,
        per_user=True,
        per_chat=True,
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("jami",  cmd_jami))
    app.add_handler(MessageHandler(filters.Regex("^📊 Статистика$"), handle_stats))
    app.add_handler(MessageHandler(filters.Regex("^📢 Бэкап$"),      handle_backup))
    app.add_handler(conv)

    logging.info("✅ Бот ишга тушди")
    app.run_polling(stop_signals=None, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    
    # Маълумотлар базасини хавфсиз ишга тушириш
    init_db()
    
    t = threading.Thread(target=run_bot, daemon=True)
    t.start()
    
    port = int(os.environ.get('PORT', 5000))
    flask_app.run(host='0.0.0.0', port=port)
