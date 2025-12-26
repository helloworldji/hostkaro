"""
TELEGRAM BOT HOSTING PLATFORM
Host Python Telegram bots for FREE - 24/7
"""

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
import time
import html
from datetime import datetime
from contextlib import contextmanager
from typing import Optional, Tuple, Dict
from aiohttp import web, ClientSession, ClientTimeout

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
)
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
# CONFIGURATION
# ==========================================
ADMIN_ID = 8175884349
GEMINI_API_KEY = "AIzaSyCE1ZG6R3yMF-95UNO0dlEjBFI4GtEOXOc"
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
RENDER_EXTERNAL_URL = "https://hostkaro.onrender.com"
PLATFORM_BOT_TOKEN = "8066184862:AAGxPAHFcwQAmEt9fsAuyZG8DUPt8A-01fY"

# ==========================================
# LOGGING
# ==========================================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("aiohttp").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

# ==========================================
# GLOBAL STATE
# ==========================================
ACTIVE_BOTS: Dict[str, Application] = {}
DB_FILE = "bot_platform.db"
BOTS_DIR = "user_bots"
platform_app: Optional[Application] = None

os.makedirs(BOTS_DIR, exist_ok=True)

# ==========================================
# CONVERSATION STATES
# ==========================================
(
    MAIN_MENU,
    HOST_GET_TOKEN,
    HOST_GET_FILE,
    CREATE_GET_TOKEN,
    CREATE_GET_DESCRIPTION,
    CREATE_COMMANDS_TYPE,
    CREATE_CHAT_TYPE,
    CREATE_LANGUAGE,
    CREATE_DATABASE,
    HELP_GET_MESSAGE,
) = range(10)


# ==========================================
# DATABASE
# ==========================================
def init_db():
    conn = sqlite3.connect(DB_FILE)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            joined_at TEXT,
            last_active TEXT
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS bots (
            bot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            token TEXT UNIQUE,
            bot_username TEXT,
            file_path TEXT,
            status TEXT DEFAULT 'stopped',
            creation_type TEXT,
            created_at TEXT,
            error_log TEXT,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS stats (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT,
            timestamp TEXT
        )
    """)
    conn.commit()
    conn.close()
    logger.info("Database initialized")


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def save_user(user_id: int, username: str, first_name: str):
    with get_db() as conn:
        c = conn.cursor()
        now = datetime.now().isoformat()
        c.execute(
            """INSERT INTO users (user_id, username, first_name, joined_at, last_active)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
               username = excluded.username,
               first_name = excluded.first_name,
               last_active = excluded.last_active""",
            (user_id, username, first_name, now, now)
        )


def save_bot(user_id: int, token: str, file_path: str, creation_type: str, bot_username: str = None):
    with get_db() as conn:
        c = conn.cursor()
        now = datetime.now().isoformat()
        c.execute(
            """INSERT INTO bots (user_id, token, bot_username, file_path, status, creation_type, created_at)
               VALUES (?, ?, ?, ?, 'running', ?, ?)
               ON CONFLICT(token) DO UPDATE SET
               file_path = excluded.file_path,
               status = 'running'""",
            (user_id, token, bot_username, file_path, creation_type, now)
        )


def get_user_bots(user_id: int):
    with get_db() as conn:
        return conn.execute("SELECT * FROM bots WHERE user_id = ?", (user_id,)).fetchall()


def update_bot_status(token: str, status: str, error: str = None):
    with get_db() as conn:
        if error:
            conn.execute("UPDATE bots SET status = ?, error_log = ? WHERE token = ?", (status, error, token))
        else:
            conn.execute("UPDATE bots SET status = ? WHERE token = ?", (status, token))


def get_all_running_bots():
    with get_db() as conn:
        return conn.execute("SELECT token, file_path FROM bots WHERE status = 'running'").fetchall()


def delete_bot_from_db(token: str):
    with get_db() as conn:
        conn.execute("DELETE FROM bots WHERE token = ?", (token,))


def get_stats():
    with get_db() as conn:
        c = conn.cursor()
        users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        bots = c.execute("SELECT COUNT(*) FROM bots").fetchone()[0]
        return {"users": users, "total_bots": bots}


# ==========================================
# CODE VALIDATION
# ==========================================
def validate_python_code(code: str) -> Tuple[bool, str]:
    try:
        ast.parse(code)
    except SyntaxError as e:
        return False, f"Syntax Error at line {e.lineno}: {e.msg}"
    return True, "OK"


def detect_imports(file_path: str) -> set:
    with open(file_path, "r", encoding="utf-8") as f:
        try:
            tree = ast.parse(f.read())
        except SyntaxError:
            return set()
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split('.')[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module.split('.')[0])
    return imports


# ==========================================
# BOT MANAGER
# ==========================================
async def install_dependencies(file_path: str) -> Tuple[bool, str]:
    imports = detect_imports(file_path)
    stdlib = {
        'os', 'sys', 'asyncio', 'logging', 'json', 're', 'typing', 'datetime',
        'time', 'random', 'math', 'collections', 'itertools', 'functools',
        'pathlib', 'io', 'hashlib', 'base64', 'urllib', 'http', 'html',
        'sqlite3', 'pickle', 'copy', 'threading', 'contextlib', 'string'
    }
    package_map = {
        'telegram': 'python-telegram-bot',
        'PIL': 'Pillow',
        'cv2': 'opencv-python',
        'sklearn': 'scikit-learn',
        'yaml': 'pyyaml',
        'bs4': 'beautifulsoup4',
    }
    to_install = []
    for lib in imports:
        if lib in stdlib:
            continue
        pkg = package_map.get(lib, lib)
        try:
            importlib.metadata.version(pkg.split('>=')[0].split('==')[0])
        except importlib.metadata.PackageNotFoundError:
            to_install.append(pkg)
    if to_install:
        try:
            logger.info(f"Installing: {to_install}")
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install"] + to_install,
                capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                return False, result.stderr[:200]
        except subprocess.TimeoutExpired:
            return False, "Installation timed out"
        except Exception as e:
            return False, str(e)
    return True, "OK"


async def validate_bot_token(token: str) -> Tuple[bool, Optional[str], Optional[str]]:
    try:
        async with ClientSession(timeout=ClientTimeout(total=10)) as session:
            async with session.get(f"https://api.telegram.org/bot{token}/getMe") as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("ok"):
                        info = data["result"]
                        return True, info.get("username"), info.get("first_name")
                return False, None, None
    except Exception as e:
        logger.error(f"Token validation error: {e}")
        return False, None, None


async def start_user_bot(token: str, file_path: str) -> Tuple[bool, str]:
    try:
        if token in ACTIVE_BOTS:
            await stop_user_bot(token)
        success, msg = await install_dependencies(file_path)
        if not success:
            return False, f"Dependency error: {msg}"
        module_name = f"userbot_{token[:10]}_{int(time.time())}"
        spec = importlib.util.spec_from_file_location(module_name, file_path)
        if spec is None or spec.loader is None:
            return False, "Failed to load module"
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception as e:
            return False, f"Code error: {str(e)[:100]}"
        if not hasattr(module, 'application'):
            return False, "Code must define 'application' variable"
        user_app = module.application
        await user_app.initialize()
        await user_app.start()
        webhook_url = f"{RENDER_EXTERNAL_URL}/bot/{token}"
        await user_app.bot.set_webhook(url=webhook_url)
        ACTIVE_BOTS[token] = user_app
        update_bot_status(token, "running")
        logger.info(f"Started bot: {token[:15]}...")
        return True, "Bot started successfully"
    except Exception as e:
        error_msg = f"{type(e).__name__}: {str(e)[:100]}"
        logger.error(f"Failed to start bot: {error_msg}")
        update_bot_status(token, "error", error_msg)
        return False, error_msg


async def stop_user_bot(token: str) -> Tuple[bool, str]:
    try:
        if token in ACTIVE_BOTS:
            app = ACTIVE_BOTS[token]
            try:
                await app.bot.delete_webhook()
            except:
                pass
            await app.stop()
            await app.shutdown()
            del ACTIVE_BOTS[token]
        update_bot_status(token, "stopped")
        logger.info(f"Stopped bot: {token[:15]}...")
        return True, "Bot stopped"
    except Exception as e:
        return False, str(e)


# ==========================================
# GEMINI BOT CODE GENERATOR
# ==========================================
async def generate_bot_code(
    description: str,
    token: str,
    use_commands: bool = True,
    use_buttons: bool = True,
    chat_type: str = "both",
    language: str = "English",
    use_database: bool = False
) -> Tuple[Optional[str], Optional[str]]:
    
    features = []
    if use_commands:
        features.append("slash commands (/start, /help, custom commands)")
    if use_buttons:
        features.append("inline keyboard buttons")
    if use_database:
        features.append("SQLite database for user data")
    
    chat_desc = {
        "private": "private chats only",
        "groups": "group chats only",
        "both": "private and group chats"
    }.get(chat_type, "both")
    
    prompt = f"""You are an expert Python developer specializing in Telegram bots.
Generate complete, production-ready Python code for a Telegram bot using python-telegram-bot library version 20+.

CRITICAL RULES:
1. Use python-telegram-bot library version 20+ (async version)
2. Define a global variable at the end: application = Application.builder().token("{token}").build()
3. DO NOT include application.run_polling() or application.run_webhook() at the end
4. All handlers must be async (async def)
5. Include proper error handling with try/except
6. Add logging at the top
7. Return ONLY raw Python code - no markdown, no explanations, no ``` blocks
8. The code must be complete and ready to run

BOT REQUIREMENTS:
- Token: {token}
- Description: {description}
- Response language: {language}
- Target chat type: {chat_desc}
- Features: {', '.join(features) if features else 'basic commands'}
- Must include /start and /help commands

Generate the complete Python code now:"""

    headers = {
        "Content-Type": "application/json",
        "X-goog-api-key": GEMINI_API_KEY
    }
    
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.7,
            "maxOutputTokens": 8192
        }
    }
    
    try:
        async with ClientSession(timeout=ClientTimeout(total=60)) as session:
            async with session.post(GEMINI_API_URL, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    error_text = await resp.text()
                    logger.error(f"Gemini API error: {resp.status} - {error_text}")
                    return None, f"Generation failed (Error {resp.status})"
                
                result = await resp.json()
                
                try:
                    # Extract text from Gemini response
                    content = result['candidates'][0]['content']['parts'][0]['text']
                    code = content.strip()
                    
                    # Clean up markdown if present
                    code = re.sub(r'^```python\s*\n?', '', code)
                    code = re.sub(r'^```\s*\n?', '', code)
                    code = re.sub(r'\n?```$', '', code)
                    code = code.strip()
                    
                    # Validate syntax
                    valid, error = validate_python_code(code)
                    if not valid:
                        return None, f"Generated code error: {error}"
                    
                    # Check for application variable
                    if 'application' not in code:
                        return None, "Generation failed - missing application variable"
                    
                    return code, None
                    
                except (KeyError, IndexError) as e:
                    logger.error(f"Failed to parse Gemini response: {e}")
                    logger.error(f"Response: {result}")
                    return None, "Failed to parse response"
                    
    except asyncio.TimeoutError:
        return None, "Request timed out. Please try again."
    except Exception as e:
        logger.error(f"Gemini API exception: {e}")
        return None, str(e)


# ==========================================
# HELPER - Escape HTML
# ==========================================
def esc(text) -> str:
    if text is None:
        return ""
    return html.escape(str(text))


# ==========================================
# KEYBOARDS
# ==========================================
def main_menu_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [["📤 Host My Bot", "✨ Create New Bot"], ["📊 My Bots", "🆘 Help"]],
        resize_keyboard=True
    )


def back_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([["🔙 Back", "🏠 Main Menu"]], resize_keyboard=True)


def commands_type_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📝 Commands Only", callback_data="cmd_commands")],
        [InlineKeyboardButton("🔘 Buttons Only", callback_data="cmd_buttons")],
        [InlineKeyboardButton("📝🔘 Both", callback_data="cmd_both")],
    ])


def chat_type_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Private Only", callback_data="chat_private")],
        [InlineKeyboardButton("👥 Groups Only", callback_data="chat_groups")],
        [InlineKeyboardButton("👤👥 Both", callback_data="chat_both")],
    ])


def language_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🇬🇧 English", callback_data="lang_english"),
            InlineKeyboardButton("🇮🇳 Hindi", callback_data="lang_hindi")
        ],
        [
            InlineKeyboardButton("🇪🇸 Spanish", callback_data="lang_spanish"),
            InlineKeyboardButton("🌍 Auto-detect", callback_data="lang_auto")
        ],
    ])


def yes_no_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Yes", callback_data="db_yes"),
            InlineKeyboardButton("❌ No", callback_data="db_no")
        ]
    ])


# ==========================================
# HANDLERS - MAIN
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    save_user(user.id, user.username, user.first_name)
    context.user_data.clear()
    text = (
        f"👋 Welcome <b>{esc(user.first_name)}</b>!\n\n"
        f"🤖 <b>Free Bot Hosting Platform</b>\n\n"
        f"I can host your Python Telegram bots 24/7 for FREE!\n\n"
        f"Choose an option below:"
    )
    await update.message.reply_text(text, reply_markup=main_menu_kb(), parse_mode='HTML')
    return MAIN_MENU


async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if "Host" in text or "📤" in text:
        return await host_start(update, context)
    elif "Create" in text or "✨" in text:
        return await create_start(update, context)
    elif "My Bots" in text or "📊" in text:
        return await my_bots(update, context)
    elif "Help" in text or "🆘" in text:
        return await help_start(update, context)
    else:
        await update.message.reply_text("Please choose an option:", reply_markup=main_menu_kb())
        return MAIN_MENU


async def go_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    return await start(update, context)


# ==========================================
# HOST BOT FLOW
# ==========================================
async def host_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = (
        "📤 <b>Host Your Bot</b>\n\n"
        "Step 1: Send me your Bot Token from @BotFather\n\n"
        "<i>Example: 123456789:ABCdefGHIjklMNOpqrsTUVwxyz</i>"
    )
    await update.message.reply_text(text, reply_markup=back_kb(), parse_mode='HTML')
    return HOST_GET_TOKEN


async def host_get_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if "Back" in text or "Main Menu" in text or "🔙" in text or "🏠" in text:
        return await go_back(update, context)
    if not re.match(r'^\d+:[A-Za-z0-9_-]{35,}$', text):
        error_text = (
            "❌ Invalid token format!\n\n"
            "Token should look like:\n"
            "<code>123456789:ABCdefGHIjklMNOpqrsTUVwxyz</code>\n\n"
            "Get it from @BotFather"
        )
        await update.message.reply_text(error_text, parse_mode='HTML')
        return HOST_GET_TOKEN
    msg = await update.message.reply_text("🔍 Verifying token...")
    valid, username, name = await validate_bot_token(text)
    if not valid:
        await msg.edit_text("❌ Invalid or expired token. Please check and try again.")
        return HOST_GET_TOKEN
    context.user_data['token'] = text
    context.user_data['bot_username'] = username
    success_text = (
        f"✅ Token verified!\n\n"
        f"🤖 Bot: @{esc(username)}\n\n"
        f"Step 2: Now upload your Python (.py) file"
    )
    await msg.edit_text(success_text, parse_mode='HTML')
    return HOST_GET_FILE


async def host_get_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if update.message.text:
        text = update.message.text
        if "Back" in text or "Main Menu" in text or "🔙" in text or "🏠" in text:
            return await go_back(update, context)
        await update.message.reply_text("❌ Please upload a .py file, not text!")
        return HOST_GET_FILE
    if not update.message.document:
        await update.message.reply_text("❌ Please upload a Python file (.py)")
        return HOST_GET_FILE
    doc = update.message.document
    if not doc.file_name.endswith('.py'):
        await update.message.reply_text("❌ Only .py files are allowed!")
        return HOST_GET_FILE
    msg = await update.message.reply_text("📥 Downloading...")
    try:
        file = await doc.get_file()
        token = context.user_data['token']
        user_id = update.effective_user.id
        filename = f"{user_id}_{token.split(':')[0]}_{int(time.time())}.py"
        file_path = os.path.join(BOTS_DIR, filename)
        await file.download_to_drive(file_path)
        with open(file_path, 'r', encoding='utf-8') as f:
            code = f.read()
        valid, error = validate_python_code(code)
        if not valid:
            os.remove(file_path)
            await msg.edit_text(f"❌ Code Error:\n<code>{esc(error)}</code>", parse_mode='HTML')
            return HOST_GET_FILE
        if 'application' not in code:
            error_text = (
                "❌ Your code must define an <code>application</code> variable!\n\n"
                "Example:\n"
                "<code>application = Application.builder().token('TOKEN').build()</code>"
            )
            await msg.edit_text(error_text, parse_mode='HTML')
            return HOST_GET_FILE
        await msg.edit_text("⚙️ Deploying your bot...")
        save_bot(user_id, token, file_path, "upload", context.user_data.get('bot_username'))
        success, result = await start_user_bot(token, file_path)
        if success:
            success_text = (
                f"🚀 <b>Bot Deployed Successfully!</b>\n\n"
                f"🤖 @{esc(context.user_data.get('bot_username', 'your_bot'))}\n"
                f"📊 Status: Running 24/7\n\n"
                f"Try sending /start to your bot!"
            )
            await msg.edit_text(success_text, parse_mode='HTML')
        else:
            await msg.edit_text(f"❌ Deployment Failed:\n<code>{esc(result)}</code>", parse_mode='HTML')
        context.user_data.clear()
        await update.message.reply_text("What would you like to do next?", reply_markup=main_menu_kb())
        return MAIN_MENU
    except Exception as e:
        logger.error(f"Host file error: {e}")
        await msg.edit_text(f"❌ Error: {esc(str(e)[:100])}", parse_mode='HTML')
        return HOST_GET_FILE


# ==========================================
# CREATE BOT FLOW
# ==========================================
async def create_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['create'] = {}
    text = (
        "✨ <b>Create New Bot</b>\n\n"
        "I will generate a complete bot based on your description!\n\n"
        "Step 1: Send your Bot Token from @BotFather\n\n"
        "Do not have one? Create it:\n"
        "1. Open @BotFather\n"
        "2. Send /newbot\n"
        "3. Follow instructions\n"
        "4. Copy the token and send it here"
    )
    await update.message.reply_text(text, reply_markup=back_kb(), parse_mode='HTML')
    return CREATE_GET_TOKEN


async def create_get_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if "Back" in text or "Main Menu" in text or "🔙" in text or "🏠" in text:
        return await go_back(update, context)
    if not re.match(r'^\d+:[A-Za-z0-9_-]{35,}$', text):
        await update.message.reply_text("❌ Invalid token format. Please try again.")
        return CREATE_GET_TOKEN
    msg = await update.message.reply_text("🔍 Verifying...")
    valid, username, name = await validate_bot_token(text)
    if not valid:
        await msg.edit_text("❌ Invalid token. Please check and try again.")
        return CREATE_GET_TOKEN
    context.user_data['create']['token'] = text
    context.user_data['create']['username'] = username
    success_text = (
        f"✅ Token verified! Bot: @{esc(username)}\n\n"
        f"Step 2: <b>Describe your bot</b>\n\n"
        f"Tell me what you want your bot to do.\n"
        f"Be as detailed as possible!\n\n"
        f"<i>You can write in any language.</i>\n\n"
        f"Examples:\n"
        f"- A bot that tells jokes and fun facts\n"
        f"- A reminder bot with snooze feature\n"
        f"- A dictionary bot that translates words"
    )
    await msg.edit_text(success_text, parse_mode='HTML')
    return CREATE_GET_DESCRIPTION


async def create_get_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if "Back" in text or "Main Menu" in text or "🔙" in text or "🏠" in text:
        return await go_back(update, context)
    if len(text) < 10:
        await update.message.reply_text("📝 Please provide more details (at least 10 characters)")
        return CREATE_GET_DESCRIPTION
    context.user_data['create']['description'] = text
    await update.message.reply_text(
        "Step 3: How should users interact with your bot?",
        reply_markup=commands_type_kb()
    )
    return CREATE_COMMANDS_TYPE


async def create_commands_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    choice = query.data.replace("cmd_", "")
    context.user_data['create']['use_commands'] = choice in ['commands', 'both']
    context.user_data['create']['use_buttons'] = choice in ['buttons', 'both']
    await query.edit_message_text(
        "Step 4: Where will this bot be used?",
        reply_markup=chat_type_kb()
    )
    return CREATE_CHAT_TYPE


async def create_chat_type(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['create']['chat_type'] = query.data.replace("chat_", "")
    await query.edit_message_text(
        "Step 5: What language should the bot respond in?",
        reply_markup=language_kb()
    )
    return CREATE_LANGUAGE


async def create_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    lang = query.data.replace("lang_", "")
    if lang == "auto":
        lang = "same language as user message"
    context.user_data['create']['language'] = lang.title()
    await query.edit_message_text(
        "Step 6: Does your bot need to store user data?\n\n(e.g., preferences, scores, history)",
        reply_markup=yes_no_kb()
    )
    return CREATE_DATABASE


async def create_database(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    context.user_data['create']['use_database'] = query.data == "db_yes"
    await query.edit_message_text(
        "🔄 <b>Generating your bot with Gemini...</b>\n\nThis may take 10-30 seconds.\nPlease wait...",
        parse_mode='HTML'
    )
    data = context.user_data['create']
    code, error = await generate_bot_code(
        description=data['description'],
        token=data['token'],
        use_commands=data.get('use_commands', True),
        use_buttons=data.get('use_buttons', True),
        chat_type=data.get('chat_type', 'both'),
        language=data.get('language', 'English'),
        use_database=data.get('use_database', False)
    )
    if error:
        await query.edit_message_text(
            f"❌ Generation Failed:\n<code>{esc(error)}</code>",
            parse_mode='HTML'
        )
        await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text="What would you like to do?",
            reply_markup=main_menu_kb()
        )
        return MAIN_MENU
    await query.edit_message_text("💾 Saving code...", parse_mode='HTML')
    user_id = update.effective_user.id
    token = data['token']
    filename = f"{user_id}_{token.split(':')[0]}_gen_{int(time.time())}.py"
    file_path = os.path.join(BOTS_DIR, filename)
    with open(file_path, 'w', encoding='utf-8') as f:
        f.write(code)
    save_bot(user_id, token, file_path, "generated", data.get('username'))
    await query.edit_message_text("🚀 Deploying...", parse_mode='HTML')
    success, msg = await start_user_bot(token, file_path)
    if success:
        success_text = (
            f"🎉 <b>Your Bot is LIVE!</b>\n\n"
            f"🤖 @{esc(data.get('username', 'your_bot'))}\n"
            f"📊 Status: Running 24/7\n\n"
            f"Try sending /start to your bot!"
        )
        await query.edit_message_text(success_text, parse_mode='HTML')
        try:
            with open(file_path, 'rb') as f:
                await context.bot.send_document(
                    chat_id=update.effective_chat.id,
                    document=f,
                    filename=f"{data.get('username', 'bot')}_code.py",
                    caption="📄 Here is your bot source code!"
                )
        except Exception as e:
            logger.error(f"Failed to send code file: {e}")
    else:
        await query.edit_message_text(
            f"❌ Deployment Failed:\n<code>{esc(msg)}</code>",
            parse_mode='HTML'
        )
    context.user_data.clear()
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text="What would you like to do next?",
        reply_markup=main_menu_kb()
    )
    return MAIN_MENU


# ==========================================
# MY BOTS
# ==========================================
async def my_bots(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    bots = get_user_bots(user_id)
    if not bots:
        await update.message.reply_text(
            "📭 You have not hosted any bots yet.\n\n"
            "Click 'Host My Bot' or 'Create New Bot' to get started!",
            reply_markup=main_menu_kb()
        )
        return MAIN_MENU
    text = "📊 <b>Your Hosted Bots:</b>\n\n"
    buttons = []
    for bot in bots:
        is_active = bot['token'] in ACTIVE_BOTS
        status = "🟢 Running" if is_active else "🔴 Stopped"
        name = bot['bot_username'] or f"Bot-{bot['token'][:8]}"
        text += f"<b>@{esc(name)}</b>\n└ {status}\n\n"
        emoji = "🟢" if is_active else "🔴"
        buttons.append([InlineKeyboardButton(f"{emoji} @{name}", callback_data=f"view_{bot['token'][:25]}")])
    await update.message.reply_text(
        text,
        parse_mode='HTML',
        reply_markup=InlineKeyboardMarkup(buttons) if buttons else None
    )
    return MAIN_MENU


async def view_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    token_prefix = query.data.replace("view_", "")
    user_id = update.effective_user.id
    bots = get_user_bots(user_id)
    target = None
    for bot in bots:
        if bot['token'].startswith(token_prefix):
            target = bot
            break
    if not target:
        await query.edit_message_text("❌ Bot not found")
        return
    is_active = target['token'] in ACTIVE_BOTS
    status = "🟢 Running" if is_active else "🔴 Stopped"
    buttons = []
    if is_active:
        buttons.append([InlineKeyboardButton("🛑 Stop", callback_data=f"stop_{token_prefix}")])
        buttons.append([InlineKeyboardButton("🔄 Restart", callback_data=f"restart_{token_prefix}")])
    else:
        buttons.append([InlineKeyboardButton("▶️ Start", callback_data=f"start_{token_prefix}")])
    buttons.append([InlineKeyboardButton("🗑️ Delete", callback_data=f"delete_{token_prefix}")])
    buttons.append([InlineKeyboardButton("🔙 Back", callback_data="back_list")])
    text = (
        f"⚙️ <b>Manage Bot</b>\n\n"
        f"🤖 @{esc(target['bot_username'] or 'Unknown')}\n"
        f"📊 Status: {status}\n"
        f"📅 Created: {target['created_at'][:10]}\n"
        f"🔧 Type: {target['creation_type'].title()}"
    )
    await query.edit_message_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(buttons))


async def bot_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "back_list":
        await query.delete_message()
        return
    parts = query.data.split("_", 1)
    action = parts[0]
    token_prefix = parts[1] if len(parts) > 1 else ""
    user_id = update.effective_user.id
    bots = get_user_bots(user_id)
    target = None
    for bot in bots:
        if bot['token'].startswith(token_prefix):
            target = bot
            break
    if not target:
        await query.edit_message_text("❌ Bot not found")
        return
    if action == "stop":
        await query.edit_message_text("🛑 Stopping...")
        success, msg = await stop_user_bot(target['token'])
        result = "✅ Bot stopped!" if success else f"❌ Error: {msg}"
    elif action == "start":
        await query.edit_message_text("▶️ Starting...")
        success, msg = await start_user_bot(target['token'], target['file_path'])
        result = "✅ Bot started!" if success else f"❌ Error: {msg}"
    elif action == "restart":
        await query.edit_message_text("🔄 Restarting...")
        await stop_user_bot(target['token'])
        await asyncio.sleep(1)
        success, msg = await start_user_bot(target['token'], target['file_path'])
        result = "✅ Bot restarted!" if success else f"❌ Error: {msg}"
    elif action == "delete":
        await query.edit_message_text("🗑️ Deleting...")
        await stop_user_bot(target['token'])
        delete_bot_from_db(target['token'])
        if os.path.exists(target['file_path']):
            os.remove(target['file_path'])
        result = "✅ Bot deleted!"
    else:
        result = "Unknown action"
    await query.edit_message_text(result)


# ==========================================
# HELP
# ==========================================
async def help_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    help_text = (
        "🆘 <b>Help Center</b>\n\n"
        "<b>📤 Host My Bot</b>\n"
        "Upload your existing Python bot code.\n\n"
        "Requirements:\n"
        "- Python file (.py only)\n"
        "- Must have: <code>application = Application.builder().token(TOKEN).build()</code>\n"
        "- No run_polling() or run_webhook()\n\n"
        "<b>✨ Create New Bot</b>\n"
        "Tell me what you want and I will create it!\n\n"
        "- Describe your bot in any language\n"
        "- Answer a few quick questions\n"
        "- Get a working bot in seconds!\n\n"
        "<b>📊 My Bots</b>\n"
        "Manage your hosted bots:\n"
        "- Start/Stop bots\n"
        "- Restart bots\n"
        "- Delete bots\n\n"
        "<b>Need more help?</b>\n"
        "Send your question below:"
    )
    await update.message.reply_text(help_text, reply_markup=back_kb(), parse_mode='HTML')
    return HELP_GET_MESSAGE


async def help_get_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if "Back" in text or "Main Menu" in text or "🔙" in text or "🏠" in text:
        return await go_back(update, context)
    user = update.effective_user
    try:
        admin_text = (
            f"🆘 <b>Support Request</b>\n\n"
            f"👤 {esc(user.first_name)} (@{esc(user.username)})\n"
            f"🆔 <code>{user.id}</code>\n\n"
            f"📝 {esc(text)}"
        )
        await context.bot.send_message(chat_id=ADMIN_ID, text=admin_text, parse_mode='HTML')
        await update.message.reply_text(
            "✅ Message sent! We will get back to you soon.",
            reply_markup=main_menu_kb()
        )
    except Exception as e:
        logger.error(f"Failed to send help message: {e}")
        await update.message.reply_text(
            "❌ Failed to send. Please try again.",
            reply_markup=main_menu_kb()
        )
    return MAIN_MENU


# ==========================================
# ADMIN COMMANDS
# ==========================================
async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    stats = get_stats()
    active = len(ACTIVE_BOTS)
    text = (
        f"👑 <b>Admin Stats</b>\n\n"
        f"👥 Users: {stats['users']}\n"
        f"🤖 Total Bots: {stats['total_bots']}\n"
        f"🟢 Active: {active}\n"
        f"🔴 Inactive: {stats['total_bots'] - active}"
    )
    await update.message.reply_text(text, parse_mode='HTML')


async def admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    with get_db() as conn:
        users = conn.execute("SELECT * FROM users ORDER BY joined_at DESC LIMIT 20").fetchall()
    text = "👥 <b>Recent Users:</b>\n\n"
    for u in users:
        text += f"- {esc(u['first_name'])} (@{esc(u['username'])})\n"
    await update.message.reply_text(text[:4000], parse_mode='HTML')


async def admin_bots(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return
    with get_db() as conn:
        bots = conn.execute("SELECT * FROM bots ORDER BY created_at DESC LIMIT 20").fetchall()
    text = "🤖 <b>Recent Bots:</b>\n\n"
    for b in bots:
        status = "🟢" if b['token'] in ACTIVE_BOTS else "🔴"
        text += f"{status} @{esc(b['bot_username'] or 'Unknown')} (User: {b['user_id']})\n"
    await update.message.reply_text(text[:4000], parse_mode='HTML')


# ==========================================
# ERROR HANDLER
# ==========================================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error(f"Exception: {context.error}")
    if update and hasattr(update, 'effective_chat') and update.effective_chat:
        try:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text="⚠️ An error occurred. Please try again or use /start to restart.",
                reply_markup=main_menu_kb()
            )
        except:
            pass


# ==========================================
# WEBHOOK
# ==========================================
async def webhook_handler(request):
    token = request.match_info.get('token')
    try:
        data = await request.json()
    except:
        return web.Response(status=400)
    try:
        if token == PLATFORM_BOT_TOKEN:
            if platform_app:
                update = Update.de_json(data, platform_app.bot)
                await platform_app.process_update(update)
        elif token in ACTIVE_BOTS:
            app = ACTIVE_BOTS[token]
            update = Update.de_json(data, app.bot)
            await app.process_update(update)
    except Exception as e:
        logger.error(f"Webhook error: {e}")
    return web.Response(text="OK")


async def health_check(request):
    return web.Response(text=f"OK - {len(ACTIVE_BOTS)} bots running")


# ==========================================
# STARTUP
# ==========================================
async def restore_bots():
    logger.info("Restoring bots...")
    bots = get_all_running_bots()
    count = 0
    for token, path in bots:
        if os.path.exists(path):
            success, _ = await start_user_bot(token, path)
            if success:
                count += 1
    logger.info(f"Restored {count}/{len(bots)} bots")


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelled", reply_markup=main_menu_kb())
    return MAIN_MENU


def main():
    global platform_app
    init_db()
    logger.info("Starting Bot Hosting Platform...")
    request = HTTPXRequest(connection_pool_size=8, connect_timeout=30.0, read_timeout=30.0)
    platform_app = Application.builder().token(PLATFORM_BOT_TOKEN).request(request).build()
    platform_app.add_error_handler(error_handler)
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            MessageHandler(filters.Regex(r"^(📤|✨|📊|🆘|🔙|🏠)"), handle_menu),
        ],
        states={
            MAIN_MENU: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu)],
            HOST_GET_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, host_get_token)],
            HOST_GET_FILE: [MessageHandler(filters.ALL, host_get_file)],
            CREATE_GET_TOKEN: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_get_token)],
            CREATE_GET_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, create_get_description)],
            CREATE_COMMANDS_TYPE: [CallbackQueryHandler(create_commands_type, pattern=r"^cmd_")],
            CREATE_CHAT_TYPE: [CallbackQueryHandler(create_chat_type, pattern=r"^chat_")],
            CREATE_LANGUAGE: [CallbackQueryHandler(create_language, pattern=r"^lang_")],
            CREATE_DATABASE: [CallbackQueryHandler(create_database, pattern=r"^db_")],
            HELP_GET_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, help_get_message)],
        },
        fallbacks=[CommandHandler("cancel", cancel), CommandHandler("start", start)],
        allow_reentry=True,
        per_message=False,
    )
    platform_app.add_handler(conv)
    platform_app.add_handler(CommandHandler("stats", admin_stats))
    platform_app.add_handler(CommandHandler("users", admin_users))
    platform_app.add_handler(CommandHandler("bots", admin_bots))
    platform_app.add_handler(CallbackQueryHandler(view_bot, pattern=r"^view_"))
    platform_app.add_handler(CallbackQueryHandler(bot_action, pattern=r"^(stop|start|restart|delete|back)_?"))
    web_app = web.Application()
    web_app.router.add_post('/bot/{token}', webhook_handler)
    web_app.router.add_get('/health', health_check)
    web_app.router.add_get('/', health_check)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def run():
        await platform_app.initialize()
        await platform_app.start()
        await platform_app.bot.set_webhook(f"{RENDER_EXTERNAL_URL}/bot/{PLATFORM_BOT_TOKEN}")
        logger.info("Webhook set!")
        await restore_bots()
        runner = web.AppRunner(web_app)
        await runner.setup()
        port = int(os.environ.get("PORT", 8080))
        await web.TCPSite(runner, '0.0.0.0', port).start()
        logger.info(f"Server running on port {port}")
        await asyncio.Event().wait()

    try:
        loop.run_until_complete(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
