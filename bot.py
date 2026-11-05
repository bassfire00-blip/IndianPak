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
    MessageHandler,
    filters,
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
PRIVATE_GROUP_ID_RAW = os.getenv("PRIVATE_GROUP_ID", "").strip()
DATABASE_PATH = os.getenv("DATABASE_PATH", "bot.db").strip()
PORT = int(os.getenv("PORT", "10000"))
COOLDOWN_SECONDS = 24 * 60 * 60
GEMS_PER_REFERRAL = 10
PRIVATE_GROUP_COST = 100

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
    PRIVATE_GROUP_ID = int(PRIVATE_GROUP_ID_RAW) if PRIVATE_GROUP_ID_RAW else None
except ValueError as exc:
    raise RuntimeError("CHANNEL_ID, ADMIN_IDS, STORAGE_CHANNEL_ID and PRIVATE_GROUP_ID must be numeric Telegram IDs.") from exc

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
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(users)")}
        for name, ddl in (
            ("referred_by", "INTEGER"),
            ("ref_credited", "INTEGER DEFAULT 0"),
            ("gems", "INTEGER DEFAULT 0"),
            ("private_unlocked", "INTEGER DEFAULT 0"),
        ):
            if name not in cols:
                conn.execute(f"ALTER TABLE users ADD COLUMN {name} {ddl}")


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
            [InlineKeyboardButton("🔑 Generate Key", callback_data="generate_key")],
            [
                InlineKeyboardButton("💎 My Gems", callback_data="gems"),
                InlineKeyboardButton("👥 Refer & Earn", callback_data="refer"),
            ],
            [InlineKeyboardButton(f"🔓 Private Group ({PRIVATE_GROUP_COST} 💎)", callback_data="private")],
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


def get_gems(user_id: int) -> int:
    with connect_db() as conn:
        row = conn.execute("SELECT gems FROM users WHERE user_id = ?", (user_id,)).fetchone()
    return int(row["gems"] or 0) if row else 0


async def try_credit_referral(context: ContextTypes.DEFAULT_TYPE, user_id: int):
    """Naya banda channel join verify kare to referrer ko 10 gems (sirf ek baar)."""
    referrer = None
    with connect_db() as conn:
        row = conn.execute(
            "SELECT referred_by, ref_credited FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row and row["referred_by"] and not row["ref_credited"] and row["referred_by"] != user_id:
            referrer = int(row["referred_by"])
            conn.execute("UPDATE users SET ref_credited = 1 WHERE user_id = ?", (user_id,))
            conn.execute("INSERT OR IGNORE INTO users(user_id) VALUES (?)", (referrer,))
            conn.execute(
                "UPDATE users SET gems = COALESCE(gems, 0) + ? WHERE user_id = ?",
                (GEMS_PER_REFERRAL, referrer),
            )
            total = conn.execute("SELECT gems FROM users WHERE user_id = ?", (referrer,)).fetchone()["gems"]
    if referrer:
        mark_dirty()
        try:
            await context.bot.send_message(
                referrer,
                f"🎉 Tumhare referral se ek naya member join hua!\n"
                f"+{GEMS_PER_REFERRAL} 💎 mile. Total gems: {total}",
            )
        except TelegramError:
            pass


async def animate_loading(query, title: str):
    frames = ["▱▱▱▱▱  0%", "▰▱▱▱▱  20%", "▰▰▱▱▱  40%", "▰▰▰▱▱  60%", "▰▰▰▰▱  80%", "▰▰▰▰▰  100%"]
    try:
        for frame in frames:
            await query.edit_message_text(f"{title}\n\n{frame}")
            await asyncio.sleep(0.6)
    except TelegramError:
        pass


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or not update.effective_message:
        return
    with connect_db() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO users(user_id) VALUES (?)", (user.id,))
        is_new = cur.rowcount > 0
        if is_new and context.args and context.args[0].startswith("ref_"):
            try:
                ref_id = int(context.args[0][4:])
            except ValueError:
                ref_id = None
            if ref_id and ref_id != user.id:
                conn.execute("UPDATE users SET referred_by = ? WHERE user_id = ?", (ref_id, user.id))
    if is_new:
        mark_dirty()
    member = await check_membership(context, user.id)
    if member:
        await try_credit_referral(context, user.id)
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
            await try_credit_referral(context, user_id)
            await query.edit_message_text(
                "✅ You are verified!\n\nPress below to generate your key.",
                reply_markup=main_keyboard(True),
            )
            return

        # Bot khud request approve karta hai. DB record na bhi ho (restart ke baad)
        # to bhi seedha approve try karta hai; request hogi to Telegram approve kar dega.
        try:
            await context.bot.approve_chat_join_request(chat_id=CHANNEL_ID, user_id=user_id)
        except TelegramError as exc:
            err = str(exc).lower()
            log.warning("Approve join request failed for %s: %s", user_id, exc)
            if "hide_requester_missing" in err or "user_already_participant" in err or "not found" in err:
                msg = (
                    "No pending join request found. Tap Join Private Channel, submit a join request, "
                    "then return here and press Verify again."
                )
            else:
                msg = (
                    "I couldn't approve the request. Please make sure the bot is an admin "
                    "with permission to invite users, then try again."
                )
            await query.edit_message_text(msg, reply_markup=main_keyboard(False))
            return

        with connect_db() as conn:
            conn.execute("DELETE FROM join_requests WHERE user_id = ?", (user_id,))

        # Approve ke turant baad membership dobara check karke seedha menu dikhao.
        verified = False
        for _ in range(3):
            if await check_membership(context, user_id):
                verified = True
                break
            await asyncio.sleep(1)
        if verified:
            await try_credit_referral(context, user_id)
            await query.edit_message_text(
                "✅ Request approve ho gayi aur tum verified ho!\n\nPress below to generate your key.",
                reply_markup=main_keyboard(True),
            )
        else:
            await query.edit_message_text(
                "✅ Your join request was approved. Press Verify again in a moment.",
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

        await animate_loading(query, "🔄 Generating your key...")
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

    if data == "gems":
        await query.edit_message_text(
            f"💎 Tumhare gems: {get_gems(user_id)}\n\n"
            f"1 referral = {GEMS_PER_REFERRAL} 💎\n"
            f"{PRIVATE_GROUP_COST} 💎 = Private Group (saari private files) ki entry",
            reply_markup=main_keyboard(True),
        )
        return

    if data == "refer":
        link = f"https://t.me/{context.bot.username}?start=ref_{user_id}"
        await query.edit_message_text(
            f"👥 Refer & Earn\n\n"
            f"Ye link dosto ko bhejo. Jab wo bot start karke channel join aur verify karenge, "
            f"tumhe {GEMS_PER_REFERRAL} 💎 milenge.\n\n"
            f"🔗 {link}\n\n"
            f"💎 Abhi ke gems: {get_gems(user_id)}",
            reply_markup=main_keyboard(True),
        )
        return

    if data == "private":
        if not await check_membership(context, user_id):
            await query.edit_message_text(
                "❌ Pehle required channel join karke verify karo.",
                reply_markup=main_keyboard(False),
            )
            return
        if not PRIVATE_GROUP_ID:
            await query.edit_message_text(
                "⚠️ Private group abhi set nahi hua. Admin se contact karo.",
                reply_markup=main_keyboard(True),
            )
            return
        with connect_db() as conn:
            row = conn.execute(
                "SELECT gems, private_unlocked FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            gems = int(row["gems"] or 0) if row else 0
            unlocked = bool(row and row["private_unlocked"])
            charged = False
            if not unlocked:
                if gems < PRIVATE_GROUP_COST:
                    need = PRIVATE_GROUP_COST - gems
                    await query.edit_message_text(
                        f"🔒 Private Group ke liye {PRIVATE_GROUP_COST} 💎 chahiye.\n"
                        f"Tumhare paas {gems} 💎 hain, abhi {need} 💎 aur chahiye "
                        f"({(need + GEMS_PER_REFERRAL - 1) // GEMS_PER_REFERRAL} referrals).",
                        reply_markup=main_keyboard(True),
                    )
                    return
                cur = conn.execute(
                    "UPDATE users SET gems = gems - ?, private_unlocked = 1 "
                    "WHERE user_id = ? AND gems >= ? AND COALESCE(private_unlocked, 0) = 0",
                    (PRIVATE_GROUP_COST, user_id, PRIVATE_GROUP_COST),
                )
                charged = cur.rowcount > 0
        try:
            invite = await context.bot.create_chat_invite_link(
                PRIVATE_GROUP_ID, member_limit=1, expire_date=int(time.time()) + 24 * 3600
            )
        except TelegramError as exc:
            log.error("Private group invite failed: %s", exc)
            if charged:
                with connect_db() as conn:
                    conn.execute(
                        "UPDATE users SET gems = gems + ?, private_unlocked = 0 WHERE user_id = ?",
                        (PRIVATE_GROUP_COST, user_id),
                    )
            await query.edit_message_text(
                "⚠️ Invite link nahi ban paya. Admin se bolo ki bot ko private group mein admin "
                "(Invite Users permission) banaye. Tumhare gems safe hain.",
                reply_markup=main_keyboard(True),
            )
            return
        mark_dirty()
        await query.edit_message_text(
            "🎉 Private Group unlock ho gaya!\n\n"
            f"🔗 {invite.invite_link}\n\n"
            "Ye link sirf 1 banda use kar sakta hai aur 24 ghante mein expire ho jayega. "
            "Link kho jaye to ye button dobara dabao (gems dobara nahi katenge).",
            reply_markup=main_keyboard(True),
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


def import_keys(text: str):
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
    if added:
        mark_dirty()
    return added, duplicates


async def addkeys_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    if not is_admin(user.id):
        await message.reply_text("⛔ This command is for bot admins only.")
        return
    # Optional shortcut: /addkeys KEY1 KEY2 ...
    if context.args:
        added, duplicates = import_keys("\n".join(context.args))
        await message.reply_text(
            f"✅ Bulk import complete.\nAdded: {added}\nDuplicates skipped: {duplicates}"
        )
        return
    context.user_data["awaiting_keys"] = True
    await message.reply_text(
        "📄 Ab apni KEY.txt file bhejo.\n"
        "File ke andar ek line mein ek key honi chahiye.\n\n"
        "Cancel karne ke liye /cancel likho."
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if message and context.user_data.pop("awaiting_keys", None):
        await message.reply_text("❎ Cancelled.")


async def keys_file_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if not message or not user or not message.document:
        return
    if not is_admin(user.id) or not context.user_data.get("awaiting_keys"):
        return
    doc = message.document
    if not (doc.file_name or "").lower().endswith(".txt"):
        await message.reply_text("❌ Sirf .txt file bhejo (jaise KEY.txt).")
        return
    try:
        tg_file = await doc.get_file()
        data = await tg_file.download_as_bytearray()
        text = bytes(data).decode("utf-8-sig", errors="ignore")
    except TelegramError as exc:
        await message.reply_text(f"❌ File download nahi hui: {exc}")
        return
    added, duplicates = import_keys(text)
    context.user_data["awaiting_keys"] = False
    await message.reply_text(
        f"✅ File import complete.\nAdded: {added}\nDuplicates skipped: {duplicates}"
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
            "/addkeys - (admin) KEY.txt file se keys add karo\n/stats - (admin) statistics\n/backup - (admin) manual backup"
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
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(MessageHandler(filters.Document.ALL, keys_file_handler))
    app.add_handler(CommandHandler("backup", backup_command))
    app.add_handler(CallbackQueryHandler(verify_or_generate))
    app.add_handler(ChatJoinRequestHandler(join_request_handler))
    app.job_queue.run_repeating(backup_job, interval=60, first=60)
    log.info("Bot is starting with long polling.")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False)


if __name__ == "__main__":
    main()
