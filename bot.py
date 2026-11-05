import asyncio
import logging
import os
import sqlite3
import threading
import time
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ChatJoinRequestHandler,
)

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("keybot")

# ---- Config (sab Render ke Environment Variables se aata hai) ----
BOT_TOKEN = os.getenv("BOT_TOKEN", "9254843564:AAGbiKXcpzmLRLQIH8DBwcaYpqwrBc1S_5M").strip()
CHANNEL_ID_RAW = os.getenv("CHANNEL_ID", "-1003245718111").strip()
CHANNEL_INVITE_URL = os.getenv("CHANNEL_INVITE_URL", "https://t.me/+IYpwhe6tiX80MzY9").strip()
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "7112911083").strip()
STORAGE_CHANNEL_ID_RAW = os.getenv("STORAGE_CHANNEL_ID", "-1004402808732").strip()
DATABASE_PATH = os.getenv("DATABASE_PATH", "bot.db").strip()
PORT = int(os.getenv("PORT", "10000"))
COOLDOWN_SECONDS = 24 * 60 * 60

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN environment variable.")
if not CHANNEL_ID_RAW:
    raise RuntimeError("Missing CHANNEL_ID environment variable.")
if not ADMIN_IDS_RAW:
    raise RuntimeError("Missing ADMIN_IDS environment variable.")

try:
    CHANNEL_ID = int(CHANNEL_ID_RAW)
    ADMIN_IDS = {int(x.strip()) for x in ADMIN_IDS_RAW.split(",") if x.strip()}
    STORAGE_CHANNEL_ID = int(STORAGE_CHANNEL_ID_RAW) if STORAGE_CHANNEL_ID_RAW else None
except ValueError as exc:
    raise RuntimeError("CHANNEL_ID, ADMIN_IDS and STORAGE_CHANNEL_ID must be numeric Telegram IDs.") from exc

db_parent = Path(DATABASE_PATH).parent
db_parent.mkdir(parents=True, exist_ok=True)


# ---- Render health server (Web Service ko port bind karna zaroori hai) ----
class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"OK")

    def do_HEAD(self):
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args):
        pass


def start_health_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    log.info("Health server listening on port %s", PORT)


# ---- Database ----
def connect_db():
    conn = sqlite3.connect(DATABASE_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    with connect_db() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            last_key_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS keys (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            key_value TEXT NOT NULL UNIQUE,
            assigned_to INTEGER,
            assigned_at INTEGER
        );
        CREATE TABLE IF NOT EXISTS join_requests (
            user_id INTEGER PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            requested_at INTEGER NOT NULL
        );
        """)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# ---- Data store channel: DB ka backup private channel mein (pinned file) ----
_state = {"dirty": False, "last_backup_msg": None}


def mark_dirty():
    _state["dirty"] = True


async def restore_db(bot):
    """Local DB nahi mili (free plan restart) to storage channel ke pinned backup se restore karo."""
    if not STORAGE_CHANNEL_ID:
        log.warning("STORAGE_CHANNEL_ID not set - backups disabled.")
        return
    if Path(DATABASE_PATH).exists():
        log.info("Local DB found, restore skipped.")
        return
    try:
        chat = await bot.get_chat(STORAGE_CHANNEL_ID)
        pinned = chat.pinned_message
        if pinned and pinned.document:
            tg_file = await bot.get_file(pinned.document.file_id)
            await tg_file.download_to_drive(DATABASE_PATH)
            _state["last_backup_msg"] = pinned.message_id
            log.info("Database restored from storage channel.")
        else:
            log.info("No pinned backup in storage channel - starting fresh.")
    except TelegramError as exc:
        log.error("Restore failed: %s", exc)


async def do_backup(bot):
    if not STORAGE_CHANNEL_ID:
        return
    tmp = DATABASE_PATH + ".backup"
    try:
        src = connect_db()
        dst = sqlite3.connect(tmp)
        src.backup(dst)
        dst.close()
        src.close()
        with open(tmp, "rb") as f:
            msg = await bot.send_document(
                STORAGE_CHANNEL_ID, f, filename="bot.db",
                caption=time.strftime("Backup %Y-%m-%d %H:%M:%S UTC", time.gmtime()),
            )
        try:
            await bot.pin_chat_message(STORAGE_CHANNEL_ID, msg.message_id, disable_notification=True)
        except TelegramError as exc:
            log.warning("Pin failed (bot ko Pin Messages permission do): %s", exc)
        old = _state["last_backup_msg"]
        if old and old != msg.message_id:
            try:
                await bot.delete_message(STORAGE_CHANNEL_ID, old)
            except TelegramError:
                pass
        _state["last_backup_msg"] = msg.message_id
        log.info("Backup saved to storage channel.")
    except Exception as exc:
        _state["dirty"] = True
        log.error("Backup failed: %s", exc)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


async def backup_job(context: ContextTypes.DEFAULT_TYPE):
    if _state["dirty"]:
        _state["dirty"] = False
        await do_backup(context.bot)


async def post_init(app: Application):
    await restore_db(app.bot)
    init_db()


async def post_stop(app: Application):
    if _state["dirty"]:
        _state["dirty"] = False
        await do_backup(app.bot)


def main_keyboard(is_member: bool = False):
    if is_member:
        return InlineKeyboardMarkup([
            [InlineKeyboardButton("🔑 Generate Key", callback_data="generate_key")]
        ])
    rows = []
    if CHANNEL_INVITE_URL:
        rows.append([InlineKeyboardButton("📢 Join Private Channel", url=CHANNEL_INVITE_URL)])
    rows.append([InlineKeyboardButton("✅ Verify", callback_data="verify_join")])
    return InlineKeyboardMarkup(rows)


async def check_membership(context: ContextTypes.DEFAULT_TYPE, user_id: int) -> bool:
    try:
        member = await context.bot.get_chat_member(CHANNEL_ID, user_id)
        return member.status in ("member", "administrator", "creator")
    except TelegramError as exc:
        log.warning("Membership check failed for %s: %s", user_id, exc)
        return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.effective_message:
        return
    with connect_db() as conn:
        conn.execute("INSERT OR IGNORE INTO users(user_id) VALUES (?)", (user.id,))
    member = await check_membership(context, user.id)
    if member:
        await update.effective_message.reply_text(
            "✅ Verification successful!\n\nYou can generate one key every 24 hours.",
            reply_markup=main_keyboard(True),
        )
    else:
        await update.effective_message.reply_text(
            "🔒 To access the key generator, join our private Telegram channel first.\n\n"
            "1. Tap “Join Private Channel” and send a join request.\n"
            "2. Return here and tap “Verify”.",
            reply_markup=main_keyboard(False),
        )


async def verify_or_generate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query or not query.from_user:
        return
    await query.answer()
    user_id = query.from_user.id
    data = query.data

    if data == "verify_join":
        if await check_membership(context, user_id):
            await query.edit_message_text(
                "✅ You are verified!\n\nPress below to generate your key.",
                reply_markup=main_keyboard(True),
            )
            return

        with connect_db() as conn:
            request = conn.execute(
                "SELECT user_id FROM join_requests WHERE user_id = ? AND chat_id = ?",
                (user_id, CHANNEL_ID),
            ).fetchone()

        if request:
            try:
                await context.bot.approve_chat_join_request(chat_id=CHANNEL_ID, user_id=user_id)
                with connect_db() as conn:
                    conn.execute("DELETE FROM join_requests WHERE user_id = ?", (user_id,))
                await query.edit_message_text(
                    "✅ Your join request was approved. Press Verify again in a moment, "
                    "or use /start to refresh your membership.",
                    reply_markup=main_keyboard(False),
                )
            except TelegramError as exc:
                log.warning("Could not approve join request for %s: %s", user_id, exc)
                await query.edit_message_text(
                    "I couldn't approve the request. Please make sure the bot is an admin "
                    "with permission to invite users, then try again.",
                    reply_markup=main_keyboard(False),
                )
        else:
            await query.edit_message_text(
                "No pending join request found. Tap Join Private Channel, submit a join request, "
                "then return here and press Verify again.",
                reply_markup=main_keyboard(False),
            )
        return

    if data == "generate_key":
        if not await check_membership(context, user_id):
            await query.edit_message_text(
                "❌ You are not currently a member of the required channel. Join it and verify first.",
                reply_markup=main_keyboard(False),
            )
            return

        now = int(time.time())
        with connect_db() as conn:
            conn.execute("INSERT OR IGNORE INTO users(user_id) VALUES (?)", (user_id,))
            row = conn.execute("SELECT last_key_at FROM users WHERE user_id = ?", (user_id,)).fetchone()
            last_key_at = row["last_key_at"] if row else None

            if last_key_at and now - int(last_key_at) < COOLDOWN_SECONDS:
                remaining = COOLDOWN_SECONDS - (now - int(last_key_at))
                hours = remaining // 3600
                minutes = (remaining % 3600) // 60
                await query.edit_message_text(
                    f"⏳ You have already received a key in the last 24 hours.\n"
                    f"Try again in about {hours}h {minutes}m.",
                    reply_markup=main_keyboard(True),
                )
                return

            available = conn.execute(
                "SELECT id, key_value FROM keys WHERE assigned_to IS NULL ORDER BY id LIMIT 1"
            ).fetchone()
            if not available:
                await query.edit_message_text(
                    "⚠️ No keys are available right now. Please try again later.",
                    reply_markup=main_keyboard(True),
                )
                return

            conn.execute(
                "UPDATE keys SET assigned_to = ?, assigned_at = ? WHERE id = ? AND assigned_to IS NULL",
                (user_id, now, available["id"]),
            )
            conn.execute(
                "UPDATE users SET last_key_at = ? WHERE user_id = ?",
                (now, user_id),
            )
            key_value = available["key_value"]
        mark_dirty()

        # Telegram HTML mein <br> support nahi hota, isliye \n use kiya hai.
        await query.edit_message_text(
            f"🎉 Your key:\n<code>{escape(key_value)}</code>\n\n"
            "You can generate your next key after 24 hours.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="refresh")]
            ]),
        )
        return

    if data == "refresh":
        if await check_membership(context, user_id):
            await query.edit_message_text(
                "You can generate one key every 24 hours.",
                reply_markup=main_keyboard(True),
            )
        else:
            await query.edit_message_text(
                "Please join the required channel and verify first.",
                reply_markup=main_keyboard(False),
            )


async def addkeys_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    if not is_admin(user.id):
        await message.reply_text("⛔ This command is for bot admins only.")
        return
    text = None
    if message.reply_to_message and message.reply_to_message.text:
        text = message.reply_to_message.text
    elif context.args:
        text = "\n".join(context.args)
    if not text:
        await message.reply_text(
            "Keys add karne ke liye:\n"
            "1) Keys ek message mein bhejo (ek line = ek key), phir us message ko reply karke /addkeys likho.\n"
            "2) Ya: /addkeys KEY1 KEY2 KEY3"
        )
        return
    values = [line.strip() for line in text.splitlines() if line.strip()]
    added = 0
    duplicates = 0
    with connect_db() as conn:
        for value in values:
            try:
                conn.execute("INSERT INTO keys(key_value) VALUES (?)", (value,))
                added += 1
            except sqlite3.IntegrityError:
                duplicates += 1
    mark_dirty()
    await message.reply_text(
        f"✅ Bulk import complete.\nAdded: {added}\nDuplicates skipped: {duplicates}"
    )


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    if not is_admin(user.id):
        await message.reply_text("⛔ This command is for bot admins only.")
        return
    with connect_db() as conn:
        users = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        available = conn.execute("SELECT COUNT(*) AS n FROM keys WHERE assigned_to IS NULL").fetchone()["n"]
        assigned = conn.execute("SELECT COUNT(*) AS n FROM keys WHERE assigned_to IS NOT NULL").fetchone()["n"]
    await message.reply_text(
        f"📊 Bot statistics\nUsers seen: {users}\nAvailable keys: {available}\n"
        f"Assigned keys: {assigned}"
    )


async def backup_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if not message or not user or not is_admin(user.id):
        return
    if not STORAGE_CHANNEL_ID:
        await message.reply_text("STORAGE_CHANNEL_ID set nahi hai.")
        return
    await do_backup(context.bot)
    await message.reply_text("✅ Backup storage channel mein save ho gaya.")


async def join_request_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    request = update.chat_join_request
    if not request or request.chat.id != CHANNEL_ID:
        return
    with connect_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO join_requests(user_id, chat_id, requested_at) VALUES (?, ?, ?)",
            (request.from_user.id, request.chat.id, int(time.time())),
        )
    log.info("Recorded join request from user %s", request.from_user.id)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if msg:
        await msg.reply_text(
            "Commands:\n/start - Open key menu\n"
            "/addkeys - (admin) keys add karo\n/stats - (admin) statistics\n/backup - (admin) manual backup"
        )


def main():
    # Python 3.14+ mein main thread par event loop auto-create nahi hota
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
    start_health_server()
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_stop(post_stop)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("addkeys", addkeys_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("backup", backup_command))
    app.add_handler(CallbackQueryHandler(verify_or_generate))
    app.add_handler(ChatJoinRequestHandler(join_request_handler))
    app.job_queue.run_repeating(backup_job, interval=60, first=60)
    log.info("Bot is starting with long polling.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
