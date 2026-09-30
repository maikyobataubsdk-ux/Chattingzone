#!/usr/bin/env python3
"""
Telegram Support & Reply Bot with Clone System and Supreme Owner System.
Single-file production-ready implementation.
"""

import os
import sys
import json
import logging
import asyncio
import datetime
import re
from typing import Dict, Any, List, Optional, Union

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Bot,
    User as TelegramUser,
    Chat,
    Message
)
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters
)
from telegram.error import TelegramError, Forbidden, BadRequest

# -------------------------------------------------------------------
# CONFIGURATION
# -------------------------------------------------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "YOUR_BOT_TOKEN_HERE")
SUPREME_OWNER_ID = int(os.getenv("SUPREME_OWNER_ID", "0"))
DATABASE_CHANNEL_ID = os.getenv("DATABASE_CHANNEL_ID", "0")
LOG_CHANNEL_ID = os.getenv("LOG_CHANNEL_ID", "0")
OFFICIAL_CHANNEL = os.getenv("OFFICIAL_CHANNEL", "https://t.me/telegram")
BOT_USERNAME = os.getenv("BOT_USERNAME", "")
DATA_FILE = os.getenv("DATA_FILE", "database.json")

# Logging setup
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("SupportBot")

# Global dict to hold running clone Application instances: bot_id -> Application
active_clone_apps: Dict[str, Application] = {}

# User state tracker for clone token collection & setting edits
# user_id -> {"state": str, "bot_id": str}
user_conversations: Dict[int, Dict[str, Any]] = {}

# -------------------------------------------------------------------
# HELPER FOR MASTER BOT IDENTIFICATION
# -------------------------------------------------------------------
def is_master_id(bot_id: str) -> bool:
    """Returns True if bot_id is master or not in registered clones."""
    if bot_id == "master":
        return True
    return str(bot_id) not in db.data["clones"]

# -------------------------------------------------------------------
# JSON DATABASE ENGINE
# -------------------------------------------------------------------
class JSONDatabase:
    def __init__(self, filepath: str = DATA_FILE):
        self.filepath = filepath
        self.lock = asyncio.Lock()
        self.data: Dict[str, Any] = {
            "clones": {},       # bot_id -> clone_data
            "users": {},        # f"{bot_id}_{user_id}" -> user_data
            "tickets": {},      # f"{bot_id}_{ticket_id}" -> ticket_data
            "forcesub": {
                "enabled": True,
                "channels": []  # list of {"channel_id": str/int, "title": str, "link": str}
            },
            "stats": {
                "total_messages": 0,
                "total_tickets": 0,
                "total_broadcasts": 0
            },
            "master_settings": {
                "welcome_message": "🚀 Welcome to our Support System!\n\nSend your message below and our support team will get back to you.",
                "official_channel": OFFICIAL_CHANNEL,
                "ban_message": "🚫 You are banned from using this support bot.",
                "support_message": "💬 Your message has been received! Our support team will reply shortly.",
                "support_channel_id": DATABASE_CHANNEL_ID
            },
            "master_ticket_counter": 0,
            "logs": []          # list of log entries
        }
        self.load()

    def load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    content = f.read()
                    if content.strip():
                        loaded = json.loads(content)
                        for k, v in loaded.items():
                            if isinstance(v, dict) and k in self.data and isinstance(self.data[k], dict):
                                self.data[k].update(v)
                            else:
                                self.data[k] = v
                        logger.info("Database loaded successfully from %s", self.filepath)
            except Exception as e:
                logger.error("Error loading database: %s", e)

    async def save(self):
        async with self.lock:
            try:
                tmp_path = f"{self.filepath}.tmp"
                with open(tmp_path, "w", encoding="utf-8") as f:
                    json.dump(self.data, f, indent=2, ensure_ascii=False)
                os.replace(tmp_path, self.filepath)
            except Exception as e:
                logger.error("Error saving database: %s", e)

    def get_user(self, bot_id: str, user_id: int) -> Optional[Dict[str, Any]]:
        key = f"{bot_id}_{user_id}"
        return self.data["users"].get(key)

    def upsert_user(self, bot_id: str, user: TelegramUser) -> Dict[str, Any]:
        key = f"{bot_id}_{user.id}"
        now_str = datetime.datetime.now(datetime.timezone.utc).isoformat()
        if key not in self.data["users"]:
            self.data["users"][key] = {
                "bot_id": str(bot_id),
                "user_id": user.id,
                "first_name": user.first_name or "",
                "username": f"@{user.username}" if user.username else "None",
                "first_seen": now_str,
                "last_seen": now_str,
                "message_count": 0,
                "ticket_count": 0,
                "status": "active",  # "active" or "banned"
                "banned_at": None,
                "banned_by": None,
                "language_code": user.language_code or "en"
            }
        else:
            u = self.data["users"][key]
            u["first_name"] = user.first_name or ""
            u["username"] = f"@{user.username}" if user.username else "None"
            u["last_seen"] = now_str
        return self.data["users"][key]

    def increment_user_msg(self, bot_id: str, user_id: int):
        u = self.get_user(bot_id, user_id)
        if u:
            u["message_count"] = u.get("message_count", 0) + 1
            u["last_seen"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        self.data["stats"]["total_messages"] = self.data["stats"].get("total_messages", 0) + 1

    def create_ticket(
        self,
        bot_id: str,
        user_id: int,
        admin_msg_id: int,
        user_msg_id: int,
        msg_type: str
    ) -> int:
        if is_master_id(bot_id):
            counter_key = "master_ticket_counter"
            ticket_id = self.data.get(counter_key, 0) + 1
            self.data[counter_key] = ticket_id
        else:
            clone = self.data["clones"].get(str(bot_id))
            if not clone:
                ticket_id = 1
            else:
                ticket_id = clone.get("ticket_counter", 0) + 1
                clone["ticket_counter"] = ticket_id

        key = f"{bot_id}_{ticket_id}"
        self.data["tickets"][key] = {
            "bot_id": str(bot_id),
            "ticket_id": ticket_id,
            "user_id": user_id,
            "admin_msg_id": admin_msg_id,
            "user_msg_id": user_msg_id,
            "status": "open",
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "message_type": msg_type
        }

        u = self.get_user(bot_id, user_id)
        if u:
            u["ticket_count"] = u.get("ticket_count", 0) + 1

        self.data["stats"]["total_tickets"] = self.data["stats"].get("total_tickets", 0) + 1
        return ticket_id

    def get_ticket(self, bot_id: str, ticket_id: int) -> Optional[Dict[str, Any]]:
        return self.data["tickets"].get(f"{bot_id}_{ticket_id}")

    def add_clone(
        self,
        bot_id: str,
        token: str,
        name: str,
        username: str,
        owner_id: int
    ) -> Dict[str, Any]:
        clone_data = {
            "bot_id": str(bot_id),
            "token": token,
            "name": name,
            "username": f"@{username}" if username and not username.startswith("@") else username,
            "owner_id": owner_id,
            "admins": [owner_id],
            "status": "active",  # "active" or "paused"
            "created_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "ticket_counter": 0,
            "settings": {
                "welcome_message": "🚀 Welcome to our Support System!\n\nSend your message below and our support team will get back to you.",
                "official_channel": OFFICIAL_CHANNEL,
                "ban_message": "🚫 You are banned from using this support bot.",
                "support_message": "💬 Your message has been received! Our support team will reply shortly.",
                "support_channel_id": None
            }
        }
        self.data["clones"][str(bot_id)] = clone_data
        return clone_data

    def get_clone(self, bot_id: str) -> Optional[Dict[str, Any]]:
        return self.data["clones"].get(str(bot_id))

    def log_event(self, event_type: str, details: str):
        entry = {
            "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "type": event_type,
            "details": details
        }
        self.data["logs"].append(entry)
        if len(self.data["logs"]) > 500:
            self.data["logs"] = self.data["logs"][-500:]

db = JSONDatabase()

# -------------------------------------------------------------------
# FORCESUB VERIFICATION HELPER
# -------------------------------------------------------------------
async def check_user_forcesub(bot: Bot, user_id: int) -> tuple[bool, List[Dict[str, Any]]]:
    fs_config = db.data.get("forcesub", {})
    if not fs_config.get("enabled", True):
        return True, []

    channels = fs_config.get("channels", [])
    if not channels:
        return True, []

    unjoined = []
    for ch in channels:
        ch_id = ch["channel_id"]
        try:
            member = await bot.get_chat_member(chat_id=ch_id, user_id=user_id)
            if member.status in ["left", "kicked"]:
                unjoined.append(ch)
        except Exception as e:
            logger.warning("ForceSub check error for chat %s user %s: %s", ch_id, user_id, e)
            unjoined.append(ch)

    return len(unjoined) == 0, unjoined

def build_forcesub_keyboard(unjoined_channels: List[Dict[str, Any]], user_id: int) -> InlineKeyboardMarkup:
    keyboard = []
    for idx, ch in enumerate(unjoined_channels, start=1):
        title = ch.get("title", f"Channel {idx}")
        link = ch.get("link", OFFICIAL_CHANNEL)
        keyboard.append([InlineKeyboardButton(f"📢 Join {title}", url=link)])

    keyboard.append([InlineKeyboardButton("✅ I Joined", callback_data=f"check_forcesub_{user_id}")])
    return InlineKeyboardMarkup(keyboard)

# -------------------------------------------------------------------
# PERMISSION HELPERS
# -------------------------------------------------------------------
def is_admin(bot_id: str, user_id: int) -> bool:
    if user_id == SUPREME_OWNER_ID:
        return True
    if is_master_id(bot_id):
        return user_id == SUPREME_OWNER_ID
    clone = db.get_clone(bot_id)
    if clone:
        return user_id == clone.get("owner_id") or user_id in clone.get("admins", [])
    return False

def is_clone_owner(bot_id: str, user_id: int) -> bool:
    if user_id == SUPREME_OWNER_ID:
        return True
    clone = db.get_clone(bot_id)
    if clone:
        return user_id == clone.get("owner_id")
    return False

# -------------------------------------------------------------------
# START & UI HANDLERS
# -------------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user or not update.effective_chat:
        return

    user = update.effective_user
    bot_id = str(context.bot.id)
    db.upsert_user(bot_id, user)
    await db.save()

    if is_master_id(bot_id):
        welcome_msg = db.data["master_settings"]["welcome_message"]
        channel_link = db.data["master_settings"]["official_channel"]
    else:
        clone = db.get_clone(bot_id)
        welcome_msg = clone["settings"]["welcome_message"] if clone else db.data["master_settings"]["welcome_message"]
        channel_link = clone["settings"]["official_channel"] if clone else db.data["master_settings"]["official_channel"]

    inline_buttons = [
        [
            InlineKeyboardButton("📢 Official Community", url=channel_link),
            InlineKeyboardButton("🤖 Clone Bot", callback_data="btn_clone_start")
        ]
    ]

    reply_markup = InlineKeyboardMarkup(inline_buttons)
    await update.message.reply_text(welcome_msg, reply_markup=reply_markup)

async def clone_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    is_joined, unjoined = await check_user_forcesub(context.bot, user_id)
    if not is_joined:
        kb = build_forcesub_keyboard(unjoined, user_id)
        msg_text = (
            "🔒 **Join Required Channels**\n\n"
            "To clone this bot, you must join all required channels first.\n"
            "After joining, press **✅ I Joined**."
        )
        await query.message.reply_text(msg_text, reply_markup=kb, parse_mode="Markdown")
        return

    user_conversations[user_id] = {"state": "AWAITING_TOKEN"}

    prompt_text = (
        "🤖 **Bot Cloning Process**\n\n"
        "To create your own Support & Reply Bot:\n"
        "1. Open @BotFather on Telegram.\n"
        "2. Create a new bot using `/newbot`.\n"
        "3. Copy the Bot API Token provided.\n"
        "4. Reply here by pasting your token below:"
    )
    await query.message.reply_text(prompt_text, parse_mode="Markdown")

async def check_forcesub_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user_id = query.from_user.id

    data = query.data or ""
    parts = data.split("_")
    target_user_id = int(parts[-1]) if parts[-1].isdigit() else user_id

    if user_id != target_user_id:
        await query.answer("⚠️ This button is not for you.", show_alert=True)
        return

    is_joined, unjoined = await check_user_forcesub(context.bot, user_id)
    if is_joined:
        await query.answer("✅ Verification successful!", show_alert=True)
        user_conversations[user_id] = {"state": "AWAITING_TOKEN"}
        await query.message.edit_text(
            "✅ **Subscription verified!**\n\nYou can now proceed to clone the bot.\n\n"
            "Please send your **BotFather API Token** below:",
            parse_mode="Markdown"
        )
    else:
        await query.answer("❌ You have not joined all required channels yet!", show_alert=True)

# -------------------------------------------------------------------
# SUPPORT & TICKET HANDLING
# -------------------------------------------------------------------
def detect_msg_type(message: Message) -> str:
    if message.text:
        return "Text"
    elif message.photo:
        return "Photo"
    elif message.video:
        return "Video"
    elif message.document:
        return "Document"
    elif message.audio:
        return "Audio"
    elif message.voice:
        return "Voice"
    elif message.sticker:
        return "Sticker"
    elif message.animation:
        return "Animation"
    return "Media"

async def handle_user_support_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    user = update.effective_user
    if not message or not user or user.is_bot:
        return

    bot_id = str(context.bot.id)
    user_record = db.upsert_user(bot_id, user)

    if user_record.get("status") == "banned":
        clone = db.get_clone(bot_id)
        ban_msg = clone["settings"]["ban_message"] if clone else db.data["master_settings"]["ban_message"]
        await message.reply_text(ban_msg)
        return

    if not is_master_id(bot_id):
        clone = db.get_clone(bot_id)
        if clone and clone.get("status") == "paused":
            await message.reply_text("⏸️ This bot is temporarily unavailable.")
            return

    db.increment_user_msg(bot_id, user.id)

    support_chat_id = None
    if is_master_id(bot_id):
        support_chat_id = DATABASE_CHANNEL_ID if str(DATABASE_CHANNEL_ID) != "0" else SUPREME_OWNER_ID
    else:
        clone = db.get_clone(bot_id)
        if clone:
            support_chat_id = clone["settings"].get("support_channel_id") or clone.get("owner_id")

    if not support_chat_id or str(support_chat_id) == "0":
        support_chat_id = SUPREME_OWNER_ID

    msg_type = detect_msg_type(message)

    try:
        copied_msg = await message.copy(chat_id=support_chat_id)
    except Exception as e:
        logger.error("Failed to copy message to admin support chat %s: %s", support_chat_id, e)
        await message.reply_text("⚠️ Support service is currently unavailable.")
        return

    ticket_id = db.create_ticket(
        bot_id=bot_id,
        user_id=user.id,
        admin_msg_id=copied_msg.message_id,
        user_msg_id=message.message_id,
        msg_type=msg_type
    )

    msg_content = message.text or message.caption or f"[{msg_type} Media]"
    user_name = user.first_name or "User"
    username_str = f"@{user.username}" if user.username else "None"

    admin_card = (
        f"#{ticket_id}\n\n"
        f"👤 User: {user_name}\n"
        f"🆔 ID: `{user.id}`\n"
        f"🔗 Username: {username_str}\n"
        f"💬 Message:\n{msg_content}"
    )

    inline_buttons = [
        [
            InlineKeyboardButton("↩️ Reply", callback_data=f"reply_t_{ticket_id}"),
            InlineKeyboardButton("👤 Open User", url=f"tg://user?id={user.id}")
        ],
        [
            InlineKeyboardButton("🚫 Ban", callback_data=f"ban_u_{user.id}"),
            InlineKeyboardButton("📋 User Info", callback_data=f"info_u_{user.id}")
        ]
    ]

    try:
        await context.bot.send_message(
            chat_id=support_chat_id,
            text=admin_card,
            reply_markup=InlineKeyboardMarkup(inline_buttons),
            reply_to_message_id=copied_msg.message_id,
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning("Could not send metadata card as reply to copied message: %s", e)

    clone = db.get_clone(bot_id)
    ack_msg = clone["settings"]["support_message"] if clone else db.data["master_settings"]["support_message"]
    await message.reply_text(f"{ack_msg}\n\n🎫 Reference Ticket: **#{ticket_id}**", parse_mode="Markdown")
    await db.save()

# -------------------------------------------------------------------
# ADMIN REPLY ENGINE
# -------------------------------------------------------------------
async def reply_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not update.effective_user:
        return

    bot_id = str(context.bot.id)
    if not is_admin(bot_id, update.effective_user.id):
        return

    if not context.args or len(context.args) < 2:
        await message.reply_text("⚠️ Usage: `/reply #<ticket_id> <your reply message>`", parse_mode="Markdown")
        return

    ticket_raw = context.args[0].replace("#", "")
    if not ticket_raw.isdigit():
        await message.reply_text("⚠️ Invalid Ticket ID format.")
        return

    ticket_id = int(ticket_raw)
    ticket = db.get_ticket(bot_id, ticket_id)

    if not ticket:
        await message.reply_text(f"❌ Ticket #{ticket_id} not found for this bot.")
        return

    reply_text = " ".join(context.args[1:])
    target_user_id = ticket["user_id"]

    try:
        await context.bot.send_message(
            chat_id=target_user_id,
            text=f"💬 **Support Reply (Ticket #{ticket_id}):**\n\n{reply_text}",
            parse_mode="Markdown"
        )
        await message.reply_text(f"✅ Reply sent to user `{target_user_id}` for Ticket #{ticket_id}.", parse_mode="Markdown")
        db.log_event("reply", f"Admin {update.effective_user.id} replied to Ticket #{ticket_id} (User {target_user_id})")
        await db.save()
    except Exception as e:
        await message.reply_text(f"❌ Failed to send reply to user: {e}")

async def reply_button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    bot_id = str(context.bot.id)
    if not is_admin(bot_id, query.from_user.id):
        await query.answer("⚠️ Unauthorized.", show_alert=True)
        return

    data = query.data or ""
    parts = data.split("_")
    ticket_id = int(parts[-1]) if parts[-1].isdigit() else 0

    ticket = db.get_ticket(bot_id, ticket_id)
    if not ticket:
        await query.answer("❌ Ticket not found.", show_alert=True)
        return

    await query.message.reply_text(
        f"↩️ To reply to Ticket #{ticket_id}, use the command:\n\n"
        f"`/reply #{ticket_id} <your message>`",
        parse_mode="Markdown"
    )

# -------------------------------------------------------------------
# BAN & USER INFO COMMANDS
# -------------------------------------------------------------------
async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    bot_id = str(context.bot.id)
    if not is_admin(bot_id, update.effective_user.id):
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("⚠️ Usage: `/ban <USER_ID>`", parse_mode="Markdown")
        return

    target_user_id = int(context.args[0])
    u = db.get_user(bot_id, target_user_id)
    if not u:
        await update.message.reply_text("❌ User record not found.")
        return

    u["status"] = "banned"
    u["banned_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    u["banned_by"] = update.effective_user.id
    await db.save()

    await update.message.reply_text(f"🚫 User `{target_user_id}` has been banned.", parse_mode="Markdown")

async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    bot_id = str(context.bot.id)
    if not is_admin(bot_id, update.effective_user.id):
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("⚠️ Usage: `/unban <USER_ID>`", parse_mode="Markdown")
        return

    target_user_id = int(context.args[0])
    u = db.get_user(bot_id, target_user_id)
    if not u:
        await update.message.reply_text("❌ User record not found.")
        return

    u["status"] = "active"
    u["banned_at"] = None
    u["banned_by"] = None
    await db.save()

    await update.message.reply_text(f"✅ User `{target_user_id}` has been unbanned.", parse_mode="Markdown")

async def user_info_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return
    bot_id = str(context.bot.id)
    if not is_admin(bot_id, update.effective_user.id):
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("⚠️ Usage: `/user <USER_ID>`", parse_mode="Markdown")
        return

    target_user_id = int(context.args[0])
    u = db.get_user(bot_id, target_user_id)
    if not u:
        await update.message.reply_text("❌ User not found in database.")
        return

    info_text = (
        f"👤 **User Information**\n\n"
        f"**Name:** {u.get('first_name')}\n"
        f"**Username:** {u.get('username')}\n"
        f"**User ID:** `{u.get('user_id')}`\n"
        f"**First Seen:** {u.get('first_seen')}\n"
        f"**Last Seen:** {u.get('last_seen')}\n"
        f"**Messages:** {u.get('message_count')}\n"
        f"**Tickets:** {u.get('ticket_count')}\n"
        f"**Status:** {u.get('status').upper()}\n"
        f"**Bot ID:** `{u.get('bot_id')}`\n"
        f"**Language:** {u.get('language_code')}"
    )

    if u.get("status") == "banned":
        info_text += f"\n**Banned Date:** {u.get('banned_at')}\n**Banned By:** `{u.get('banned_by')}`"

    await update.message.reply_text(info_text, parse_mode="Markdown")

async def ban_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    bot_id = str(context.bot.id)
    if not is_admin(bot_id, query.from_user.id):
        await query.answer("⚠️ Unauthorized.", show_alert=True)
        return

    parts = query.data.split("_")
    target_user_id = int(parts[-1]) if parts[-1].isdigit() else 0
    u = db.get_user(bot_id, target_user_id)
    if u:
        u["status"] = "banned"
        u["banned_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        u["banned_by"] = query.from_user.id
        await db.save()
        await query.answer(f"🚫 User {target_user_id} banned.", show_alert=True)
    else:
        await query.answer("❌ User not found.", show_alert=True)

async def user_info_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    bot_id = str(context.bot.id)
    if not is_admin(bot_id, query.from_user.id):
        await query.answer("⚠️ Unauthorized.", show_alert=True)
        return

    parts = query.data.split("_")
    target_user_id = int(parts[-1]) if parts[-1].isdigit() else 0
    u = db.get_user(bot_id, target_user_id)
    if not u:
        await query.answer("❌ User not found.", show_alert=True)
        return

    info_text = (
        f"👤 **User Info**\n"
        f"Name: {u.get('first_name')}\n"
        f"ID: `{u.get('user_id')}`\n"
        f"Status: {u.get('status').upper()}\n"
        f"Tickets: {u.get('ticket_count')}"
    )
    await query.message.reply_text(info_text, parse_mode="Markdown")

# -------------------------------------------------------------------
# SUPREME OWNER SYSTEM
# -------------------------------------------------------------------
async def supreme_panel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != SUPREME_OWNER_ID:
        return

    clones = db.data["clones"]
    total_bots = len(clones) + 1
    active_bots = sum(1 for c in clones.values() if c.get("status") == "active") + 1
    total_users = len(db.data["users"])
    total_tickets = len(db.data["tickets"])

    panel_text = (
        "👑 **SUPREME CONTROL PANEL**\n\n"
        f"🤖 Total Bots: **{total_bots}** (Master + {len(clones)} Clones)\n"
        f"🟢 Active Bots: **{active_bots}**\n"
        f"👥 Global Users: **{total_users}**\n"
        f"🎫 Global Tickets: **{total_tickets}**\n\n"
        "Select an administrative module below:"
    )

    buttons = [
        [
            InlineKeyboardButton("🤖 Bots", callback_data="sup_bots"),
            InlineKeyboardButton("📊 Stats", callback_data="sup_stats")
        ],
        [
            InlineKeyboardButton("📢 All Broadcast", callback_data="sup_allbc"),
            InlineKeyboardButton("📢 ForceSub", callback_data="sup_forcesub")
        ],
        [
            InlineKeyboardButton("📋 Logs", callback_data="sup_logs")
        ]
    ]

    await update.message.reply_text(panel_text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")

async def supreme_bots_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != SUPREME_OWNER_ID:
        return

    clones = db.data["clones"]
    if not clones:
        await update.message.reply_text("🤖 No cloned bots registered yet.")
        return

    for c_id, clone in clones.items():
        users = [u for u in db.data["users"].values() if u.get("bot_id") == c_id]
        tickets = [t for t in db.data["tickets"].values() if t.get("bot_id") == c_id]
        status_emoji = "🟢" if clone.get("status") == "active" else "⏸️"

        bot_text = (
            f"🤖 **Bot Name:** {clone.get('name')}\n"
            f"🔗 **Username:** {clone.get('username')}\n"
            f"🆔 **Bot ID:** `{c_id}`\n"
            f"👤 **Owner ID:** `{clone.get('owner_id')}`\n"
            f"👥 **Users:** {len(users)}\n"
            f"🎫 **Tickets:** {len(tickets)}\n"
            f"**Status:** {status_emoji} {clone.get('status').upper()}\n"
            f"📅 **Created:** {clone.get('created_at')}"
        )

        pause_resume_btn = (
            InlineKeyboardButton("⏸️ Pause", callback_data=f"sup_pause_{c_id}")
            if clone.get("status") == "active"
            else InlineKeyboardButton("▶️ Resume", callback_data=f"sup_resume_{c_id}")
        )

        buttons = [
            [
                pause_resume_btn,
                InlineKeyboardButton("🗑️ Delete", callback_data=f"sup_del_{c_id}")
            ]
        ]

        await update.message.reply_text(bot_text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")

async def supreme_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != SUPREME_OWNER_ID:
        await query.answer("⚠️ Unauthorized.", show_alert=True)
        return

    await query.answer()
    data = query.data

    if data == "sup_bots":
        await supreme_bots_command(update, context)
    elif data == "sup_stats":
        clones = db.data["clones"]
        active_clones = sum(1 for c in clones.values() if c.get("status") == "active")
        paused_clones = len(clones) - active_clones
        total_users = len(db.data["users"])
        total_tickets = len(db.data["tickets"])
        banned_users = sum(1 for u in db.data["users"].values() if u.get("status") == "banned")

        stats_text = (
            "👑 **SUPREME STATISTICS**\n\n"
            f"🤖 Total Bots: {len(clones) + 1}\n"
            f"🟢 Active Bots: {active_clones + 1}\n"
            f"⏸️ Paused Bots: {paused_clones}\n"
            f"👥 Total Users: {total_users}\n"
            f"💬 Total Messages: {db.data['stats'].get('total_messages', 0)}\n"
            f"🎫 Total Tickets: {total_tickets}\n"
            f"📢 Total Broadcasts: {db.data['stats'].get('total_broadcasts', 0)}\n"
            f"🚫 Total Banned Users: {banned_users}"
        )
        await query.message.reply_text(stats_text, parse_mode="Markdown")

    elif data == "sup_forcesub":
        await forcesub_command(update, context)

    elif data == "sup_logs":
        logs = db.data.get("logs", [])[-10:]
        if not logs:
            await query.message.reply_text("📋 No log entries found.")
            return
        log_text = "📋 **REVIEWS & LOGS**\n\n"
        for l in logs:
            log_text += f"• `{l.get('timestamp')}` | **{l.get('type')}**: {l.get('details')}\n"
        await query.message.reply_text(log_text, parse_mode="Markdown")

    elif data.startswith("sup_pause_"):
        c_id = data.replace("sup_pause_", "")
        clone = db.get_clone(c_id)
        if clone:
            clone["status"] = "paused"
            await db.save()
            await stop_clone_instance(c_id)
            await query.message.reply_text(f"⏸️ Clone `{c_id}` paused successfully.", parse_mode="Markdown")

    elif data.startswith("sup_resume_"):
        c_id = data.replace("sup_resume_", "")
        clone = db.get_clone(c_id)
        if clone:
            clone["status"] = "active"
            await db.save()
            await start_clone_instance(clone)
            await query.message.reply_text(f"▶️ Clone `{c_id}` resumed successfully.", parse_mode="Markdown")

    elif data.startswith("sup_del_"):
        c_id = data.replace("sup_del_", "")
        if c_id in db.data["clones"]:
            await stop_clone_instance(c_id)
            del db.data["clones"][c_id]
            await db.save()
            await query.message.reply_text(f"🗑️ Clone `{c_id}` deleted permanently.", parse_mode="Markdown")

async def allbroadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != SUPREME_OWNER_ID:
        return

    if not update.message.reply_to_message and len(context.args) == 0:
        await update.message.reply_text("📢 Usage: Reply to message with `/allbroadcast` OR `/allbroadcast <text>`", parse_mode="Markdown")
        return

    all_users = list(db.data["users"].values())
    target_bots = len(db.data["clones"]) + 1

    status_msg = await update.message.reply_text(
        f"📢 **GLOBAL BROADCAST STARTED**\n\n"
        f"🤖 Target Bots: {target_bots}\n"
        f"👥 Target Users: {len(all_users)}\n"
        "Sending messages...",
        parse_mode="Markdown"
    )

    success, failed = 0, 0

    for u in all_users:
        uid = u["user_id"]
        bot_id = u["bot_id"]

        target_bot = context.bot
        if bot_id in active_clone_apps:
            target_bot = active_clone_apps[bot_id].bot

        try:
            if update.message.reply_to_message:
                await update.message.reply_to_message.copy(chat_id=uid)
            else:
                txt = " ".join(context.args)
                await target_bot.send_message(chat_id=uid, text=txt, parse_mode="Markdown")
            success += 1
        except Exception:
            failed += 1

        await asyncio.sleep(0.05)

    db.data["stats"]["total_broadcasts"] = db.data["stats"].get("total_broadcasts", 0) + 1
    await db.save()

    res_text = (
        "📢 **GLOBAL BROADCAST FINISHED**\n\n"
        f"🤖 Bots targeted: {target_bots}\n"
        f"👥 Users targeted: {len(all_users)}\n"
        f"✅ Successful: {success}\n"
        f"❌ Failed: {failed}"
    )
    await status_msg.edit_text(res_text, parse_mode="Markdown")

# FORCESUB SUPREME COMMANDS
async def forcesub_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != SUPREME_OWNER_ID:
        return

    fs = db.data.get("forcesub", {})
    status = "ENABLED" if fs.get("enabled", True) else "DISABLED"
    channels = fs.get("channels", [])

    text = f"👑 **FORCE-SUB PANEL**\n\n"
    text += f"📊 Status: **{status}**\n"
    text += f"📢 Required Channels: **{len(channels)}**\n\n"

    for idx, ch in enumerate(channels, 1):
        text += f"{idx}. **{ch.get('title', 'Channel')}** (`{ch.get('channel_id')}`)\n   Link: {ch.get('link')}\n"

    buttons = [
        [
            InlineKeyboardButton("➕ Add Channel", callback_data="fs_add_info"),
            InlineKeyboardButton("🗑️ Clear Channels", callback_data="fs_clear")
        ],
        [
            InlineKeyboardButton("🔘 Toggle Enable/Disable", callback_data="fs_toggle")
        ]
    ]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")

async def addforcesub_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != SUPREME_OWNER_ID:
        return

    if not context.args or len(context.args) < 3:
        await update.message.reply_text(
            "⚠️ Usage:\n`/addforcesub <channel_id/username> <title> <invite_link>`\n\n"
            "Example:\n`/addforcesub @MyChannel OfficialChannel https://t.me/MyChannel`",
            parse_mode="Markdown"
        )
        return

    ch_id = context.args[0]
    title = context.args[1]
    link = context.args[2]

    fs = db.data.setdefault("forcesub", {"enabled": True, "channels": []})
    fs["channels"].append({
        "channel_id": ch_id,
        "title": title,
        "link": link
    })
    await db.save()

    await update.message.reply_text(f"✅ Added ForceSub channel: **{title}** (`{ch_id}`)", parse_mode="Markdown")

async def removeforcesub_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != SUPREME_OWNER_ID:
        return

    if not context.args:
        await update.message.reply_text("⚠️ Usage: `/removeforcesub <channel_id>`", parse_mode="Markdown")
        return

    ch_id = context.args[0]
    fs = db.data.get("forcesub", {})
    channels = fs.get("channels", [])

    new_channels = [c for c in channels if str(c.get("channel_id")) != str(ch_id)]
    fs["channels"] = new_channels
    await db.save()

    await update.message.reply_text(f"✅ Removed channel `{ch_id}` from ForceSub requirements.", parse_mode="Markdown")

# -------------------------------------------------------------------
# HELP COMMAND
# -------------------------------------------------------------------
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    bot_id = str(context.bot.id)

    if user_id == SUPREME_OWNER_ID:
        help_text = (
            "👑 **SUPREME OWNER HELP**\n\n"
            "`/supreme` - Open Supreme Control Panel\n"
            "`/bots` - Manage all cloned bots\n"
            "`/allbroadcast` - Global message broadcast\n"
            "`/forcesub` - Manage ForceSub settings\n"
            "`/addforcesub` - Add ForceSub channel\n"
            "`/removeforcesub` - Remove ForceSub channel\n\n"
            "👮 **ADMIN COMMANDS**\n"
            "`/reply #ID text` - Reply to ticket\n"
            "`/ban ID` - Ban user\n"
            "`/unban ID` - Unban user\n"
            "`/user ID` - View user info\n"
            "`/stats` - View stats\n"
            "`/broadcast` - Broadcast to users"
        )
    elif is_admin(bot_id, user_id):
        help_text = (
            "👮 **ADMIN HELP MENU**\n\n"
            "`/reply #ID text` - Reply to user support ticket\n"
            "`/stats` - Support bot statistics\n"
            "`/broadcast` - Send message to all bot users\n"
            "`/ban USER_ID` - Ban user from support\n"
            "`/unban USER_ID` - Unban user\n"
            "`/user USER_ID` - View user profile info\n"
            "`/settings` - Clone bot settings"
        )
    else:
        help_text = (
            "🤖 **USER SUPPORT HELP**\n\n"
            "`/start` - Welcome message & menu\n"
            "Simply send any message (text, photo, video, document, voice, audio) to contact support team."
        )

    await update.message.reply_text(help_text, parse_mode="Markdown")

# -------------------------------------------------------------------
# CLONE & SETTING INPUT PROCESSORS
# -------------------------------------------------------------------
async def process_clone_token_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conv = user_conversations.get(user_id)

    if not conv or conv.get("state") != "AWAITING_TOKEN":
        if conv and conv.get("state", "").startswith("SETTING_"):
            await process_setting_input(update, context)
            return
        await handle_user_support_message(update, context)
        return

    token = update.message.text.strip()

    is_joined, unjoined = await check_user_forcesub(context.bot, user_id)
    if not is_joined:
        del user_conversations[user_id]
        kb = build_forcesub_keyboard(unjoined, user_id)
        await update.message.reply_text(
            "🔒 **ForceSub Requirement Failed**\nYou must stay joined in required channels to clone.",
            reply_markup=kb
        )
        return

    del user_conversations[user_id]
    status_msg = await update.message.reply_text("⏳ Validating bot token...")

    try:
        temp_bot = Bot(token=token)
        me = await temp_bot.get_me()
    except Exception as e:
        logger.warning("Invalid token attempt by user %s: %s", user_id, e)
        await status_msg.edit_text("❌ **Invalid Bot Token!**\n\nPlease check token from @BotFather and try again.")
        return

    clone_bot_id = str(me.id)

    if clone_bot_id in db.data["clones"]:
        await status_msg.edit_text("⚠️ This bot is already registered in our Clone System.")
        return

    clone_info = db.add_clone(
        bot_id=clone_bot_id,
        token=token,
        name=me.first_name,
        username=me.username or "None",
        owner_id=user_id
    )
    db.log_event("clone_created", f"User {user_id} cloned bot @{me.username} ({me.id})")
    await db.save()

    started = await start_clone_instance(clone_info)

    if started:
        success_text = (
            "✅ **Bot Cloned Successfully!**\n\n"
            f"🤖 Bot: @{me.username}\n"
            f"🆔 Bot ID: `{me.id}`\n\n"
            "Your support bot is now active and ready!\n"
            "Open your bot, send `/start`, and manage your clone using `/settings`."
        )
        await status_msg.edit_text(success_text, parse_mode="Markdown")
    else:
        await status_msg.edit_text("⚠️ Clone registered, but dynamic startup encountered an issue.")

async def start_clone_instance(clone_info: Dict[str, Any]) -> bool:
    bot_id = clone_info["bot_id"]
    token = clone_info["token"]

    if clone_info.get("status") == "paused":
        logger.info("Clone %s is paused; skipping startup.", bot_id)
        return False

    try:
        app = ApplicationBuilder().token(token).build()
        register_handlers(app)
        await app.initialize()
        await app.start()
        await app.updater.start_polling()
        active_clone_apps[bot_id] = app
        logger.info("Successfully started clone bot @%s (%s)", clone_info.get("username"), bot_id)
        return True
    except Exception as e:
        logger.error("Failed to start clone bot %s: %s", bot_id, e)
        return False

async def stop_clone_instance(bot_id: str):
    app = active_clone_apps.get(bot_id)
    if app:
        try:
            await app.updater.stop()
            await app.stop()
            await app.shutdown()
        except Exception as e:
            logger.error("Error stopping clone %s: %s", bot_id, e)
        finally:
            active_clone_apps.pop(bot_id, None)

async def clone_settings_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_id = str(context.bot.id)
    if not is_clone_owner(bot_id, update.effective_user.id):
        await update.message.reply_text("⚠️ Only the Clone Owner can access settings.")
        return

    clone = db.get_clone(bot_id)
    if not clone:
        await update.message.reply_text("⚠️ Clone settings not available for master bot.")
        return

    text = (
        "⚙️ **BOT SETTINGS**\n\n"
        f"🤖 Bot Name: {clone['name']}\n"
        f"🔗 Username: {clone['username']}\n"
        f"🆔 Bot ID: `{bot_id}`\n\n"
        "Configure your support bot parameters below:"
    )

    buttons = [
        [
            InlineKeyboardButton("✏️ Welcome Message", callback_data="set_welcome"),
            InlineKeyboardButton("📢 Community Channel", callback_data="set_channel")
        ],
        [
            InlineKeyboardButton("💬 Support Message", callback_data="set_suppmsg"),
            InlineKeyboardButton("🚫 Ban Message", callback_data="set_banmsg")
        ],
        [
            InlineKeyboardButton("🔄 Reset Settings", callback_data="set_reset")
        ]
    ]

    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons), parse_mode="Markdown")

async def settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    bot_id = str(context.bot.id)
    if not is_clone_owner(bot_id, query.from_user.id):
        await query.answer("⚠️ Unauthorized.", show_alert=True)
        return

    action = query.data
    user_id = query.from_user.id

    if action == "set_welcome":
        user_conversations[user_id] = {"state": "SETTING_WELCOME", "bot_id": bot_id}
        await query.message.reply_text("✏️ Please send the new **Welcome Message**:")
    elif action == "set_channel":
        user_conversations[user_id] = {"state": "SETTING_CHANNEL", "bot_id": bot_id}
        await query.message.reply_text("📢 Please send the new **Official Channel Link** (e.g. `https://t.me/...`):")
    elif action == "set_suppmsg":
        user_conversations[user_id] = {"state": "SETTING_SUPPMSG", "bot_id": bot_id}
        await query.message.reply_text("💬 Please send the new **Support Confirmation Message**:")
    elif action == "set_banmsg":
        user_conversations[user_id] = {"state": "SETTING_BANMSG", "bot_id": bot_id}
        await query.message.reply_text("🚫 Please send the new **Banned User Message**:")
    elif action == "set_reset":
        clone = db.get_clone(bot_id)
        if clone:
            clone["settings"] = {
                "welcome_message": "🚀 Welcome to our Support System!\n\nSend your message below and our support team will get back to you.",
                "official_channel": OFFICIAL_CHANNEL,
                "ban_message": "🚫 You are banned from using this support bot.",
                "support_message": "💬 Your message has been received! Our support team will reply shortly.",
                "support_channel_id": None
            }
            await db.save()
            await query.message.reply_text("✅ Settings reset to default.")

async def process_setting_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    conv = user_conversations.get(user_id)
    if not conv:
        return

    state = conv.get("state")
    bot_id = conv.get("bot_id", str(context.bot.id))
    clone = db.get_clone(bot_id)

    if not clone:
        del user_conversations[user_id]
        return

    val = update.message.text.strip()

    if state == "SETTING_WELCOME":
        clone["settings"]["welcome_message"] = val
        await update.message.reply_text("✅ Welcome Message updated!")
    elif state == "SETTING_CHANNEL":
        clone["settings"]["official_channel"] = val
        await update.message.reply_text("✅ Official Channel updated!")
    elif state == "SETTING_SUPPMSG":
        clone["settings"]["support_message"] = val
        await update.message.reply_text("✅ Support Message updated!")
    elif state == "SETTING_BANMSG":
        clone["settings"]["ban_message"] = val
        await update.message.reply_text("✅ Ban Message updated!")

    await db.save()
    del user_conversations[user_id]

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_id = str(context.bot.id)
    if not is_admin(bot_id, update.effective_user.id):
        return

    users = [u for u in db.data["users"].values() if u.get("bot_id") == bot_id]
    tickets = [t for t in db.data["tickets"].values() if t.get("bot_id") == bot_id]
    banned = [u for u in users if u.get("status") == "banned"]
    total_msgs = sum(u.get("message_count", 0) for u in users)

    text = (
        "📊 **SUPPORT BOT STATS**\n\n"
        f"👥 Total Users: {len(users)}\n"
        f"🟢 Active Users: {len(users) - len(banned)}\n"
        f"🚫 Banned Users: {len(banned)}\n"
        f"💬 Total Messages: {total_msgs}\n"
        f"🎫 Total Tickets: {len(tickets)}\n"
        f"🤖 Bot ID: `{bot_id}`"
    )

    await update.message.reply_text(text, parse_mode="Markdown")

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_id = str(context.bot.id)
    if not is_admin(bot_id, update.effective_user.id):
        return

    if not update.message.reply_to_message and len(context.args) == 0:
        await update.message.reply_text(
            "📢 Usage: Reply to a message with `/broadcast` OR `/broadcast <text>`",
            parse_mode="Markdown"
        )
        return

    users = [u for u in db.data["users"].values() if u.get("bot_id") == bot_id and u.get("status") != "banned"]

    status_msg = await update.message.reply_text("📢 Broadcast Started...")

    sent, failed, blocked = 0, 0, 0

    for u in users:
        uid = u["user_id"]
        try:
            if update.message.reply_to_message:
                await update.message.reply_to_message.copy(chat_id=uid)
            else:
                bc_text = " ".join(context.args)
                await context.bot.send_message(chat_id=uid, text=bc_text, parse_mode="Markdown")
            sent += 1
        except Forbidden:
            blocked += 1
        except Exception as e:
            failed += 1
        await asyncio.sleep(0.05)

    result_text = (
        "📢 **Broadcast Completed**\n\n"
        f"✅ Sent: {sent}\n"
        f"❌ Failed: {failed}\n"
        f"🚫 Blocked Users: {blocked}"
    )
    await status_msg.edit_text(result_text, parse_mode="Markdown")

async def global_message_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.is_bot:
        return

    user_id = user.id
    if user_id in user_conversations:
        conv = user_conversations[user_id]
        if conv.get("state") == "AWAITING_TOKEN":
            await process_clone_token_input(update, context)
            return
        elif conv.get("state", "").startswith("SETTING_"):
            await process_setting_input(update, context)
            return

    await handle_user_support_message(update, context)

def register_handlers(app: Application):
    # Public & General
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("settings", clone_settings_command))

    # Admin
    app.add_handler(CommandHandler("reply", reply_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("user", user_info_command))

    # Supreme Owner
    app.add_handler(CommandHandler("supreme", supreme_panel_command))
    app.add_handler(CommandHandler("bots", supreme_bots_command))
    app.add_handler(CommandHandler("allbroadcast", allbroadcast_command))
    app.add_handler(CommandHandler("forcesub", forcesub_command))
    app.add_handler(CommandHandler("addforcesub", addforcesub_command))
    app.add_handler(CommandHandler("removeforcesub", removeforcesub_command))

    # Callback Queries
    app.add_handler(CallbackQueryHandler(clone_start_callback, pattern="^btn_clone_start$"))
    app.add_handler(CallbackQueryHandler(check_forcesub_callback, pattern="^check_forcesub_"))
    app.add_handler(CallbackQueryHandler(reply_button_callback, pattern="^reply_t_"))
    app.add_handler(CallbackQueryHandler(ban_callback, pattern="^ban_u_"))
    app.add_handler(CallbackQueryHandler(user_info_callback, pattern="^info_u_"))
    app.add_handler(CallbackQueryHandler(settings_callback, pattern="^set_"))
    app.add_handler(CallbackQueryHandler(supreme_callback, pattern="^sup_"))

    # Messages
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND, global_message_router))

async def main():
    if BOT_TOKEN == "YOUR_BOT_TOKEN_HERE" or not BOT_TOKEN:
        logger.error("BOT_TOKEN is not configured! Please set BOT_TOKEN environment variable.")
        sys.exit(1)

    master_app = ApplicationBuilder().token(BOT_TOKEN).build()
    register_handlers(master_app)

    await master_app.initialize()
    await master_app.start()
    await master_app.updater.start_polling()

    logger.info("Master Bot started successfully.")

    for c_id, clone_data in db.data.get("clones", {}).items():
        if clone_data.get("status") == "active":
            logger.info("Starting active clone bot: %s (%s)", clone_data.get("username"), c_id)
            await start_clone_instance(clone_data)

    try:
        while True:
            await asyncio.sleep(3600)
    except (KeyboardInterrupt, SystemExit):
        logger.info("Shutdown signal received...")

    for c_id in list(active_clone_apps.keys()):
        await stop_clone_instance(c_id)

    await master_app.updater.stop()
    await master_app.stop()
    await master_app.shutdown()
    logger.info("All bots stopped gracefully.")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
