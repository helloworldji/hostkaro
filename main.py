"""
TELEGRAM BOT HOSTING PLATFORM - VERIFIED EDITION (v4.1)
Host Python Telegram bots for FREE - 24/7
Powered by Google Gemini 2.0 Flash
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
import json
from datetime import datetime
from contextlib import contextmanager
from typing import Optional, Tuple, Dict, List, Any
from io import BytesIO

# ==========================================
# SAFE IMPORT FOR DOTENV
# ==========================================
try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(): pass

from aiohttp import web, ClientSession, ClientTimeout
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove
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
from telegram.error import Forbidden, BadRequest, InvalidToken

# ==========================================
# CONFIGURATION
# ==========================================
load_dotenv()

ADMIN_ID = 8175884349
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent"
PLATFORM_BOT_TOKEN = "8066184862:AAGxPAHFcwQAmEt9fsAuyZG8DUPt8A-01fY"

# AUTO-DETECT RENDER URL (Fallback if env var missing)
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL", "https://hostkaro.onrender.com")

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
    CREATE_INITIAL_IDEA,
    CREATE_CONSULTATION,
    HELP_GET_MESSAGE,
    BROADCAST_MSG,
    ADMIN_REPLY_MSG
) = range(9)


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
            is_blocked INTEGER DEFAULT 0,
            update_count INTEGER DEFAULT 0,
            FOREIGN KEY (user_id) REFERENCES users(user_id)
        )
    """)
    try:
        c.execute("SELECT is_blocked FROM bots LIMIT 1")
    except sqlite3.OperationalError:
        c.execute("ALTER TABLE bots ADD COLUMN is_blocked INTEGER DEFAULT 0")
    try:
        c.execute("SELECT update_count FROM bots LIMIT 1")
    except sqlite3.OperationalError:
        c.execute("ALTER TABLE bots ADD COLUMN update_count INTEGER DEFAULT 0")
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
        now = datetime.now().isoformat()
        conn.execute(
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
        now = datetime.now().isoformat()
        conn.execute(
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


def get_all_bots_admin():
    with get_db() as conn:
        return conn.execute("SELECT * FROM bots ORDER BY created_at DESC").fetchall()


def update_bot_status(token: str, status: str, error: str = None):
    with get_db() as conn:
        if error:
            conn.execute("UPDATE bots SET status = ?, error_log = ? WHERE token = ?", (status, error, token))
        else:
            conn.execute("UPDATE bots SET status = ? WHERE token = ?", (status, token))


def increment_bot_update_count(token: str):
    with get_db() as conn:
        conn.execute("UPDATE bots SET update_count = update_count + 1 WHERE token = ?", (token,))


def toggle_bot_block(token: str) -> bool:
    with get_db() as conn:
        current = conn.execute("SELECT is_blocked FROM bots WHERE token = ?", (token,)).fetchone()[0]
        new_status = 0 if current else 1
        conn.execute("UPDATE bots SET is_blocked = ? WHERE token = ?", (new_status, token))
        return bool(new_status)


def get_all_running_bots():
    with get_db() as conn:
        return conn.execute("SELECT token, file_path, is_blocked FROM bots WHERE status = 'running'").fetchall()


def delete_bot_from_db(token: str):
    with get_db() as conn:
        conn.execute("DELETE FROM bots WHERE token = ?", (token,))


def get_stats():
    with get_db() as conn:
        c = conn.cursor()
        users = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        bots = c.execute("SELECT COUNT(*) FROM bots").fetchone()[0]
        blocked = c.execute("SELECT COUNT(*) FROM bots WHERE is_blocked = 1").fetchone()[0]
        return {"users": users, "total_bots": bots, "blocked": blocked}


# ==========================================
# VALIDATION
# ==========================================
def validate_python_code(code: str) -> Tuple[bool, str]:
    # Basic syntax check only
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
        'requests': 'requests',
        'numpy': 'numpy'
    }
    to_install = []
    for lib in imports:
        if lib in stdlib: continue
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
        
        # --- VERIFICATION STEP 1: TEST CONNECTION ---
        try:
            # We try to fetch bot info. If this fails, the token is bad or blocked.
            me = await user_app.bot.get_me()
            logger.info(f"Bot connected: @{me.username}")
        except Exception as conn_err:
            return False, f"Connection Failed: {conn_err}"

        # --- VERIFICATION STEP 2: START APP ---
        await user_app.initialize()
        await user_app.start()
        
        # --- VERIFICATION STEP 3: SET WEBHOOK ---
        webhook_url = f"{RENDER_EXTERNAL_URL}/bot/{token}"
        try:
            await user_app.bot.set_webhook(url=webhook_url)
            logger.info(f"Webhook set: {webhook_url}")
        except Exception as wh_err:
            return False, f"Webhook Failed: {wh_err}"
        
        ACTIVE_BOTS[token] = user_app
        update_bot_status(token, "running")
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
            except: pass
            await app.stop()
            await app.shutdown()
            del ACTIVE_BOTS[token]
        update_bot_status(token, "stopped")
        return True, "Bot stopped"
    except Exception as e:
        return False, str(e)


# ==========================================
# NON-TECHNICAL AI ENGINE (FIXED PROMPT)
# ==========================================
async def consult_gemini_analyst(current_info: str, history: List[Dict]) -> Dict[str, Any]:
    if not GEMINI_API_KEY:
        return {"question": "System Error: API Key missing.", "options": ["Contact Admin"], "refined_summary": current_info}

    prompt = f"""You are a helpful Product Manager helping a user create a Telegram bot.
    Current Idea: {current_info}
    History: {json.dumps(history)}
    RULES: Ask ONE simple question. Output JSON ONLY.
    {{ "question": "...", "options": ["...", "..."], "refined_summary": "..." }}
    """
    
    headers = { "Content-Type": "application/json", "X-goog-api-key": GEMINI_API_KEY }
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": { "responseMimeType": "application/json" }
    }
    
    try:
        async with ClientSession() as session:
            async with session.post(GEMINI_API_URL, json=payload, headers=headers) as resp:
                if resp.status != 200: return {"question": "Ready?", "options": ["Build"], "refined_summary": current_info}
                result = await resp.json()
                if 'candidates' not in result: return {"question": "Build now?", "options": ["Yes"], "refined_summary": current_info}
                return json.loads(result['candidates'][0]['content']['parts'][0]['text'])
    except: return {"question": "Build now?", "options": ["Yes"], "refined_summary": current_info}

async def generate_final_code(summary: str, token: str) -> Tuple[Optional[str], Optional[str]]:
    if not GEMINI_API_KEY: return None, "API Key missing"

    prompt = f"""You are an expert Python developer. Generate a complete, production-ready Telegram bot.
    
    DESCRIPTION: {summary}
    TOKEN: {token}
    
    CRITICAL TECHNICAL RULES:
    1. Define global variable: application = Application.builder().token("{token}").build()
    2. Register all handlers IMMEDIATELY after creating 'application'.
    3. DO NOT wrap handler registration in 'if __name__ == "__main__":'
    4. DO NOT use application.run_polling() or run_webhook().
    5. Return ONLY raw Python code.
    """
    
    headers = { "Content-Type": "application/json", "X-goog-api-key": GEMINI_API_KEY }
    payload = { "contents": [{"parts": [{"text": prompt}]}], "generationConfig": { "temperature": 0.5 } }
    
    try:
        async with ClientSession() as session:
            async with session.post(GEMINI_API_URL, json=payload, headers=headers) as resp:
                result = await resp.json()
                if 'candidates' not in result: return None, "AI Blocked"
                content = result['candidates'][0]['content']['parts'][0]['text']
                code = re.sub(r'^```python\s*\n?', '', content)
                code = re.sub(r'^```\s*\n?', '', code)
                code = re.sub(r'\n?```$', '', code).strip()
                
                valid, error = validate_python_code(code)
                if not valid: return None, f"Syntax Error: {error}"
                if 'application' not in code: return None, "Missing 'application' object"
                return code, None
    except Exception as e: return None, str(e)


# ==========================================
# HELPER & KEYBOARDS
# ==========================================
def esc(text) -> str:
    if text is None: return ""
    return html.escape(str(text))

def main_menu_kb(user_id) -> ReplyKeyboardMarkup:
    keyboard = [["✨ Create Bot", "📤 Host Bot"], ["📊 My Bots", "🆘 Help"]]
    if user_id == ADMIN_ID: keyboard.append(["🔐 Admin Panel"]) 
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

def back_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup([["🔙 Back", "🏠 Main Menu"]], resize_keyboard=True)

# ==========================================
# HANDLERS - MAIN
# ==========================================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    save_user(user.id, user.username, user.first_name)
    context.user_data.clear()
    
    if user.id == ADMIN_ID:
        text = f"👋 <b>Welcome Admin!</b>\n\nI have added the 🔐 <b>Admin Panel</b> button below."
    else:
        text = (
            f"👋 Welcome <b>{esc(user.first_name)}</b>!\n\n"
            f"🤖 <b>AI Bot Builder & Hosting</b>\n\n"
            f"I can build complex bots for you and host them for FREE.\n"
            f"Just tell me your idea!"
        )
    
    await update.message.reply_text(text, reply_markup=main_menu_kb(user.id), parse_mode='HTML')
    return MAIN_MENU

async def handle_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    user = update.effective_user
    
    if "Host" in text or "📤" in text: return await host_start(update, context)
    elif "Create" in text or "✨" in text: return await create_start(update, context)
    elif "My Bots" in text or "📊" in text: return await my_bots(update, context)
    elif "Help" in text or "🆘" in text: return await help_start(update, context)
    elif "Admin" in text or "🔐" in text:
        if user.id == ADMIN_ID: return await admin_panel(update, context)
    
    await update.message.reply_text("Please choose an option:", reply_markup=main_menu_kb(user.id))
    return MAIN_MENU

async def go_back(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.clear()
    return await start(update, context)

# ==========================================
# HOST BOT FLOW
# ==========================================
async def host_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("📤 <b>Host Your Bot</b>\n\nSend your Bot Token from @BotFather:", reply_markup=back_kb(), parse_mode='HTML')
    return HOST_GET_TOKEN

async def host_get_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if "Back" in text or "Main Menu" in text: return await go_back(update, context)
    
    msg = await update.message.reply_text("🔍 Verifying token...")
    valid, username, name = await validate_bot_token(text)
    if not valid:
        await msg.edit_text("❌ Invalid token. Try again.")
        return HOST_GET_TOKEN
    context.user_data['token'] = text
    context.user_data['bot_username'] = username
    await msg.edit_text(f"✅ Verified: @{username}\n\nNow upload your <b>.py</b> file.", parse_mode='HTML')
    return HOST_GET_FILE

async def host_get_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.message.document:
        await update.message.reply_text("❌ Please upload a Python file.")
        return HOST_GET_FILE
    doc = update.message.document
    if not doc.file_name.endswith('.py'):
        await update.message.reply_text("❌ Only .py files allowed.")
        return HOST_GET_FILE
    
    msg = await update.message.reply_text("📥 Deploying...")
    file = await doc.get_file()
    token = context.user_data['token']
    user_id = update.effective_user.id
    filename = f"{user_id}_{token.split(':')[0]}_{int(time.time())}.py"
    file_path = os.path.join(BOTS_DIR, filename)
    await file.download_to_drive(file_path)
    
    with open(file_path, 'r') as f: code = f.read()
    valid, error = validate_python_code(code)
    
    if not valid or 'application' not in code:
        os.remove(file_path)
        await msg.edit_text(f"❌ Error: {error or 'No application object found'}")
        return HOST_GET_FILE
    
    save_bot(user_id, token, file_path, "upload", context.user_data.get('bot_username'))
    success, res = await start_user_bot(token, file_path)
    
    if success:
        await msg.edit_text(f"🚀 <b>Bot Deployed!</b>\n\n@{context.user_data.get('bot_username')}\nRunning: 🟢", parse_mode='HTML')
    else:
        await msg.edit_text(f"❌ Failed: {res}")
    return MAIN_MENU

# ==========================================
# AI CREATE BOT FLOW
# ==========================================
async def create_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data['create'] = {}
    await update.message.reply_text("✨ <b>AI Bot Builder</b>\n\nFirst, send your Bot Token from @BotFather:", reply_markup=back_kb(), parse_mode='HTML')
    return CREATE_GET_TOKEN

async def create_get_token(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    if "Back" in text or "Main Menu" in text: return await go_back(update, context)
    
    msg = await update.message.reply_text("🔍 Verifying...")
    valid, username, name = await validate_bot_token(text)
    if not valid:
        await msg.edit_text("❌ Invalid token.")
        return CREATE_GET_TOKEN
        
    context.user_data['create']['token'] = text
    context.user_data['create']['username'] = username
    context.user_data['create']['history'] = []
    context.user_data['create']['question_count'] = 0
    
    await msg.edit_text(f"✅ <b>Target: @{username}</b>\n\n💡 <b>What is your idea?</b>\nTell me what you want the bot to do.", parse_mode='HTML')
    return CREATE_INITIAL_IDEA

async def create_initial_idea(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    idea = update.message.text
    if "Back" in idea or "Main Menu" in idea: return await go_back(update, context)
    
    context.user_data['create']['summary'] = idea
    context.user_data['create']['history'].append({"role": "user", "content": idea})
    await update.message.reply_text("🤔 <b>Thinking...</b>", parse_mode='HTML')
    return await create_consultation_loop(update, context)

async def create_consultation_loop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data['create']
    if data['question_count'] >= 3:
        await start_build_process(update, context)
        return MAIN_MENU
        
    ai_response = await consult_gemini_analyst(data['summary'], data['history'])
    data['summary'] = ai_response.get('refined_summary', data['summary'])
    data['question_count'] += 1
    
    question = ai_response.get('question', "What else?")
    options = ai_response.get('options', ["Continue"])
    
    keyboard = []
    row = []
    for opt in options:
        row.append(InlineKeyboardButton(opt, callback_data=f"ans_{opt[:20]}"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row: keyboard.append(row)
    keyboard.append([InlineKeyboardButton("✍️ Custom Answer", callback_data="ans_custom")])
    keyboard.append([InlineKeyboardButton("🚀 Build Now", callback_data="ans_done")])
    
    func = update.callback_query.edit_message_text if update.callback_query else update.message.reply_text
    await func(f"🤖 <b>Bot Architect ({data['question_count']}/3)</b>\n\n{question}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='HTML')
    return CREATE_CONSULTATION

async def create_handle_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    answer = query.data.replace("ans_", "")
    
    if answer == "done":
        await start_build_process(update, context)
        return MAIN_MENU
    if answer == "custom":
        await query.edit_message_text("✍️ <b>Type your answer below:</b>", parse_mode='HTML')
        return CREATE_CONSULTATION 
        
    context.user_data['create']['history'].append({"role": "user", "content": answer})
    await query.edit_message_text(f"✅ Selected: <b>{answer}</b>\nThinking...", parse_mode='HTML')
    return await create_consultation_loop(update, context)

async def create_handle_text_answer(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text
    context.user_data['create']['history'].append({"role": "user", "content": text})
    await update.message.reply_text("✅ <b>Got it.</b> Thinking...", parse_mode='HTML')
    return await create_consultation_loop(update, context)

async def start_build_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg_func = update.callback_query.edit_message_text if update.callback_query else update.message.reply_text
    msg = await msg_func("🏗️ <b>Blueprint Complete!</b>\n\nCoding your bot now... (approx 20s)", parse_mode='HTML')
    
    data = context.user_data['create']
    code, error = await generate_final_code(data['summary'], data['token'])
    
    if error:
        await msg.edit_text(f"❌ Coding Failed:\n{error}")
        return
        
    user_id = update.effective_user.id
    token = data['token']
    filename = f"{user_id}_{token.split(':')[0]}_ai_{int(time.time())}.py"
    file_path = os.path.join(BOTS_DIR, filename)
    
    with open(file_path, 'w', encoding='utf-8') as f: f.write(code)
    save_bot(user_id, token, file_path, "ai_generated", data['username'])
    
    await msg.edit_text("🚀 Deploying to server...", parse_mode='HTML')
    success, res = await start_user_bot(token, file_path)
    
    if success:
        await msg.edit_text(f"🎉 <b>Bot Launched!</b>\n\n🤖 @{data['username']}\nStatus: 🟢 Online\n\nTry sending /start to it!", parse_mode='HTML')
    else:
        await msg.edit_text(f"❌ Deployment Error: {res}")

# ==========================================
# ADMIN & MANAGEMENT
# ==========================================
async def my_bots(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    bots = get_user_bots(user_id)
    if not bots:
        await update.message.reply_text("📭 You have 0 bots.", reply_markup=main_menu_kb(user_id))
        return MAIN_MENU
    text = "📊 <b>Your Bots:</b>\n"
    buttons = []
    for bot in bots:
        status = "🟢" if bot['token'] in ACTIVE_BOTS else "🔴"
        if bot['is_blocked']: status = "🚫"
        name = bot['bot_username'] or "Bot"
        buttons.append([InlineKeyboardButton(f"{status} @{name}", callback_data=f"view_{bot['token'][:10]}")])
    await update.message.reply_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(buttons))
    return MAIN_MENU

async def view_bot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    prefix = query.data.replace("view_", "")
    with get_db() as conn:
        bot = conn.execute("SELECT * FROM bots WHERE token LIKE ?", (f"{prefix}%",)).fetchone()
    if not bot: return
    
    status = "🟢 Online" if bot['token'] in ACTIVE_BOTS else "🔴 Offline"
    if bot['is_blocked']: status = "🚫 Blocked"
    
    text = f"🤖 <b>@{esc(bot['bot_username'])}</b>\nStatus: {status}\nUpdates: {bot['update_count']}"
    btns = []
    if not bot['is_blocked']:
        if bot['token'] in ACTIVE_BOTS:
            btns.append([InlineKeyboardButton("🛑 Stop", callback_data=f"stop_{prefix}"), InlineKeyboardButton("🔄 Restart", callback_data=f"restart_{prefix}")])
        else:
            btns.append([InlineKeyboardButton("▶️ Start", callback_data=f"start_{prefix}")])
    btns.append([InlineKeyboardButton("📜 Error Logs", callback_data=f"logs_{prefix}")])
    btns.append([InlineKeyboardButton("🗑️ Delete", callback_data=f"delete_{prefix}")])
    btns.append([InlineKeyboardButton("🔙 Back", callback_data="back_list")])
    try: await query.edit_message_text(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(btns))
    except BadRequest: pass

async def bot_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.data == "back_list":
        await query.delete_message()
        return
    
    action, prefix = query.data.split("_", 1)
    with get_db() as conn:
        bot = conn.execute("SELECT * FROM bots WHERE token LIKE ?", (f"{prefix}%",)).fetchone()
    if not bot: return
    
    token = bot['token']
    if action == "stop": await stop_user_bot(token); msg = "🛑 Stopped."
    elif action == "start": s, _ = await start_user_bot(token, bot['file_path']); msg = "✅ Started." if s else "❌ Error."
    elif action == "restart": await stop_user_bot(token); await asyncio.sleep(1); await start_user_bot(token, bot['file_path']); msg = "🔄 Restarted."
    elif action == "delete": await stop_user_bot(token); delete_bot_from_db(token); msg = "🗑️ Deleted."
    
    await query.answer(msg)
    await view_bot(update, context)

async def view_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    prefix = query.data.replace("logs_", "")
    with get_db() as conn:
        bot = conn.execute("SELECT error_log FROM bots WHERE token LIKE ?", (f"{prefix}%",)).fetchone()
    if bot and bot['error_log']:
        if len(bot['error_log']) > 200:
            bio = BytesIO(bot['error_log'].encode())
            bio.name = "error.txt"
            await context.bot.send_document(update.effective_chat.id, bio)
        else: await query.answer(f"Log: {bot['error_log']}", show_alert=True)
    else: await query.answer("✅ No errors.", show_alert=True)

# ==========================================
# ADMIN & SYSTEM HANDLERS
# ==========================================
async def help_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    await update.message.reply_text("🆘 Type your message for admin:", reply_markup=back_kb())
    return HELP_GET_MESSAGE

async def help_send(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    reply_btn = InlineKeyboardMarkup([[InlineKeyboardButton("↩️ Reply", callback_data=f"reply_{user.id}")]])
    await context.bot.send_message(ADMIN_ID, f"📩 <b>Support:</b>\n{esc(update.message.text)}\nFrom: {user.id}", parse_mode='HTML', reply_markup=reply_btn)
    await update.message.reply_text("✅ Sent!", reply_markup=main_menu_kb(user.id))
    return MAIN_MENU

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    stats = get_stats()
    text = f"🔐 <b>Admin</b>\nUsers: {stats['users']} | Bots: {stats['total_bots']}\nActive: {len(ACTIVE_BOTS)}"
    kb = [[InlineKeyboardButton("📜 List Bots", callback_data="admin_list"), InlineKeyboardButton("📢 Broadcast", callback_data="admin_cast")]]
    func = update.callback_query.edit_message_text if update.callback_query else update.message.reply_text
    try: await func(text, parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))
    except BadRequest: pass

async def admin_reply_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    context.user_data['reply_target'] = int(update.callback_query.data.split("_")[1])
    await update.callback_query.message.reply_text("✍️ Type reply:")
    return ADMIN_REPLY_MSG

async def admin_reply_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await context.bot.send_message(context.user_data['reply_target'], f"📨 <b>Admin Reply:</b>\n{update.message.text}", parse_mode='HTML')
        await update.message.reply_text("✅ Sent.")
    except: await update.message.reply_text("❌ Failed.")
    return MAIN_MENU

async def admin_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bots = get_all_bots_admin()[:20]
    kb = []
    for b in bots:
        kb.append([InlineKeyboardButton(f"@{b['bot_username']}", callback_data=f"abot_{b['token'][:10]}")])
    kb.append([InlineKeyboardButton("🔙", callback_data="admin_panel")])
    await update.callback_query.edit_message_text("📜 <b>Bots</b>", parse_mode='HTML', reply_markup=InlineKeyboardMarkup(kb))

async def admin_bot_view(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prefix = update.callback_query.data.replace("abot_", "")
    kb = [[InlineKeyboardButton("Delete", callback_data=f"adel_{prefix}"), InlineKeyboardButton("🔙", callback_data="admin_list")]]
    await update.callback_query.edit_message_text(f"Bot: {prefix}...", reply_markup=InlineKeyboardMarkup(kb))

async def admin_action(update: Update, context: ContextTypes.DEFAULT_TYPE):
    prefix = update.callback_query.data.split("_")[1]
    with get_db() as conn:
        bot = conn.execute("SELECT * FROM bots WHERE token LIKE ?", (f"{prefix}%",)).fetchone()
    if bot:
        await stop_user_bot(bot['token'])
        delete_bot_from_db(bot['token'])
    await admin_list(update, context)

async def admin_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("📢 Send broadcast msg:")
    return BROADCAST_MSG

async def admin_broadcast_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    with get_db() as conn:
        users = conn.execute("SELECT user_id FROM users").fetchall()
    for u in users:
        try: await context.bot.send_message(u[0], update.message.text)
        except: pass
    await update.message.reply_text(f"✅ Broadcasted to {len(users)} users.")
    return MAIN_MENU

async def webhook_handler(request):
    token = request.match_info.get('token')
    try:
        data = await request.json()
        if token == PLATFORM_BOT_TOKEN and platform_app:
            await platform_app.process_update(Update.de_json(data, platform_app.bot))
        elif token in ACTIVE_BOTS:
            increment_bot_update_count(token)
            await ACTIVE_BOTS[token].process_update(Update.de_json(data, ACTIVE_BOTS[token].bot))
        return web.Response(text="OK")
    except Exception as e:
        logger.error(f"Webhook Error: {e}")
        return web.Response(status=400)

async def restore_bots():
    bots = get_all_running_bots()
    logger.info(f"Restoring {len(bots)} bots...")
    for t, p, b in bots:
        if not b and os.path.exists(p): await start_user_bot(t, p)

def main():
    global platform_app
    init_db()
    req = HTTPXRequest(connection_pool_size=20)
    platform_app = Application.builder().token(PLATFORM_BOT_TOKEN).request(req).build()
    
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start), CommandHandler("admin", admin_panel), MessageHandler(filters.Regex(r"^(📤|✨|📊|🆘|🔐)"), handle_menu)],
        states={
            MAIN_MENU: [MessageHandler(filters.TEXT, handle_menu)],
            HOST_GET_TOKEN: [MessageHandler(filters.TEXT, host_get_token)],
            HOST_GET_FILE: [MessageHandler(filters.ALL, host_get_file)],
            CREATE_GET_TOKEN: [MessageHandler(filters.TEXT, create_get_token)],
            CREATE_INITIAL_IDEA: [MessageHandler(filters.TEXT, create_initial_idea)],
            CREATE_CONSULTATION: [CallbackQueryHandler(create_handle_answer, pattern="^ans_"), MessageHandler(filters.TEXT & ~filters.COMMAND, create_handle_text_answer)],
            HELP_GET_MESSAGE: [MessageHandler(filters.TEXT, help_send)],
            BROADCAST_MSG: [MessageHandler(filters.TEXT, admin_broadcast_send)],
            ADMIN_REPLY_MSG: [MessageHandler(filters.TEXT, admin_reply_send)]
        },
        fallbacks=[CommandHandler("start", start)]
    )
    
    platform_app.add_handler(conv)
    platform_app.add_handler(CallbackQueryHandler(admin_panel, pattern="^admin_panel"))
    platform_app.add_handler(CallbackQueryHandler(admin_list, pattern="^admin_list"))
    platform_app.add_handler(CallbackQueryHandler(admin_broadcast_start, pattern="^admin_cast"))
    platform_app.add_handler(CallbackQueryHandler(admin_bot_view, pattern="^abot_"))
    platform_app.add_handler(CallbackQueryHandler(admin_action, pattern="^(ablock|adel)_"))
    platform_app.add_handler(CallbackQueryHandler(view_bot, pattern="^view_"))
    platform_app.add_handler(CallbackQueryHandler(view_logs, pattern="^logs_"))
    platform_app.add_handler(CallbackQueryHandler(bot_action, pattern="^(stop|start|restart|delete|back)_"))
    platform_app.add_handler(CallbackQueryHandler(admin_reply_start, pattern="^reply_"))
    
    app = web.Application()
    app.router.add_post('/bot/{token}', webhook_handler)
    app.router.add_get('/', lambda r: web.Response(text="Running"))
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    async def runner():
        await platform_app.initialize()
        await platform_app.start()
        # SET PLATFORM WEBHOOK
        url = f"{RENDER_EXTERNAL_URL}/bot/{PLATFORM_BOT_TOKEN}"
        logger.info(f"Setting Platform Webhook: {url}")
        await platform_app.bot.set_webhook(url)
        await restore_bots()
        
        server = web.AppRunner(app)
        await server.setup()
        await web.TCPSite(server, '0.0.0.0', int(os.environ.get("PORT", 8080))).start()
        await asyncio.Event().wait()
        
    try: loop.run_until_complete(runner())
    except KeyboardInterrupt: pass

if __name__ == "__main__":
    main()
