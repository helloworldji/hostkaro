import logging
import sqlite3
import os
import sys
import asyncio
import importlib.util
import importlib.metadata
import subprocess
import ast
import re
import json
import time
from datetime import datetime
from aiohttp import web, ClientSession

# Telegram Imports
from telegram import Update, BotCommand, ReplyKeyboardMarkup
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
    ConversationHandler,
    CallbackQueryHandler,
)

# ==========================================
# ⚙️ CONFIGURATION
# ==========================================
ADMIN_ID = 8175884349
GEMINI_API_KEY = "AIzaSyCE1ZG6R3yMF-95UNO0dlEjBFI4GtEOXOc"
RENDER_EXTERNAL_URL = "https://hostkaro.onrender.com"
PLATFORM_BOT_TOKEN = "8066184862:AAGxPAHFcwQAmEt9fsAuyZG8DUPt8A-01fY"
# ==========================================

# Configure Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# Global State
ACTIVE_BOTS = {}  # {token: application_object}
DB_FILE = "bot_platform.db"
BOTS_DIR = "user_bots"

# Create directories
os.makedirs(BOTS_DIR, exist_ok=True)

# Conversation States
(
    CHOOSING,
    GET_TOKEN_UPLOAD,
    GET_FILE_UPLOAD,
    GET_TOKEN_AI,
    GET_DESC_AI,
    GET_HELP_MESSAGE,
) = range(6)


# ==========================================
# 🗄️ DATABASE MANAGER
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute(
        """CREATE TABLE IF NOT EXISTS users
                 (user_id INTEGER PRIMARY KEY, username TEXT, joined_at TEXT)"""
    )
    c.execute(
        """CREATE TABLE IF NOT EXISTS bots
                 (bot_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id INTEGER,
                  token TEXT UNIQUE,
                  bot_username TEXT,
                  file_path TEXT,
                  status TEXT,
                  created_at TEXT)"""
    )
    conn.commit()
    conn.close()

def get_db_connection():
    return sqlite3.connect(DB_FILE)


# ==========================================
# 🧠 AI GENERATION LOGIC (GEMINI)
# ==========================================
async def generate_bot_code(description, token):
    if not GEMINI_API_KEY:
        return None, "System Error: AI API Key not configured."

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent?key={GEMINI_API_KEY}"
    
    system_prompt = f"""
    You are a Senior Python DevOps Engineer. Write a COMPLETE, production-ready Telegram Bot.
    
    RULES:
    1. Use 'python-telegram-bot' library (version 20+ async).
    2. The code MUST define a variable named `application` at the global scope.
    3. `application` must be built using `Application.builder().token('{token}').build()`.
    4. DO NOT include `application.run_polling()` or `application.run_webhook()` at the end. The hosting platform handles execution.
    5. Include 3-4 useful features based on the description: "{description}".
    6. Use `async def` for all handlers.
    7. Return ONLY the raw Python code. Do not start with ```python or markdown. Just the code.
    8. Imports must be standard or common (requests, numpy, etc.).
    """

    payload = {
        "contents": [{
            "parts": [{"text": f"{system_prompt}\n\nUser Request: Create a bot that does: {description}"}]
        }]
    }
    
    headers = {"Content-Type": "application/json"}

    try:
        async with ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"Gemini Error: {error_text}")
                    return None, f"Gemini API Error: {resp.status}"
                
                result = await resp.json()
                try:
                    content = result['candidates'][0]['content']['parts'][0]['text']
                    code = content.replace("```python", "").replace("```", "").strip()
                    return code, None
                except (KeyError, IndexError):
                    return None, "Failed to parse AI response."
    except Exception as e:
        logger.error(f"AI Generation Exception: {e}")
        return None, str(e)


# ==========================================
# ⚙️ BOT MANAGER
# ==========================================
async def install_dependencies(file_path):
    with open(file_path, "r", encoding="utf-8") as f:
        try:
            tree = ast.parse(f.read())
        except SyntaxError:
            return

    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                imports.add(n.name.split('.')[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module.split('.')[0])

    ignored = {'os', 'sys', 'asyncio', 'logging', 'telegram', 'typing', 'datetime', 'json', 're'}
    packages_to_install = []
    
    for lib in imports:
        if lib in ignored:
            continue
        try:
            importlib.metadata.version(lib)
        except importlib.metadata.PackageNotFoundError:
            packages_to_install.append(lib)

    if packages_to_install:
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", *packages_to_install])
        except Exception:
            pass

async def start_user_bot(token, file_path, context_app=None):
    try:
        await install_dependencies(file_path)
        spec = importlib.util.spec_from_file_location(f"bot_{token[:10]}", file_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"bot_{token[:10]}"] = module
        spec.loader.exec_module(module)

        if not hasattr(module, "application"):
            return False, "Code must define an 'application' object."
        
        user_app = module.application
        await user_app.initialize()
        await user_app.start()
        webhook_url = f"{RENDER_EXTERNAL_URL}/bot/{token}"
        await user_app.bot.set_webhook(url=webhook_url)

        ACTIVE_BOTS[token] = user_app
        return True, "Bot started successfully."

    except Exception as e:
        logger.error(f"Failed to start bot {token}: {e}")
        return False, str(e)

async def stop_user_bot(token):
    if token in ACTIVE_BOTS:
        app = ACTIVE_BOTS[token]
        await app.stop()
        await app.shutdown()
        del ACTIVE_BOTS[token]


# ==========================================
# 🎮 TELEGRAM HANDLERS
# ==========================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO users (user_id, username, joined_at) VALUES (?, ?, ?)",
              (user.id, user.username, str(datetime.now())))
    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"👋 Hi {user.first_name}! Welcome to the **Free Bot Hosting Platform**.\n\n"
        "I can host your Python Telegram bots 24/7 on this server.\n\n"
        "👇 **Choose an option:**",
        reply_markup=ReplyKeyboardMarkup(
            [["📤 Host Existing Bot", "✨ Create Bot (AI)"], ["📊 My Bots", "🆘 Help"]],
            resize_keyboard=True
        )
    )
    return CHOOSING

async def back_to_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)
    return CHOOSING

# --- SMART NAVIGATION CHECKER ---
async def check_navigation(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Checks if user clicked a menu button while inside a conversation."""
    text = update.message.text
    if text == "🏠 Main Menu" or text == "/start":
        await back_to_main(update, context)
        return True
    if text == "✨ Create Bot (AI)":
        await create_ai_start(update, context)
        return True
    if text == "📤 Host Existing Bot":
        await host_start(update, context)
        return True
    if text == "🆘 Help":
        await help_start(update, context)
        return True
    if text == "📊 My Bots":
        await my_bots(update, context)
        return True
    return False

# --- HOST EXISTING BOT FLOW ---
async def host_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "1️⃣ Please send me your **Telegram Bot Token** from @BotFather.",
        reply_markup=ReplyKeyboardMarkup([["🏠 Main Menu"]], resize_keyboard=True)
    )
    return GET_TOKEN_UPLOAD

async def receive_token_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_navigation(update, context): return ConversationHandler.END
    
    text = update.message.text.strip()
    if not re.match(r'^\d+:[A-Za-z0-9_-]+$', text):
        await update.message.reply_text("❌ Invalid token format. Please try again or click 🏠 Main Menu.")
        return GET_TOKEN_UPLOAD
    
    context.user_data['token'] = text
    await update.message.reply_text(
        "✅ Token accepted.\n2️⃣ Please <b>upload your Python (.py) file</b>.", 
        parse_mode='HTML'
    )
    return GET_FILE_UPLOAD

async def receive_file_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message.text and await check_navigation(update, context): return ConversationHandler.END

    file = await update.message.document.get_file()
    if not file.file_path.endswith('.py'):
        await update.message.reply_text("❌ Please upload a .py file.")
        return GET_FILE_UPLOAD

    token = context.user_data['token']
    user_id = update.effective_user.id
    file_path = os.path.join(BOTS_DIR, f"{user_id}_{token.split(':')[0]}.py")

    status_msg = await update.message.reply_text("⏳ Downloading and scanning file...")
    await file.download_to_drive(file_path)

    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO bots (user_id, token, file_path, status, created_at) VALUES (?, ?, ?, ?, ?)",
              (user_id, token, file_path, "running", str(datetime.now())))
    conn.commit()
    conn.close()

    await context.bot.edit_message_text("⚙️ Deploying...", chat_id=update.effective_chat.id, message_id=status_msg.message_id)
    success, msg = await start_user_bot(token, file_path)
    
    if success:
        await context.bot.edit_message_text(f"🚀 <b>Bot Deployed Successfully!</b>", chat_id=update.effective_chat.id, message_id=status_msg.message_id, parse_mode='HTML')
    else:
        await context.bot.edit_message_text(f"❌ Deployment Failed.\nError: {msg}", chat_id=update.effective_chat.id, message_id=status_msg.message_id)

    return ConversationHandler.END

# --- CREATE AI BOT FLOW ---
async def create_ai_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "✨ Let's create a bot with Google Gemini!\n\n1️⃣ Send me the **Telegram Bot Token** from @BotFather.",
        reply_markup=ReplyKeyboardMarkup([["🏠 Main Menu"]], resize_keyboard=True)
    )
    return GET_TOKEN_AI

async def receive_token_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_navigation(update, context): return ConversationHandler.END

    context.user_data['token'] = update.message.text.strip()
    await update.message.reply_text("2️⃣ Describe what you want your bot to do.", parse_mode='HTML')
    return GET_DESC_AI

async def receive_desc_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_navigation(update, context): return ConversationHandler.END

    description = update.message.text
    token = context.user_data['token']
    
    status_msg = await update.message.reply_text("🧠 Gemini is thinking... (This takes ~10s)")
    code, error = await generate_bot_code(description, token)
    
    if error:
        await status_msg.edit_text(f"❌ AI Error: {error}")
        return ConversationHandler.END

    user_id = update.effective_user.id
    file_path = os.path.join(BOTS_DIR, f"{user_id}_{token.split(':')[0]}_ai.py")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(code)

    await status_msg.edit_text("💾 Code generated! Deploying...")
    
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO bots (user_id, token, file_path, status, created_at) VALUES (?, ?, ?, ?, ?)",
              (user_id, token, file_path, "running", str(datetime.now())))
    conn.commit()
    conn.close()

    success, msg = await start_user_bot(token, file_path)
    if success:
        await update.message.reply_text(f"🚀 <b>Your AI Bot is LIVE!</b>\n\nTry sending /start to it.", parse_mode='HTML')
    else:
        await update.message.reply_text(f"❌ Deployment Failed.\nError: {msg}")

    return ConversationHandler.END

# --- HELP & SUPPORT FLOW ---
async def help_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🆘 <b>Support Center</b>\n\nDescribe your problem below. I will forward it to the Admin.",
        parse_mode='HTML',
        reply_markup=ReplyKeyboardMarkup([["🔙 Back", "🏠 Main Menu"]], resize_keyboard=True)
    )
    return GET_HELP_MESSAGE

async def receive_help_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if await check_navigation(update, context): return ConversationHandler.END

    text = update.message.text
    user = update.effective_user
    
    admin_message = (
        f"🚨 <b>New Support Ticket</b>\n\n"
        f"👤 <b>User:</b> {user.first_name} (@{user.username})\n"
        f"🆔 <b>ID:</b> <code>{user.id}</code>\n\n"
        f"📝 <b>Issue:</b>\n{text}"
    )
    
    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=admin_message, parse_mode='HTML')
        await update.message.reply_text("✅ <b>Ticket Sent!</b>", parse_mode='HTML')
    except Exception:
        await update.message.reply_text("❌ Failed to contact admin.")

    return await start(update, context)

# --- INFO & STATS ---
async def my_bots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conn = get_db_connection()
    c = conn.cursor()
    bots = c.execute("SELECT token, status FROM bots WHERE user_id=?", (user_id,)).fetchall()
    conn.close()

    if not bots:
        await update.message.reply_text("You have no hosted bots.")
        return

    msg = "📊 <b>Your Bots:</b>\n"
    for token, status in bots:
        msg += f"- <code>{token[:5]}...</code>: {status.upper()}\n"
    await update.message.reply_text(msg, parse_mode='HTML')

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    conn = get_db_connection()
    c = conn.cursor()
    user_count = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    bot_count = c.execute("SELECT COUNT(*) FROM bots").fetchone()[0]
    conn.close()
    await update.message.reply_text(f"👑 **Stats:**\n👥 Users: {user_count}\n🤖 Bots: {bot_count}")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Operation cancelled.")
    return ConversationHandler.END

# ==========================================
# 🌐 WEBHOOK ROUTER
# ==========================================
async def webhook_handler(request):
    token = request.match_info.get('token')
    try:
        data = await request.json()
    except:
        return web.Response(status=400)

    if token == PLATFORM_BOT_TOKEN:
        update = Update.de_json(data, platform_app.bot)
        await platform_app.process_update(update)
    elif token in ACTIVE_BOTS:
        user_app = ACTIVE_BOTS[token]
        update = Update.de_json(data, user_app.bot)
        await user_app.process_update(update)
    
    return web.Response(text="OK")

async def health_check(request):
    return web.Response(text="Alive")

# ==========================================
# 🚀 MAIN ENTRY POINT
# ==========================================
async def restore_bots():
    conn = get_db_connection()
    c = conn.cursor()
    bots = c.execute("SELECT token, file_path FROM bots WHERE status='running'").fetchall()
    conn.close()
    for token, path in bots:
        if os.path.exists(path):
            await start_user_bot(token, path)

async def safe_start_application(app, max_retries=10):
    for attempt in range(max_retries):
        try:
            await app.initialize()
            await app.start()
            return True
        except Exception:
            await asyncio.sleep(5)
    return False

def main():
    global platform_app
    init_db()

    trequest = HTTPXRequest(connection_pool_size=4, connect_timeout=120.0, read_timeout=120.0, http_version="1.1")
    platform_app = Application.builder().token(PLATFORM_BOT_TOKEN).request(trequest).build()

    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r"^📤 Host Existing Bot$"), host_start),
            MessageHandler(filters.Regex(r"^✨ Create Bot \(AI\)$"), create_ai_start),
            MessageHandler(filters.Regex(r"^🆘 Help$"), help_start),
        ],
        states={
            GET_TOKEN_UPLOAD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_token_upload)],
            GET_FILE_UPLOAD: [MessageHandler(filters.Document.FileExtension("py") | filters.Regex(r"^🏠 Main Menu$"), receive_file_upload)],
            GET_TOKEN_AI: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_token_ai)],
            GET_DESC_AI: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_desc_ai)],
            GET_HELP_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_help_message)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start)],
    )

    platform_app.add_handler(CommandHandler("start", start))
    platform_app.add_handler(MessageHandler(filters.Regex(r"^📊 My Bots$"), my_bots))
    platform_app.add_handler(CommandHandler("stats", admin_stats))
    platform_app.add_handler(conv_handler)

    app = web.Application()
    app.router.add_post('/bot/{token}', webhook_handler)
    app.router.add_get('/', health_check)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    async def runner():
        if not await safe_start_application(platform_app): sys.exit(1)
        await platform_app.bot.set_webhook(f"{RENDER_EXTERNAL_URL}/bot/{PLATFORM_BOT_TOKEN}")
        await restore_bots()
        
        runner = web.AppRunner(app)
        await runner.setup()
        await web.TCPSite(runner, '0.0.0.0', int(os.environ.get("PORT", 8080))).start()
        await asyncio.Event().wait()

    try:
        loop.run_until_complete(runner())
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
