import os
import asyncio
import json
from datetime import date
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters,
    ContextTypes, ConversationHandler, CallbackQueryHandler
)
from telethon import TelegramClient, functions
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError

BOT_TOKEN = os.getenv("BOT_TOKEN")  # put your bot token here or in environment

# ----- In-memory storage -----
user_sessions = {}  # {telegram_user_id: (TelegramClient, api_id, api_hash)}
temp_login = {}     # Temporary storage during login

# ----- JSON storage -----
LIMITS_FILE = "user_limits.json"

# ----- Conversation steps -----
API_ID, API_HASH, PHONE, CODE, PASSWORD = range(5)

# ----- JSON utility functions -----
def load_limits():
    try:
        with open(LIMITS_FILE, "r") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_limits(limits):
    with open(LIMITS_FILE, "w") as f:
        json.dump(limits, f, indent=4)

def reset_daily_limits(limits):
    today = str(date.today())
    for user_id, data in limits.items():
        if data.get("last_reset") != today:
            data["groups_created"] = 0
            data["last_reset"] = today

def ensure_user(user_id, limits):
    user_id = str(user_id)
    if user_id not in limits:
        limits[user_id] = {"groups_created": 0, "last_reset": str(date.today()), "vip": False}

def can_create_group(user_id, limits):
    user_id = str(user_id)
    ensure_user(user_id, limits)
    user = limits[user_id]
    if user.get("vip"):
        return True
    return user["groups_created"] < 7

def increment_group_count(user_id, limits):
    user_id = str(user_id)
    limits[user_id]["groups_created"] += 1
    save_limits(limits)

# ----- Step-by-step login -----
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🌟 Welcome to the Advanced Group Creator Bot!\n\n"
        "Step 1: Enter your **API ID** (Go to https://my.telegram.org/apps → create an app → copy API ID):",
        parse_mode="Markdown"
    )
    return API_ID

async def api_id_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message.text.isdigit():
        await update.message.reply_text("❌ API ID must be a number. Please enter your API ID:")
        return API_ID
    user_id = update.effective_user.id
    temp_login[user_id] = {"api_id": int(update.message.text)}
    await update.message.reply_text(
        "Step 2: Enter your **API Hash** (Go to https://my.telegram.org/apps → copy API Hash):"
    )
    return API_HASH

async def api_hash_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    temp_login[user_id]["api_hash"] = update.message.text.strip()
    await update.message.reply_text(
        "Step 3: Enter your **phone number** (with country code, e.g., +251912345678):"
    )
    return PHONE

async def phone_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    temp_login[user_id]["phone"] = update.message.text.strip()
    data = temp_login[user_id]
    api_id = data["api_id"]
    api_hash = data["api_hash"]
    phone = data["phone"]

    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()

    try:
        await client.send_code_request(phone)
    except Exception as e:
        await update.message.reply_text(
            f"❌ Failed to send code: {e}\nMake sure your phone and API credentials are correct."
        )
        await client.disconnect()
        temp_login.pop(user_id, None)
        return ConversationHandler.END

    temp_login[user_id]["client"] = client
    await update.message.reply_text(
        "✅ Code sent! Step 4: Enter the code you received via Telegram (with spaces, e.g., `1 2 3 4 5 6`):"
    )
    return CODE

async def code_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    code = update.message.text.replace(" ", "")
    data = temp_login[user_id]
    client = data["client"]
    phone = data["phone"]

    try:
        await client.sign_in(phone=phone, code=code)
    except SessionPasswordNeededError:
        await update.message.reply_text("🔒 Two-Step Verification detected. Please enter your password:")
        return PASSWORD
    except Exception as e:
        await update.message.reply_text(f"❌ Login failed: {e}\nPlease try /start again.")
        await client.disconnect()
        temp_login.pop(user_id, None)
        return ConversationHandler.END

    user_sessions[user_id] = (client, data["api_id"], data["api_hash"])
    limits = load_limits()
    ensure_user(user_id, limits)
    save_limits(limits)
    temp_login.pop(user_id, None)
    me = await client.get_me()
    await update.message.reply_text(
        f"✅ Logged in successfully as **{me.first_name}**!\nYou can now create groups using `/creategroups <number>`",
        parse_mode="Markdown"
    )
    return ConversationHandler.END

async def password_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    password = update.message.text.strip()
    data = temp_login[user_id]
    client = data["client"]

    try:
        await client.sign_in(password=password)
    except Exception as e:
        await update.message.reply_text(f"❌ Login failed: {e}\nPlease try /start again.")
        await client.disconnect()
        temp_login.pop(user_id, None)
        return ConversationHandler.END

    user_sessions[user_id] = (client, data["api_id"], data["api_hash"])
    limits = load_limits()
    ensure_user(user_id, limits)
    save_limits(limits)
    temp_login.pop(user_id, None)
    me = await client.get_me()
    await update.message.reply_text(
        f"✅ Logged in successfully as **{me.first_name}**!\nYou can now create groups using `/creategroups <number>`",
        parse_mode="Markdown"
    )
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    temp_login.pop(update.effective_user.id, None)
    await update.message.reply_text("❌ Operation cancelled.")
    return ConversationHandler.END

# ----- /creategroups -----
async def creategroups(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in user_sessions:
        await update.message.reply_text("❌ You must first log in via /start.")
        return

    limits = load_limits()
    reset_daily_limits(limits)

    try:
        count = int(context.args[0])
    except:
        await update.message.reply_text("Usage: /creategroups <number>")
        return

    if not can_create_group(user_id, limits):
        await update.message.reply_text("⚠️ Daily limit reached. Upgrade to VIP to create more!")
        return

    client, _, _ = user_sessions[user_id]
    messages = [
        "🌟 Welcome to the group!",
        "🔥 Stay active and enjoy!",
        "💬 Feel free to chat with everyone.",
        "📢 Check pinned messages for updates.",
        "🎉 Have fun and invite your friends!",
        "✅ Automated message delivered."
    ]

    successes = 0
    for i in range(1, count + 1):
        if not can_create_group(user_id, limits):
            await update.message.reply_text(f"⚠️ Daily limit reached after creating {successes} groups.")
            break
        try:
            title = f"MyGroup {i}"
            result = await client(functions.channels.CreateChannelRequest(
                title=title,
                about="Created automatically",
                megagroup=True
            ))
            chat = result.chats[0]

            try:
                await client(functions.channels.TogglePreHistoryHiddenRequest(channel=chat, enabled=False))
            except Exception:
                pass

            for msg in messages:
                await client.send_message(chat, msg)
                await asyncio.sleep(1)

            successes += 1
            increment_group_count(user_id, limits)
        except Exception as e:
            await update.message.reply_text(f"Error creating group {i}: {e}")

    await update.message.reply_text(f"✅ Created {successes}/{count} groups successfully.")

# ----- Admin commands -----
async def addvip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = 7796215343
    if update.effective_user.id != admin_id:
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /addvip <user_id>")
        return
    user_id = str(context.args[0])
    limits = load_limits()
    ensure_user(user_id, limits)
    limits[user_id]["vip"] = True
    save_limits(limits)
    await update.message.reply_text(f"✅ User {user_id} is now VIP.")

async def removevip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admin_id = 7796215343
    if update.effective_user.id != admin_id:
        await update.message.reply_text("❌ You are not authorized to use this command.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /removevip <user_id>")
        return
    user_id = str(context.args[0])
    limits = load_limits()
    ensure_user(user_id, limits)
    limits[user_id]["vip"] = False
    save_limits(limits)
    await update.message.reply_text(f"✅ User {user_id} is no longer VIP.")

# ----- VIP subscription menu -----
async def vip_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton("I have paid", callback_data="paid")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        "💎 To become VIP, send 100 birr via TeleBirr to +251973101919 for weekly subscription.\n"
        "After paying, press the button below to confirm:", reply_markup=reply_markup
    )

async def vip_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    await query.edit_message_text(
        f"✅ Payment confirmation received!\nUserID {user_id} paid. Admin, please run `/addvip {user_id}` to grant VIP."
    )

# ----- Main -----
def main():
    if not os.path.exists(LIMITS_FILE):
        save_limits({})

    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            API_ID: [MessageHandler(filters.TEXT & ~filters.COMMAND, api_id_handler)],
            API_HASH: [MessageHandler(filters.TEXT & ~filters.COMMAND, api_hash_handler)],
            PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, phone_handler)],
            CODE: [MessageHandler(filters.TEXT & ~filters.COMMAND, code_handler)],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, password_handler)],
        },
        fallbacks=[CommandHandler("cancel", cancel)]
    )

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(conv_handler)
    app.add_handler(CommandHandler("creategroups", creategroups))
    app.add_handler(CommandHandler("addvip", addvip))
    app.add_handler(CommandHandler("removevip", removevip))
    app.add_handler(CommandHandler("vip", vip_menu))
    app.add_handler(CallbackQueryHandler(vip_callback, pattern="paid"))

    print("🤖 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
