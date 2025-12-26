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
from datetime import datetime
from aiohttp import web, ClientSession

# Telegram Imports
# FIXED: Added ReplyKeyboardMarkup to imports
from telegram import Update, BotCommand, ReplyKeyboardMarkup
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
# ⚙️ CONFIGURATION (HARDCODED)
# ==========================================
ADMIN_ID = 8175884349
DEEPSEEK_API_KEY = "sk-d0522e698322494db0196cdfbdecca05"
RENDER_EXTERNAL_URL = "https://hostkaro.onrender.com"
PLATFORM_BOT_TOKEN = "8066184862:AAGxPAHFcwQAmEt9fsAuyZG8DUPt8A-01fY"
# ==========================================

# Configure Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
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
    CONFIRM_AI,
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
# 🧠 AI GENERATION LOGIC (DEEPSEEK)
# ==========================================
async def generate_bot_code(description, token):
    """Generates Python code for a Telegram bot using DeepSeek API."""
    if not DEEPSEEK_API_KEY:
        return None, "System Error: AI API Key not configured."

    url = "https://api.deepseek.com/chat/completions"
    
    system_prompt = f"""
    You are a Senior Python DevOps Engineer. Write a COMPLETE, production-ready Telegram Bot.
    
    RULES:
    1. Use 'python-telegram-bot' library (version 20+ async).
    2. The code MUST define a variable named `application` at the global scope.
    3. `application` must be built using `Application.builder().token('{token}').build()`.
    4. DO NOT include `application.run_polling()` or `application.run_webhook()` at the end. The hosting platform handles execution.
    5. Include 3-4 useful features based on the description: "{description}".
    6. Use `async def` for all handlers.
    7. Return ONLY the raw Python code. No markdown backticks. No explanations.
    8. Imports must be standard or common (requests, numpy, etc.).
    """

    payload = {
        "model": "deepseek-coder",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"Create a bot that does: {description}"}
        ],
        "temperature": 0.7,
        "stream": False
    }
    
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json"
    }

    try:
        async with ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"DeepSeek Error: {error_text}")
                    return None, f"DeepSeek API Error: {resp.status}"
                
                result = await resp.json()
                content = result['choices'][0]['message']['content']
                
                # Cleanup code blocks
                code = content.replace("```python", "").replace("```", "").strip()
                return code, None
    except Exception as e:
        logger.error(f"AI Generation Exception: {e}")
        return None, str(e)


# ==========================================
# ⚙️ BOT MANAGER (The Core Engine)
# ==========================================
async def install_dependencies(file_path):
    """Scans code for imports and installs missing ones."""
    with open(file_path, "r", encoding="utf-8") as f:
        try:
            tree = ast.parse(f.read())
        except SyntaxError:
            return # Let the runtime fail later if syntax is bad

    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                imports.add(n.name.split('.')[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module.split('.')[0])

    # Whitelist/Blacklist
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
        logger.info(f"Installing dependencies: {packages_to_install}")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", *packages_to_install])
        except Exception as e:
            logger.error(f"Failed to install dependencies: {e}")


async def start_user_bot(token, file_path, context_app=None):
    """Dynamically loads and starts a user bot."""
    try:
        # 1. Dependency Check
        await install_dependencies(file_path)

        # 2. Dynamic Import
        spec = importlib.util.spec_from_file_location(f"bot_{token[:10]}", file_path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[f"bot_{token[:10]}"] = module
        spec.loader.exec_module(module)

        # 3. Extract Application
        if not hasattr(module, "application"):
            return False, "Code must define an 'application' object."
        
        user_app = module.application

        # 4. Initialize & Start (Inject into asyncio loop)
        await user_app.initialize()
        await user_app.start()
        
        # 5. Register Webhook
        webhook_url = f"{RENDER_EXTERNAL_URL}/bot/{token}"
        await user_app.bot.set_webhook(url=webhook_url)

        # 6. Store in Registry
        ACTIVE_BOTS[token] = user_app
        logger.info(f"Bot {token[:10]} started successfully on webhook {webhook_url}")
        
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
# 🎮 PLATFORM INTERFACE (Telegram Handlers)
# ==========================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # Save User
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
        # FIXED: Removed 'filters.' prefix here
        reply_markup=ReplyKeyboardMarkup(
            [["📤 Host Existing Bot", "✨ Create Bot (AI)"], ["📊 My Bots", "🆘 Help"]],
            resize_keyboard=True
        )
    )
    return CHOOSING

# --- HOST EXISTING BOT FLOW ---
async def host_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("1️⃣ Please send me your **Telegram Bot Token** from @BotFather.")
    return GET_TOKEN_UPLOAD

async def receive_token_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    token = update.message.text.strip()
    # Simple validation
    if not re.match(r'^\d+:[A-Za-z0-9_-]+$', token):
        await update.message.reply_text("❌ Invalid token format. Please try again.")
        return GET_TOKEN_UPLOAD
    
    context.user_data['token'] = token
    await update.message.reply_text(
        "✅ Token accepted.\n\n"
        "2️⃣ Now, please **upload your Python (.py) file**.\n"
        "⚠️ _Ensure your code defines an `application` object and DOES NOT use `run_polling()`._"
    , parse_mode='Markdown')
    return GET_FILE_UPLOAD

async def receive_file_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    file = await update.message.document.get_file()
    if not file.file_path.endswith('.py'):
        await update.message.reply_text("❌ Please upload a .py file.")
        return GET_FILE_UPLOAD

    token = context.user_data['token']
    user_id = update.effective_user.id
    file_path = os.path.join(BOTS_DIR, f"{user_id}_{token.split(':')[0]}.py")

    status_msg = await update.message.reply_text("⏳ Downloading and scanning file...")
    await file.download_to_drive(file_path)

    # Save to DB
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO bots (user_id, token, file_path, status, created_at) VALUES (?, ?, ?, ?, ?)",
              (user_id, token, file_path, "running", str(datetime.now())))
    conn.commit()
    conn.close()

    await context.bot.edit_message_text("⚙️ Installing dependencies and deploying...", chat_id=update.effective_chat.id, message_id=status_msg.message_id)

    # Launch
    success, msg = await start_user_bot(token, file_path)
    
    if success:
        await context.bot.edit_message_text(f"🚀 **Bot Deployed Successfully!**\n\nStatus: Online\nEngine: Webhook", chat_id=update.effective_chat.id, message_id=status_msg.message_id, parse_mode='Markdown')
    else:
        await context.bot.edit_message_text(f"❌ Deployment Failed.\nError: {msg}", chat_id=update.effective_chat.id, message_id=status_msg.message_id)

    return ConversationHandler.END

# --- CREATE AI BOT FLOW ---
async def create_ai_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✨ Let's create a bot with AI (DeepSeek)!\n\n1️⃣ Send me the **Telegram Bot Token** from @BotFather.")
    return GET_TOKEN_AI

async def receive_token_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data['token'] = update.message.text.strip()
    await update.message.reply_text("2️⃣ Describe what you want your bot to do.\n\n_Example: A bot that welcomes users in groups and deletes links._", parse_mode='Markdown')
    return GET_DESC_AI

async def receive_desc_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    description = update.message.text
    token = context.user_data['token']
    
    status_msg = await update.message.reply_text("🧠 DeepSeek is thinking... (This takes ~15s)")
    
    code, error = await generate_bot_code(description, token)
    
    if error:
        await status_msg.edit_text(f"❌ AI Error: {error}")
        return ConversationHandler.END

    # Save code
    user_id = update.effective_user.id
    file_path = os.path.join(BOTS_DIR, f"{user_id}_{token.split(':')[0]}_ai.py")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(code)

    await status_msg.edit_text("💾 Code generated! Deploying to server...")

    # Save to DB
    conn = get_db_connection()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO bots (user_id, token, file_path, status, created_at) VALUES (?, ?, ?, ?, ?)",
              (user_id, token, file_path, "running", str(datetime.now())))
    conn.commit()
    conn.close()

    # Launch
    success, msg = await start_user_bot(token, file_path)
    if success:
        await update.message.reply_text(f"🚀 **Your AI Bot is LIVE!**\n\nTry sending /start to it.", parse_mode='Markdown')
    else:
        await update.message.reply_text(f"❌ Deployment Failed.\nError: {msg}")

    return ConversationHandler.END

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

    msg = "📊 **Your Bots:**\n"
    for token, status in bots:
        masked_token = f"{token[:5]}...{token[-5:]}"
        msg += f"- `{masked_token}`: {status.upper()}\n"
    
    await update.message.reply_text(msg, parse_mode='Markdown')

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return

    conn = get_db_connection()
    c = conn.cursor()
    user_count = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    bot_count = c.execute("SELECT COUNT(*) FROM bots").fetchone()[0]
    conn.close()
    
    active_runtime = len(ACTIVE_BOTS)
    
    await update.message.reply_text(
        f"👑 **Admin Stats:**\n\n"
        f"👥 Total Users: {user_count}\n"
        f"🤖 Total Bots (DB): {bot_count}\n"
        f"⚡ Active Bots (RAM): {active_runtime}\n"
        f"🐍 Python: {sys.version.split()[0]}"
    )

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🚫 Operation cancelled.")
    return ConversationHandler.END

# ==========================================
# 🌐 WEBHOOK ROUTER (The Multiplexer)
# ==========================================
async def webhook_handler(request):
    """
    Central router that receives ALL Telegram updates.
    Routes updates to specific bot instances based on URL token.
    """
    token = request.match_info.get('token')
    
    if token == PLATFORM_BOT_TOKEN:
        # Update for the Platform Bot itself
        try:
            data = await request.json()
            update = Update.de_json(data, platform_app.bot)
            await platform_app.process_update(update)
            return web.Response(text="OK")
        except Exception as e:
            logger.error(f"Main Bot Error: {e}")
            return web.Response(status=500)

    elif token in ACTIVE_BOTS:
        # Update for a User's Bot
        try:
            data = await request.json()
            user_app = ACTIVE_BOTS[token]
            # Create Update object bound to the user's bot instance
            update = Update.de_json(data, user_app.bot)
            # Feed into user bot's event loop
            await user_app.process_update(update)
            return web.Response(text="OK")
        except Exception as e:
            logger.error(f"User Bot {token[:5]} Error: {e}")
            return web.Response(status=500)
    
    return web.Response(status=404, text="Bot not found")

async def health_check(request):
    return web.Response(text="Alive")

# ==========================================
# 🚀 MAIN ENTRY POINT
# ==========================================
async def restore_bots():
    """Restores bots from DB on server restart."""
    conn = get_db_connection()
    c = conn.cursor()
    bots = c.execute("SELECT token, file_path FROM bots WHERE status='running'").fetchall()
    conn.close()
    
    logger.info(f"♻️ Restoring {len(bots)} bots from database...")
    for token, path in bots:
        if os.path.exists(path):
            await start_user_bot(token, path)
        else:
            logger.warning(f"File missing for bot {token}")

def main():
    global platform_app

    # 1. Initialize DB
    init_db()

    # 2. Build Platform Bot
    platform_app = Application.builder().token(PLATFORM_BOT_TOKEN).build()

    # 3. Add Handlers
    conv_handler = ConversationHandler(
        entry_points=[
            # FIXED: Used raw strings r"..." to fix SyntaxWarning
            MessageHandler(filters.Regex(r"^📤 Host Existing Bot$"), host_start),
            MessageHandler(filters.Regex(r"^✨ Create Bot \(AI\)$"), create_ai_start),
        ],
        states={
            GET_TOKEN_UPLOAD: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_token_upload)],
            GET_FILE_UPLOAD: [MessageHandler(filters.Document.FileExtension("py"), receive_file_upload)],
            GET_TOKEN_AI: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_token_ai)],
            GET_DESC_AI: [MessageHandler(filters.TEXT & ~filters.COMMAND, receive_desc_ai)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    platform_app.add_handler(CommandHandler("start", start))
    platform_app.add_handler(MessageHandler(filters.Regex(r"^📊 My Bots$"), my_bots))
    platform_app.add_handler(CommandHandler("stats", admin_stats))
    platform_app.add_handler(conv_handler)

    # 4. Setup Aiohttp Web Server (Custom Webhook Logic)
    # We use aiohttp directly to have full control over routing
    app = web.Application()
    app.router.add_post('/bot/{token}', webhook_handler) # Handles main bot AND user bots
    app.router.add_get('/', health_check) # For UptimeRobot

    # 5. Run Everything
    # We must initialize the platform app manually since we aren't using .run_polling
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    async def runner():
        await platform_app.initialize()
        await platform_app.start()
        
        # Set webhook for the main platform bot
        await platform_app.bot.set_webhook(f"{RENDER_EXTERNAL_URL}/bot/{PLATFORM_BOT_TOKEN}")
        
        # Restore user bots
        await restore_bots()
        
        # Start Web Server
        runner = web.AppRunner(app)
        await runner.setup()
        port = int(os.environ.get("PORT", 8080))
        site = web.TCPSite(runner, '0.0.0.0', port)
        await site.start()
        
        logger.info(f"🌍 Server running on port {port}")
        
        # Keep alive
        await asyncio.Event().wait()

    try:
        loop.run_until_complete(runner())
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
