import os
import sys
import time
import html
import logging
import re
import asyncio
import json
import base64
import random
import secrets
import subprocess
import threading
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest, TimedOut, NetworkError, RetryAfter
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, 
    MessageHandler, filters, ContextTypes, ConversationHandler
)
from telegram.request import HTTPXRequest

# Direct Bot Token for Termux & Render
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# ========== SILENCE ALL LOGS FOR CLEAN TERMINAL ==========
logging.basicConfig(level=logging.CRITICAL)
for log_name in ["httpx", "httpcore", "telegram", "telegram.ext", "asyncio"]:
    logging.getLogger(log_name).setLevel(logging.CRITICAL)
logger = logging.getLogger(__name__)

ADMIN_ID = 6976492778                    # ✅ Teri Admin ID
ADMIN_USERNAME = "@anuowns"              # ✅ Tera Username

CHANNELS = [
    {"username": "@ig_reset_sender", "link": "https://t.me/Ig_reset_sender"},
    {"username": "@ig_reset_backup", "link": "https://t.me/ig_reset_backup"},
    {"username": "@crustlized", "link": "https://t.me/crustlized"},
    {"username": "@anukehits", "link": "https://t.me/anukehits"}
]

# States for ConversationHandler
(
    CHOOSING_MODE, SINGLE_INPUT, MULTI_INPUT, REDEEM_INPUT,
    ADM_ADD_INPUT, ADM_CUT_INPUT, ADM_BAN_INPUT, ADM_CODE_INPUT, 
    ADM_BC_INPUT, ADM_LOOKUP_INPUT, ADM_SET_DAILY_INPUT, ADM_SET_REF_INPUT,
    ADM_DEL_CODE_INPUT
) = range(13)

# High concurrency: 25 resets can run in parallel without hanging Termux
MAX_PARALLEL_RESES = 25
CONCURRENCY_LIMIT = asyncio.Semaphore(MAX_PARALLEL_RESES)

# ========== HIGH-SPEED IN-MEMORY DATABASE WITH LOCK ==========
DB_FILE = "users_db.json"
DB_CACHE = None
JOIN_CACHE = {}  # user_id -> timestamp (caches channel check for 10 mins)

def load_db() -> dict:
    global DB_CACHE
    if DB_CACHE is not None:
        return DB_CACHE
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f:
                DB_CACHE = json.load(f)
                return DB_CACHE
        except Exception:
            pass
    DB_CACHE = {"users": {}, "codes": {}, "settings": {"daily_points": 50, "referral_points": 30}}
    return DB_CACHE

def save_db(data: dict = None):
    global DB_CACHE
    if data is not None:
        DB_CACHE = data
    if DB_CACHE is None:
        return
    try:
        temp_file = f"{DB_FILE}.tmp"
        with open(temp_file, "w") as f:
            json.dump(DB_CACHE, f, indent=4)
        os.replace(temp_file, DB_FILE)
    except Exception:
        pass

# ========== SAFE TELEGRAM HELPERS (NO CRASHES ON 100 USERS) ==========
async def safe_edit(message, text: str, reply_markup=None, parse_mode="HTML"):
    """Prevents 'Message is not modified' & FloodWait errors when multiple users click fast"""
    try:
        return await message.edit_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return None
        try:
            return await message.reply_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception:
            return None
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after)
        try:
            return await message.edit_text(text=text, reply_markup=reply_markup, parse_mode=parse_mode)
        except Exception:
            return None
    except Exception:
        return None

async def safe_answer(query, text: str = None, show_alert: bool = False):
    try:
        await query.answer(text=text, show_alert=show_alert)
    except Exception:
        pass

async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Catches any unexpected network or Telegram API glitches silently"""
    pass

# ========== SETTINGS & USER FUNCTIONS ==========
def get_settings() -> dict:
    data = load_db()
    if "settings" not in data:
        data["settings"] = {"daily_points": 50, "referral_points": 30}
        save_db(data)
    else:
        if "daily_points" not in data["settings"]:
            data["settings"]["daily_points"] = 50
        if "referral_points" not in data["settings"]:
            data["settings"]["referral_points"] = 30
    return data["settings"]

def get_daily_limit() -> int:
    return get_settings().get("daily_points", 50)

def set_daily_limit(val: int):
    data = load_db()
    if "settings" not in data:
        data["settings"] = {}
    data["settings"]["daily_points"] = val
    save_db(data)

def get_referral_reward() -> int:
    return get_settings().get("referral_points", 30)

def set_referral_reward(val: int):
    data = load_db()
    if "settings" not in data:
        data["settings"] = {}
    data["settings"]["referral_points"] = val
    save_db(data)

def check_and_give_daily(user_id: int, referred_by: str = None, user_obj=None) -> dict:
    data = load_db()
    if "users" not in data:
        data["users"] = {}
    if "codes" not in data:
        data["codes"] = {}
        
    settings = get_settings()
    daily_val = settings.get("daily_points", 50)
    uid = str(user_id)
    today = datetime.now().strftime("%Y-%m-%d")
    now_str = datetime.now().strftime("%I:%M:%S %p (%d %b)")
    now_ts = int(time.time())
    
    if uid not in data["users"]:
        full_name = user_obj.full_name if user_obj and user_obj.full_name else "User"
        uname = f"@{user_obj.username}" if user_obj and user_obj.username else "No Username"
        data["users"][uid] = {
            "points": daily_val, 
            "last_daily": today,
            "referred_by": referred_by,
            "referral_reward_claimed": False,
            "banned": False,
            "name": full_name,
            "username": uname,
            "last_active": now_str,
            "last_active_ts": now_ts,
            "synced": True if user_obj else False,
            "lifetime_checks": 0,
            "success_checks": 0,
            "failed_checks": 0,
            "referrals_count": 0,
            "account_type": "FREE"
        }
        save_db(data)
    else:
        u = data["users"][uid]
        needs_save = False
        if user_obj:
            new_name = user_obj.full_name or u.get("name", "User")
            new_uname = f"@{user_obj.username}" if user_obj.username else "No Username"
            if u.get("name") != new_name or u.get("username") != new_uname or not u.get("synced"):
                u["name"] = new_name
                u["username"] = new_uname
                u["synced"] = True
                needs_save = True

        # Throttle disk writes: only write activity timestamp to disk if > 30s elapsed
        if now_ts - u.get("last_active_ts", 0) > 30:
            needs_save = True
        u["last_active"] = now_str
        u["last_active_ts"] = now_ts

        for key, default_val in [("lifetime_checks", 0), ("success_checks", 0), ("failed_checks", 0), ("referrals_count", 0), ("account_type", "FREE")]:
            if key not in u:
                u[key] = default_val
                needs_save = True

        if u.get("banned", False):
            if needs_save:
                save_db(data)
            return u
            
        if u.get("last_daily") != today:
            current_pts = u["points"]
            if current_pts < daily_val:
                u["points"] = daily_val
            u["last_daily"] = today
            needs_save = True

        if needs_save:
            save_db(data)
            
    return data["users"][uid]

def track_activity(update: Update):
    user = update.effective_user
    if user:
        check_and_give_daily(user.id, user_obj=user)

async def sync_missing_profiles(bot):
    data = load_db()
    users = data.get("users", {})
    changed = False
    
    async def fetch_one(uid, udata):
        nonlocal changed
        try:
            chat = await bot.get_chat(int(uid))
            udata["name"] = chat.full_name or chat.first_name or "Telegram User"
            udata["username"] = f"@{chat.username}" if chat.username else "No Username"
            udata["synced"] = True
            changed = True
        except Exception:
            udata["name"] = udata.get("name") or "User"
            udata["username"] = udata.get("username") or "No Username"
            udata["synced"] = True
            changed = True

    tasks = [
        fetch_one(uid, udata)
        for uid, udata in users.items()
        if not udata.get("synced") or udata.get("name") in [None, "User"] or udata.get("username") in [None, "None"]
    ]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
    if changed:
        save_db(data)

def update_user_stats(user_id: int, points_diff: int, success: bool = None):
    data = load_db()
    uid = str(user_id)
    if uid in data.get("users", {}):
        data["users"][uid]["points"] += points_diff
        if success is not None:
            data["users"][uid]["lifetime_checks"] += 1
            if success:
                data["users"][uid]["success_checks"] += 1
            else:
                data["users"][uid]["failed_checks"] += 1
        save_db(data)

# ========== UTILS (PARALLEL CHANNEL CHECK) ==========
def is_valid_email(email: str) -> bool:
    pattern = r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"
    return bool(re.match(pattern, email))

async def check_join(user_id: int, bot, force_refresh: bool = False) -> bool:
    now_ts = time.time()
    if not force_refresh and user_id in JOIN_CACHE and (now_ts - JOIN_CACHE[user_id] < 600):
        return True

    async def check_single_channel(channel):
        try:
            member = await bot.get_chat_member(chat_id=channel["username"], user_id=user_id)
            return member.status in ["creator", "administrator", "member", "restricted"]
        except Exception:
            return False

    results = await asyncio.gather(*(check_single_channel(ch) for ch in CHANNELS))
    if all(results):
        JOIN_CACHE[user_id] = now_ts
        return True
    JOIN_CACHE.pop(user_id, None)
    return False

def get_join_keyboard():
    keyboard = [
        [
            InlineKeyboardButton("📢 Channel 1", url=CHANNELS[0]["link"], style="danger"),
            InlineKeyboardButton("📢 Channel 2", url=CHANNELS[1]["link"], style="danger")
        ],
        [
            InlineKeyboardButton("📢 Channel 3", url=CHANNELS[2]["link"], style="danger"),
            InlineKeyboardButton("📢 Channel 4", url=CHANNELS[3]["link"], style="danger")
        ],
        [
            InlineKeyboardButton("✅ I've Joined All", callback_data="verify_join", style="success")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_admin_keyboard():
    daily_val = get_daily_limit()
    ref_val = get_referral_reward()
    keyboard = [
        [
            InlineKeyboardButton("🟢 Live Users", callback_data="adm_live_btn", style="success"),
            InlineKeyboardButton("🔍 Lookup User", callback_data="adm_lookup_btn", style="primary")
        ],
        [
            InlineKeyboardButton(f"⚙️ Daily Pts: {daily_val}", callback_data="adm_set_daily_btn", style="primary"),
            InlineKeyboardButton(f"🎁 Refer Pts: {ref_val}", callback_data="adm_set_ref_btn", style="primary")
        ],
        [
            InlineKeyboardButton("🎟️ Create Code", callback_data="adm_code_btn", style="primary"),
            InlineKeyboardButton("🗑️ Delete/Stop Code", callback_data="adm_del_code_btn", style="danger")
        ],
        [
            InlineKeyboardButton("⛔ Ban / Unban", callback_data="adm_ban_btn", style="danger"),
            InlineKeyboardButton("➕ Add Points", callback_data="adm_add_btn", style="success")
        ],
        [
            InlineKeyboardButton("➖ Cut Points", callback_data="adm_cut_btn", style="danger"),
            InlineKeyboardButton("📢 Broadcast", callback_data="adm_bc_btn", style="primary")
        ],
        [
            InlineKeyboardButton("📊 Refresh Stats", callback_data="adm_stats", style="success"),
            InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_menu", style="danger")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

def get_back_admin_kb():
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Admin Panel", callback_data="open_admin_panel", style="danger")]])

# ========== COMMANDS ==========
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    db = load_db()
    users = db.get("users", {})
    
    if str(user_id) in users and users[str(user_id)].get("banned", False):
        if update.message:
            await update.message.reply_text("⛔ <b>Access Denied</b>\nYou have been banned from using this bot.", parse_mode="HTML")
        return CHOOSING_MODE
    
    referred_by = None
    if context.args and context.args[0].isdigit() and context.args[0] != str(user_id):
        referred_by = context.args[0]
        
    check_and_give_daily(user_id, referred_by, user_obj=user)
    
    if await check_join(user_id, context.bot):
        await show_modes(update, context)
        return CHOOSING_MODE
    else:
        if update.message:
            await update.message.reply_text("🔒 <b>JOIN REQUIRED</b>\n\nPlease join ALL our channels first to use the bot.", reply_markup=get_join_keyboard(), parse_mode="HTML")
        return CHOOSING_MODE

async def my_points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    u_data = check_and_give_daily(user.id, user_obj=user)
    if update.message:
        profile_text = (
            "📊 <b>YOUR STATISTICS</b>\n\n"
            f"💰 <b>Points Balance:</b> {u_data.get('points', 0)}\n"
            f"📈 <b>Lifetime checks done:</b> {u_data.get('lifetime_checks', 0)}\n"
            f"✅ <b>Successful / Found:</b> {u_data.get('success_checks', 0)}\n"
            f"❌ <b>Failed:</b> {u_data.get('failed_checks', 0)}\n"
            f"🎁 <b>Referrals:</b> {u_data.get('referrals_count', 0)}\n\n"
            f"👤 <b>Account:</b> {u_data.get('account_type', 'FREE')}"
        )
        await update.message.reply_text(profile_text, parse_mode="HTML")

# ========== ADMIN PANEL & FUNCTIONS ==========
def build_admin_text():
    db = load_db()
    users = db.get("users", {})
    codes = db.get("codes", {})
    total_users = len(users)
    banned_users = sum(1 for u in users.values() if u.get("banned", False))
    total_pts = sum(u.get("points", 0) for u in users.values())
    now_ts = int(time.time())
    live_count = sum(1 for u in users.values() if now_ts - u.get("last_active_ts", 0) <= 300)
    daily_val = get_daily_limit()
    ref_val = get_referral_reward()
    now_str = datetime.now().strftime("%I:%M:%S %p")
    
    active_codes_count = sum(1 for c in codes.values() if c.get("expires_at", 0) == 0 or c.get("expires_at", 0) > now_ts)
    
    return (
        "🛠️ <b>ADMIN CONTROL PANEL</b>\n\n"
        f"⚙️ <b>Daily Free Points:</b> {daily_val}\n"
        f"🎁 <b>Referral Reward:</b> {ref_val} Pts\n"
        f"🟢 <b>Live Now (5m):</b> {live_count} Users\n"
        f"👥 <b>Total Users:</b> {total_users}\n"
        f"💰 <b>Total Points:</b> {total_pts}\n"
        f"⛔ <b>Banned Users:</b> {banned_users}\n"
        f"🎟️ <b>Active/Total Codes:</b> {active_codes_count} / {len(codes)}\n"
        f"🕒 <b>Updated:</b> {now_str}\n\n"
        "👇 <b>Choose an action below:</b>"
    )

def build_live_users_text():
    db = load_db()
    users = db.get("users", {})
    now_ts = int(time.time())
    now_str = datetime.now().strftime("%I:%M:%S %p")

    if not users:
        return "👥 <b>No users found in database.</b>"

    sorted_users = sorted(
        users.items(),
        key=lambda item: item[1].get("last_active_ts", 0),
        reverse=True
    )

    live_count = sum(1 for _, u in sorted_users if now_ts - u.get("last_active_ts", 0) <= 300)

    lines = [
        f"📡 <b>LIVE & RECENT USERS</b>",
        f"🟢 <b>Currently Live (Last 5m):</b> {live_count} | 👥 <b>Total:</b> {len(users)}",
        f"🕒 <b>Checked at:</b> {now_str}\n"
    ]

    for uid, udata in sorted_users[:20]:
        is_live = (now_ts - udata.get("last_active_ts", 0)) <= 300
        status_icon = "🟢 <b>[LIVE]</b>" if is_live else "⚪"
        ban_tag = " ⛔<b>[BANNED]</b>" if udata.get("banned", False) else ""
        name = html.escape(str(udata.get("name", "User")))
        raw_uname = str(udata.get("username", "No Username"))
        uname = html.escape(raw_uname)
        pts = udata.get("points", 0)
        last_seen = udata.get("last_active") or f"Last Daily: {udata.get('last_daily', 'Unknown')}"

        lines.append(
            f"{status_icon} <a href='tg://user?id={uid}'><b>{name}</b></a> ({uname}){ban_tag}\n"
            f"├ 🆔 ID: <code>{uid}</code> | 💰 Pts: <b>{pts}</b>\n"
            f"└ 🕒 Seen: <i>{last_seen}</i>\n"
        )

    if len(sorted_users) > 20:
        lines.append(f"<i>...showing top 20 recent users out of {len(sorted_users)}. Use Lookup for specific user.</i>")

    return "\n".join(lines)

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_activity(update)
    user_id = update.effective_user.id if update.effective_user else update.callback_query.from_user.id
    if user_id != ADMIN_ID:
        return CHOOSING_MODE
        
    text = build_admin_text()
    markup = get_admin_keyboard()
    
    if update.callback_query:
        await safe_edit(update.callback_query.message, text, reply_markup=markup)
    elif update.message:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")
    return CHOOSING_MODE

# --- BUTTON-BASED ADMIN INPUT PROCESSORS ---
async def process_adm_set_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return CHOOSING_MODE
    try:
        val = int(update.message.text.strip())
        if val < 0:
            raise ValueError()
        set_daily_limit(val)
        await update.message.reply_text(
            f"✅ <b>Daily Free Points Updated Successfully!</b>\nNew daily limit set to: <b>{val} points</b>",
            reply_markup=get_back_admin_kb(), parse_mode="HTML"
        )
        return CHOOSING_MODE
    except Exception:
        await update.message.reply_text(
            "⚠️ <b>Invalid Number!</b>\nPlease send a valid numeric value for daily points (e.g. <code>50</code> or <code>30</code>):",
            reply_markup=get_back_admin_kb(), parse_mode="HTML"
        )
        return ADM_SET_DAILY_INPUT

async def process_adm_set_ref(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return CHOOSING_MODE
    try:
        val = int(update.message.text.strip())
        if val < 0:
            raise ValueError()
        set_referral_reward(val)
        await update.message.reply_text(
            f"✅ <b>Referral Reward Points Updated!</b>\nNew referral reward set to: <b>{val} points</b>",
            reply_markup=get_back_admin_kb(), parse_mode="HTML"
        )
        return CHOOSING_MODE
    except Exception:
        await update.message.reply_text(
            "⚠️ <b>Invalid Number!</b>\nPlease send a valid numeric value for referral points (e.g. <code>30</code> or <code>20</code>):",
            reply_markup=get_back_admin_kb(), parse_mode="HTML"
        )
        return ADM_SET_REF_INPUT

async def process_adm_del_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return CHOOSING_MODE
    code_name = update.message.text.strip().upper()
    db = load_db()
    codes = db.get("codes", {})
    
    if code_name in codes:
        del codes[code_name]
        save_db(db)
        await update.message.reply_text(
            f"🗑️ <b>Code Deleted Successfully!</b>\nCode <code>{code_name}</code> has been stopped and removed permanently.",
            reply_markup=get_back_admin_kb(), parse_mode="HTML"
        )
    else:
        await update.message.reply_text(
            f"❌ <b>Code Not Found!</b>\nNo redeem code named <code>{code_name}</code> exists in database.",
            reply_markup=get_back_admin_kb(), parse_mode="HTML"
        )
    return CHOOSING_MODE

async def process_adm_lookup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return CHOOSING_MODE
    await sync_missing_profiles(context.bot)
    query_str = update.message.text.strip()
    db = load_db()
    users = db.get("users", {})
    
    found_uid = None
    found_data = None
    
    if query_str in users:
        found_uid = query_str
        found_data = users[query_str]
    else:
        search_uname = query_str.lower()
        if not search_uname.startswith("@"):
            search_uname = "@" + search_uname
        for uid, udata in users.items():
            if str(udata.get("username", "")).lower() == search_uname:
                found_uid = uid
                found_data = udata
                break

    if not found_data:
        await update.message.reply_text(
            f"❌ <b>User Not Found!</b>\nNo user matching <code>{html.escape(query_str)}</code> in database.",
            reply_markup=get_back_admin_kb(), parse_mode="HTML"
        )
        return ADM_LOOKUP_INPUT

    now_ts = int(time.time())
    is_live = (now_ts - found_data.get("last_active_ts", 0)) <= 300
    live_str = "🟢 LIVE NOW (Active in last 5m)" if is_live else "⚪ Offline"
    ban_str = "⛔ Yes (Banned)" if found_data.get("banned", False) else "🟢 No (Active)"
    name = html.escape(str(found_data.get("name", "User")))
    uname = html.escape(str(found_data.get("username", "No Username")))
    pts = found_data.get("points", 0)
    last_seen = found_data.get("last_active") or f"Last Daily: {found_data.get('last_daily', 'Unknown')}"

    info_text = (
        "🔍 <b>USER LOOKUP DETAILS</b>\n\n"
        f"👤 <b>Name:</b> <a href='tg://user?id={found_uid}'>{name}</a>\n"
        f"🔗 <b>Username:</b> {uname}\n"
        f"🆔 <b>Chat ID:</b> <code>{found_uid}</code>\n"
        f"💰 <b>Points Balance:</b> <b>{pts}</b>\n"
        f"📈 <b>Lifetime Checks:</b> {found_data.get('lifetime_checks', 0)}\n"
        f"✅ <b>Successful:</b> {found_data.get('success_checks', 0)}\n"
        f"📡 <b>Status:</b> {live_str}\n"
        f"🕒 <b>Last Active:</b> {last_seen}\n"
        f"⛔ <b>Banned:</b> {ban_str}"
    )
    await update.message.reply_text(info_text, reply_markup=get_back_admin_kb(), parse_mode="HTML")
    return CHOOSING_MODE

async def process_adm_add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return CHOOSING_MODE
    try:
        parts = update.message.text.strip().split()
        target = str(parts[0])
        amt = int(parts[1])
        db = load_db()
        if "users" not in db: db["users"] = {}
        if target not in db["users"]:
            db["users"][target] = {"points": amt, "last_daily": "", "banned": False, "name": "User", "username": "No Username", "synced": False, "lifetime_checks": 0, "success_checks": 0, "failed_checks": 0, "referrals_count": 0, "account_type": "FREE"}
        else:
            db["users"][target]["points"] += amt
        save_db(db)
        await update.message.reply_text(
            f"✅ Added <b>{amt}</b> points to <code>{target}</code>.\n💰 New Balance: <b>{db['users'][target]['points']}</b>",
            reply_markup=get_back_admin_kb(), parse_mode="HTML"
        )
        try:
            await context.bot.send_message(chat_id=int(target), text=f"🎉 Received <b>{amt}</b> points from Admin!", parse_mode="HTML")
        except Exception:
            pass
        return CHOOSING_MODE
    except Exception:
        await update.message.reply_text(
            "⚠️ <b>Invalid Format!</b>\nSend User ID and Amount separated by space.\nExample: <code>123456789 100</code>",
            reply_markup=get_back_admin_kb(), parse_mode="HTML"
        )
        return ADM_ADD_INPUT

async def process_adm_cut(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return CHOOSING_MODE
    try:
        parts = update.message.text.strip().split()
        target = str(parts[0])
        amt = int(parts[1])
        db = load_db()
        users = db.get("users", {})
        if target in users:
            users[target]["points"] = max(0, users[target]["points"] - amt)
            save_db(db)
            await update.message.reply_text(
                f"✅ Cut <b>{amt}</b> points from <code>{target}</code>.\n💰 New Balance: <b>{users[target]['points']}</b>",
                reply_markup=get_back_admin_kb(), parse_mode="HTML"
            )
        else:
            await update.message.reply_text("❌ User not found in database.", reply_markup=get_back_admin_kb(), parse_mode="HTML")
        return CHOOSING_MODE
    except Exception:
        await update.message.reply_text(
            "⚠️ <b>Invalid Format!</b>\nSend User ID and Amount separated by space.\nExample: <code>123456789 50</code>",
            reply_markup=get_back_admin_kb(), parse_mode="HTML"
        )
        return ADM_CUT_INPUT

async def process_adm_ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return CHOOSING_MODE
    target = update.message.text.strip()
    db = load_db()
    users = db.get("users", {})
    if target in users:
        status = users[target].get("banned", False)
        users[target]["banned"] = not status
        save_db(db)
        msg = "Banned ⛔" if users[target]["banned"] else "Unbanned 🟢"
        await update.message.reply_text(
            f"✅ User <code>{target}</code> is now <b>{msg}</b>",
            reply_markup=get_back_admin_kb(), parse_mode="HTML"
        )
    else:
        await update.message.reply_text("❌ User not found in database.", reply_markup=get_back_admin_kb(), parse_mode="HTML")
    return CHOOSING_MODE

async def process_adm_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return CHOOSING_MODE
    try:
        parts = update.message.text.strip().split()
        code = parts[0].upper()
        points = int(parts[1])
        minutes = int(parts[2]) if len(parts) > 2 else 0
        expires_at = int(time.time()) + (minutes * 60) if minutes > 0 else 0
        
        db = load_db()
        if "codes" not in db: db["codes"] = {}
        
        db["codes"][code] = {
            "points": points, 
            "expires_at": expires_at,
            "used_by": []
        }
        save_db(db)
        
        expiry_str = f"{minutes} Minutes" if minutes > 0 else "Lifetime (Never expires)"
        await update.message.reply_text(
            f"✅ <b>Redeem Code Created Successfully!</b>\n\n🎟️ <b>Code:</b> <code>{code}</code>\n💰 <b>Points:</b> {points}\n⏳ <b>Validity:</b> {expiry_str}",
            reply_markup=get_back_admin_kb(), parse_mode="HTML"
        )
        return CHOOSING_MODE
    except Exception:
        await update.message.reply_text(
            "⚠️ <b>Invalid Format!</b>\nSend Code, Points and Minutes separated by space.\nExample: <code>ANU50 50 15</code> (15 mins valid)\nOr: <code>ANU50 50 0</code> (Lifetime valid)",
            reply_markup=get_back_admin_kb(), parse_mode="HTML"
        )
        return ADM_CODE_INPUT

async def process_adm_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return CHOOSING_MODE
    msg_text = update.message.text
    db = load_db()
    users = db.get("users", {})
    success, failed = 0, 0
    
    status_msg = await update.message.reply_text("⏳ <b>Broadcasting message to all users...</b>", parse_mode="HTML")
    for uid in list(users.keys()):
        try:
            await context.bot.send_message(chat_id=int(uid), text=msg_text, parse_mode="HTML")
            success += 1
            await asyncio.sleep(0.04)
        except Exception:
            failed += 1
            
    await safe_edit(
        status_msg,
        f"📢 <b>Broadcast Completed!</b>\n\n✅ <b>Sent:</b> {success}\n❌ <b>Failed:</b> {failed}",
        reply_markup=get_back_admin_kb()
    )
    return CHOOSING_MODE

# --- DIRECT SLASH COMMAND HANDLERS (OPTIONAL SHORTCUTS) ---
async def admin_add_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    try:
        target, amt = str(context.args[0]), int(context.args[1])
        db = load_db()
        if "users" not in db: db["users"] = {}
        if target not in db["users"]:
            db["users"][target] = {"points": amt, "last_daily": "", "banned": False}
        else:
            db["users"][target]["points"] += amt
        save_db(db)
        await update.message.reply_text(f"✅ Added {amt} points to <code>{target}</code>.\nNew Balance: {db['users'][target]['points']}", parse_mode="HTML")
    except Exception:
        await update.message.reply_text("⚠️ Format: <code>/add USER_ID AMOUNT</code>", parse_mode="HTML")

async def admin_cut_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    try:
        target, amt = str(context.args[0]), int(context.args[1])
        db = load_db()
        users = db.get("users", {})
        if target in users:
            users[target]["points"] = max(0, users[target]["points"] - amt)
            save_db(db)
            await update.message.reply_text(f"✅ Cut {amt} points from <code>{target}</code>.\nNew Balance: {users[target]['points']}", parse_mode="HTML")
        else:
            await update.message.reply_text("❌ User not found.", parse_mode="HTML")
    except Exception:
        await update.message.reply_text("⚠️ Format: <code>/cut USER_ID AMOUNT</code>", parse_mode="HTML")

async def admin_ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    try:
        target = str(context.args[0])
        db = load_db()
        users = db.get("users", {})
        if target in users:
            users[target]["banned"] = not users[target].get("banned", False)
            save_db(db)
            msg = "Banned ⛔" if users[target]["banned"] else "Unbanned 🟢"
            await update.message.reply_text(f"✅ User <code>{target}</code> is now <b>{msg}</b>", parse_mode="HTML")
        else:
            await update.message.reply_text("❌ User not found.", parse_mode="HTML")
    except Exception:
        await update.message.reply_text("⚠️ Format: <code>/ban USER_ID</code>", parse_mode="HTML")

async def admin_create_code_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    try:
        code = context.args[0].strip().upper()
        points = int(context.args[1])
        minutes = int(context.args[2]) if len(context.args) > 2 else 0
        expires_at = int(time.time()) + (minutes * 60) if minutes > 0 else 0
        
        db = load_db()
        if "codes" not in db: db["codes"] = {}
        db["codes"][code] = {"points": points, "expires_at": expires_at, "used_by": []}
        save_db(db)
        await update.message.reply_text(f"✅ <b>Code Created!</b>\n🎟️ <code>{code}</code> ({points} pts, {minutes}m validity)", parse_mode="HTML")
    except Exception:
        await update.message.reply_text("⚠️ Use: <code>/createcode ANU50 50 15</code>", parse_mode="HTML")

async def admin_broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or not context.args: return
    msg_text = " ".join(context.args)
    db = load_db()
    users = db.get("users", {})
    success, failed = 0, 0
    status_msg = await update.message.reply_text("⏳ <b>Broadcasting...</b>", parse_mode="HTML")
    for uid in list(users.keys()):
        try:
            await context.bot.send_message(chat_id=int(uid), text=msg_text, parse_mode="HTML")
            success += 1
            await asyncio.sleep(0.04)
        except Exception:
            failed += 1
    await safe_edit(status_msg, f"📢 <b>Broadcast Done!</b>\n✅ Sent: {success} | ❌ Failed: {failed}")

# ========== MENUS & HANDLERS ==========
async def show_modes(update: Update, context: ContextTypes.DEFAULT_TYPE, message_text: str = None):
    user = update.effective_user
    user_id = user.id if user else update.callback_query.from_user.id
    u_data = check_and_give_daily(user_id, user_obj=user)
    pts = u_data.get('points', 0)
    
    keyboard = [
        [
            InlineKeyboardButton("📩 Single Reset", callback_data="single", style="primary"),
            InlineKeyboardButton("📨 Bulk Reset", callback_data="multi", style="primary")
        ],
        [
            InlineKeyboardButton("💳 My Profile", callback_data="check_points", style="success"),
            InlineKeyboardButton("💎 Buy Points", callback_data="buy_points", style="success")
        ],
        [
            InlineKeyboardButton("🎁 Refer & Earn", callback_data="refer", style="success"),
            InlineKeyboardButton("🎟️ Redeem Code", callback_data="redeem_menu", style="success")
        ],
        [
            InlineKeyboardButton("👨‍💻 Contact Admin", url=f"https://t.me/{ADMIN_USERNAME.replace('@', '')}", style="danger")
        ]
    ]
    
    if user_id == ADMIN_ID:
        keyboard.insert(0, [InlineKeyboardButton("🛠️ Admin Panel", callback_data="open_admin_panel", style="danger")])
        
    markup = InlineKeyboardMarkup(keyboard)
    text = message_text or f"🤖 <b>Welcome to Ig Reset Bot!</b>\n\n💰 <b>Available Points:</b> {pts}\n\n👇 <b>Select your option below:</b>"
    
    if update.callback_query:
        await safe_edit(update.callback_query.message, text, reply_markup=markup)
    elif update.message:
        await update.message.reply_text(text, reply_markup=markup, parse_mode="HTML")

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data
    user = query.from_user
    user_id = user.id
    track_activity(update)

    if data == "verify_join":
        if await check_join(user_id, context.bot, force_refresh=True):
            await safe_answer(query, "✅ Verification Successful!")
            db = load_db()
            users = db.get("users", {})
            uid = str(user_id)
            
            ref_reward_pts = get_referral_reward()
            if users.get(uid, {}).get("referred_by") and not users[uid].get("referral_reward_claimed"):
                referrer_id = users[uid]["referred_by"]
                if referrer_id in users:
                    users[referrer_id]["points"] += ref_reward_pts
                    users[referrer_id]["referrals_count"] = users[referrer_id].get("referrals_count", 0) + 1
                    users[uid]["referral_reward_claimed"] = True
                    save_db(db)
                    try:
                        await context.bot.send_message(
                            chat_id=int(referrer_id),
                            text=f"🎉 <b>Referral Success!</b>\nA new user joined channels using your link. You received <b>{ref_reward_pts} Points</b>! 🎁",
                            parse_mode="HTML"
                        )
                    except Exception:
                        pass

            try:
                await query.message.delete()
            except Exception:
                pass
            await show_modes(update, context, "🎉 <b>Verification Successful! System Ready.</b>")
        else:
            await safe_answer(query, "❌ You haven't joined all channels yet!", show_alert=True)
        return CHOOSING_MODE

    # --- ADMIN PANEL CALLBACKS ---
    elif data == "open_admin_panel" and user_id == ADMIN_ID:
        await safe_answer(query)
        await admin_panel(update, context)
        return CHOOSING_MODE

    elif data == "adm_stats" and user_id == ADMIN_ID:
        await safe_answer(query, "✅ Stats Refreshed!")
        await safe_edit(query.message, build_admin_text(), reply_markup=get_admin_keyboard())
        return CHOOSING_MODE

    elif data == "adm_set_daily_btn" and user_id == ADMIN_ID:
        await safe_answer(query)
        current_val = get_daily_limit()
        await safe_edit(
            query.message,
            f"⚙️ <b>CHANGE DAILY FREE POINTS</b>\n\nCurrent limit is: <b>{current_val} points/day</b>\n\nSend the new daily points number you want to set:",
            reply_markup=get_back_admin_kb()
        )
        return ADM_SET_DAILY_INPUT

    elif data == "adm_set_ref_btn" and user_id == ADMIN_ID:
        await safe_answer(query)
        current_ref = get_referral_reward()
        await safe_edit(
            query.message,
            f"🎁 <b>CHANGE REFERRAL REWARD</b>\n\nCurrent reward is: <b>{current_ref} points per referral</b>\n\nSend the new referral points reward number you want to set:",
            reply_markup=get_back_admin_kb()
        )
        return ADM_SET_REF_INPUT

    elif data == "adm_live_btn" and user_id == ADMIN_ID:
        await safe_answer(query, "📡 Fetching User Profiles...")
        await sync_missing_profiles(context.bot)
        live_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("🔄 Refresh Live List", callback_data="adm_live_btn", style="success")],
            [InlineKeyboardButton("🔍 Lookup Specific User", callback_data="adm_lookup_btn", style="primary")],
            [InlineKeyboardButton("⬅️ Back to Admin Panel", callback_data="open_admin_panel", style="danger")]
        ])
        await safe_edit(query.message, build_live_users_text(), reply_markup=live_kb)
        return CHOOSING_MODE

    elif data == "adm_lookup_btn" and user_id == ADMIN_ID:
        await safe_answer(query)
        await safe_edit(
            query.message,
            "🔍 <b>USER LOOKUP</b>\n\nSend the user's <b>Chat ID</b> or <b>@Username</b> to check their stats and live status:\nExample: <code>6976492778</code> or <code>@anuowns</code>",
            reply_markup=get_back_admin_kb()
        )
        return ADM_LOOKUP_INPUT

    elif data == "adm_add_btn" and user_id == ADMIN_ID:
        await safe_answer(query)
        await safe_edit(
            query.message,
            "➕ <b>ADD POINTS</b>\n\nSend <b>User ID</b> and <b>Points</b> separated by a space:\nExample: <code>6976492778 100</code>",
            reply_markup=get_back_admin_kb()
        )
        return ADM_ADD_INPUT

    elif data == "adm_cut_btn" and user_id == ADMIN_ID:
        await safe_answer(query)
        await safe_edit(
            query.message,
            "➖ <b>CUT POINTS</b>\n\nSend <b>User ID</b> and <b>Points</b> to deduct:\nExample: <code>6976492778 50</code>",
            reply_markup=get_back_admin_kb()
        )
        return ADM_CUT_INPUT

    elif data == "adm_ban_btn" and user_id == ADMIN_ID:
        await safe_answer(query)
        await safe_edit(
            query.message,
            "⛔ <b>BAN / UNBAN USER</b>\n\nSend the <b>User ID</b> you want to Ban or Unban:\nExample: <code>6976492778</code>",
            reply_markup=get_back_admin_kb()
        )
        return ADM_BAN_INPUT

    elif data == "adm_code_btn" and user_id == ADMIN_ID:
        await safe_answer(query)
        await safe_edit(
            query.message,
            "🎟️ <b>CREATE REDEEM CODE</b>\n\nSend Code, Points, and Validity (Minutes) separated by space:\nExample: <code>ANU50 50 15</code> (15 mins)\nOr: <code>ANU50 50 0</code> (Lifetime)",
            reply_markup=get_back_admin_kb()
        )
        return ADM_CODE_INPUT

    elif data == "adm_del_code_btn" and user_id == ADMIN_ID:
        await safe_answer(query)
        db = load_db()
        codes = db.get("codes", {})
        code_list_str = "\n".join([f"• <code>{c}</code> (Pts: {d.get('points')}, Used: {len(d.get('used_by', []))})" for c, d in codes.items()]) or "No active codes."
        
        await safe_edit(
            query.message,
            f"🗑️ <b>DELETE / STOP REDEEM CODE</b>\n\nActive Codes:\n{code_list_str}\n\n<b>Send the exact Code Name you want to delete/stop right now:</b>",
            reply_markup=get_back_admin_kb()
        )
        return ADM_DEL_CODE_INPUT

    elif data == "adm_bc_btn" and user_id == ADMIN_ID:
        await safe_answer(query)
        await safe_edit(
            query.message,
            "📢 <b>BROADCAST MESSAGE</b>\n\nSend the text message you want to broadcast to all bot users:",
            reply_markup=get_back_admin_kb()
        )
        return ADM_BC_INPUT
    # -----------------------------

    elif data == "single":
        await safe_answer(query)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_menu", style="danger")]])
        await safe_edit(query.message, "📩 <b>SINGLE MODE</b>\n\n<b>Send me the target email address:</b>\n<i>(Cost: 1 Point)</i>", reply_markup=kb)
        return SINGLE_INPUT

    elif data == "multi":
        await safe_answer(query)
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_menu", style="danger")]])
        await safe_edit(query.message, "📨 <b>MULTI MODE</b>\n\n<b>Send multiple emails (Max 20):</b>\n<i>(Cost: 1 Point per email)</i>", reply_markup=kb)
        return MULTI_INPUT
        
    elif data == "check_points":
        await safe_answer(query)
        u_data = check_and_give_daily(user_id, user_obj=user)
        profile_text = (
            "📊 <b>YOUR STATISTICS</b>\n\n"
            f"💰 <b>Points Balance:</b> {u_data.get('points', 0)}\n"
            f"📈 <b>Lifetime checks done:</b> {u_data.get('lifetime_checks', 0)}\n"
            f"✅ <b>Successful / Found:</b> {u_data.get('success_checks', 0)}\n"
            f"❌ <b>Failed:</b> {u_data.get('failed_checks', 0)}\n"
            f"🎁 <b>Referrals:</b> {u_data.get('referrals_count', 0)}\n\n"
            f"👤 <b>Account:</b> {u_data.get('account_type', 'FREE')}"
        )
        keyboard = [[InlineKeyboardButton("⬅️ Main Menu", callback_data="back_to_menu", style="danger")]]
        await safe_edit(query.message, profile_text, reply_markup=InlineKeyboardMarkup(keyboard))
        return CHOOSING_MODE

    elif data == "buy_points":
        await safe_answer(query)
        rates_text = (
            "💎 <b>BUY POINTS - RATE CARD</b>\n\n"
            "💵 <b>₹20 = 100 Points</b>\n"
            "💵 <b>₹50 = 300 Points</b>\n"
            "💵 <b>₹100 = 800 Points</b>\n\n"
            f"📲 <b>Message Admin to buy:</b> {ADMIN_USERNAME}"
        )
        keyboard = [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_menu", style="danger")]]
        await safe_edit(query.message, rates_text, reply_markup=InlineKeyboardMarkup(keyboard))
        return CHOOSING_MODE
        
    elif data == "refer":
        await safe_answer(query)
        bot_username = context.bot.username
        ref_link = f"https://t.me/{bot_username}?start={user_id}"
        u_data = check_and_give_daily(user_id, user_obj=user)
        ref_val = get_referral_reward()
        ref_text = (
            "🎁 <b>REFER & EARN</b>\n\n"
            f"Total Referrals: <b>{u_data.get('referrals_count', 0)}</b>\n"
            f"Invite your friends and earn <b>{ref_val} Points</b> for every valid referral!\n\n"
            f"🔗 <b>Your Referral Link:</b>\n<code>{ref_link}</code>"
        )
        keyboard = [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_menu", style="danger")]]
        await safe_edit(query.message, ref_text, reply_markup=InlineKeyboardMarkup(keyboard))
        return CHOOSING_MODE

    elif data == "redeem_menu":
        await safe_answer(query)
        redeem_text = (
            "🎟️ <b>REDEEM COUPON CODE</b>\n\n"
            "Have a special coupon code? Send it below in the chat to claim your free points!"
        )
        keyboard = [[InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_menu", style="danger")]]
        await safe_edit(query.message, redeem_text, reply_markup=InlineKeyboardMarkup(keyboard))
        return REDEEM_INPUT

    elif data == "back_to_menu":
        await safe_answer(query)
        await show_modes(update, context)
        return CHOOSING_MODE

    await safe_answer(query)
    return CHOOSING_MODE

# ========== REDEEM PROCESSOR ==========
async def process_redeem(update: Update, context: ContextTypes.DEFAULT_TYPE):
    track_activity(update)
    user_id = update.effective_user.id
    code = update.message.text.strip().upper()
    
    db = load_db()
    codes = db.get("codes", {})
    
    if code not in codes:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_menu", style="danger")]])
        await update.message.reply_text("❌ <b>Invalid Code!</b>\nThis coupon code does not exist.", reply_markup=kb, parse_mode="HTML")
        return REDEEM_INPUT
        
    code_data = codes[code]
    
    expires_at = code_data.get("expires_at", 0)
    if expires_at > 0 and int(time.time()) > expires_at:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_menu", style="danger")]])
        await update.message.reply_text("⏰ <b>Code Expired!</b>\nThis coupon code has expired and is no longer active.", reply_markup=kb, parse_mode="HTML")
        return REDEEM_INPUT

    if user_id in code_data.get("used_by", []):
        await update.message.reply_text("⚠️ <b>Already Redeemed!</b>\nYou have already used this coupon code.", parse_mode="HTML")
        await show_modes(update, context)
        return CHOOSING_MODE
        
    points_to_add = code_data["points"]
    
    users = db.get("users", {})
    uid_str = str(user_id)
    if uid_str in users:
        users[uid_str]["points"] += points_to_add
    else:
        users[uid_str] = {
            "points": points_to_add, 
            "last_daily": datetime.now().strftime("%Y-%m-%d"),
            "lifetime_checks": 0, "success_checks": 0, "failed_checks": 0, "referrals_count": 0, "account_type": "FREE", "banned": False
        }
        
    code_data["used_by"].append(user_id)
    save_db(db)
    
    total_redeemed_count = len(code_data["used_by"])
    await update.message.reply_text(
        f"🎉 <b>Redeem Successful!</b>\n\nYou successfully claimed <b>{points_to_add} Points</b> using code <code>{code}</code>!\n👥 <i>Total people redeemed this code: {total_redeemed_count}</i>",
        parse_mode="HTML"
    )
    await show_modes(update, context, "<b>What would you like to do next?</b>")
    return CHOOSING_MODE

# ========== INSTAGRAM RECOVERY ENGINE (merged from anu.py - single file) ==========
# Tokenless bloks POST to i.instagram.com. Every request round-robins through
# alive_proxies.txt (written by proxiech.py) - never a bare direct-IP call
# while the pool has exits.
URL = ("https://i.instagram.com/api/v1/bloks/async_action/"
       "com.bloks.www.caa.ar.search.async/")
BLOKS_VERSION = ("5e47baf35c5a270b44c8906c8b99063564b30ef69779f3dee0b828bee2e4ef5b")
APP_USER_AGENT = ("Instagram 320.0.0.34.109 Android (33/13; 420dpi; "
                  "1080x2340; samsung; SM-A546B; a54x; exynos1380; "
                  "en_US; 465123678)")

DEADLINE_SECS = 60.0
ATTEMPTS = 2
PAUSE_BETWEEN = 3.0
READ_TIMEOUT = 20
CONNECT_TIMEOUT = 6
PROXY_SPIN = 3            # extra proxies burned through when one fails at network level
ENGINE_VERBOSE = False    # True = print attempt lines (kept False for clean bot terminal)

BOT_DIR = os.path.dirname(os.path.abspath(__file__))
PROXY_FILE = os.path.join(BOT_DIR, "alive_proxies.txt")

PROXY_TARGET = 100        # keep this many verified exits saved
PROXY_REFILL_AT = 10      # runtime refill kicks in below this
REFILL_COOLDOWN = 120     # min seconds between refill hunts
_proxy_pool = None
_proxy_idx = 0
_pool_lock = threading.Lock()
_refill_task = None       # asyncio.Task running the current refill hunt
_last_refill_end = 0.0


def _ensure_deps():
    """requests + pysocks (for socks4/socks5 exits) - auto-installed once."""
    try:
        import requests          # noqa: F401
        import socks             # noqa: F401
    except ImportError:
        print("[setup] installing requests + pysocks ...", flush=True)
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--quiet", "requests", "pysocks"]
        )
        import requests          # noqa: F401
        import socks             # noqa: F401
    return __import__("requests")


requests = _ensure_deps()


def load_proxies():
    """alive_proxies.txt -> list of proxy URLs (cached, comments skipped)."""
    global _proxy_pool
    if _proxy_pool is not None:
        return _proxy_pool
    pool = []
    try:
        with open(PROXY_FILE, encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    pool.append(line)
    except OSError:
        pass
    _proxy_pool = pool
    return pool


def next_proxy():
    """Round-robin over the alive pool. None = no pool -> direct IP fallback."""
    global _proxy_idx
    pool = load_proxies()
    with _pool_lock:
        n = len(pool)
        if n == 0:
            return None
        proxy = pool[_proxy_idx % n]
        _proxy_idx += 1
        return proxy


def retire_proxy(proxy):
    """Drop a proven-dead exit from the in-memory pool (file stays untouched
    - only proxiech.py writes alive_proxies.txt)."""
    global _proxy_pool
    if not proxy:
        return
    with _pool_lock:
        if _proxy_pool and proxy in _proxy_pool:
            try:
                _proxy_pool.remove(proxy)
            except ValueError:
                pass


def reload_proxies():
    """Re-read alive_proxies.txt (after a refill hunt) and reset rotation."""
    global _proxy_pool, _proxy_idx
    with _pool_lock:
        _proxy_pool = None
        _proxy_idx = 0
    return load_proxies()


def build_request(email):
    device = "android-" + "".join(random.choices("abcdef0123456789", k=16))
    family = str(uuid.uuid4())
    android = "android-" + "".join(random.choices("abcdef0123456789", k=16))
    waterfall = str(uuid.uuid4())

    params = (
        "{\"client_input_params\":{\"aac\":\"{\\\"aac_init_timestamp\\\":"
        + str(int(time.time()))
        + ",\\\"aacjid\\\":\\\"" + str(uuid.uuid4())
        + "\\\",\\\"aaccs\\\":\\\"" + secrets.token_urlsafe(32)
        + "\\\"}\",\"flash_call_permissions_status\":{\"READ_PHONE_STATE\":"
          "\"PERMANENTLY_DENIED\",\"READ_CALL_LOG\":\"DENIED\","
          "\"ANSWER_PHONE_CALLS\":\"DENIED\"},\"was_headers_prefill_available\":0,"
          "\"network_bssid\":null,\"sfdid\":\"\",\"fetched_email_token_list\":{},"
          "\"search_query\":\""
        + email
        + "\",\"auth_secure_device_id\":\"\",\"ig_oauth_token\":[],"
          "\"cloud_trust_token\":null,\"was_headers_prefill_used\":0,"
          "\"sso_accounts_auth_data\":[],\"encrypted_msisdn\":\"\","
          "\"device_network_info\":null,\"text_input_id\":\"akyuf0:61\","
          "\"zero_balance_state\":null,\"android_build_type\":\"release\","
          "\"accounts_list\":[],\"is_oauth_without_permission\":0,"
          "\"ig_android_qe_device_id\":\""
        + device
        + "\",\"gms_incoming_call_retriever_eligibility\":\"client_not_supported\","
          "\"search_screen_type\":\"email_or_username\",\"is_whatsapp_installed\":1,"
          "\"lois_settings\":{\"lois_token\":\"\"},\"ig_vetted_device_nonce\":null,"
          "\"headers_infra_flow_id\":\"\",\"fetched_email_list\":[]},"
          "\"server_params\":{\"event_request_id\":\""
        + str(uuid.uuid4())
        + "\",\"is_from_logged_out\":0,\"layered_homepage_experiment_group\":null,"
          "\"device_id\":\""
        + android
        + "\",\"login_surface\":\"login_home\",\"waterfall_id\":\""
        + waterfall
        + "\",\"INTERNAL__latency_qpl_instance_id\":6.3987980400102E13,"
          "\"is_platform_login\":0,\"context_data\":\"\","
          "\"login_entry_point\":\"logged_out\","
          "\"INTERNAL__latency_qpl_marker_id\":36707139,\"family_device_id\":\""
        + family
        + "\",\"offline_experiment_group\":\"caa_iteration_v3_perf_ig_4\","
          "\"access_flow_version\":\"pre_mt_behavior\","
          "\"is_from_logged_in_switcher\":0,\"qe_device_id\":\""
        + device + "\"}}"
    )

    payload = {
        "params": params,
        "bk_client_context": ("{\"bloks_version\":\"" + BLOKS_VERSION
                              + "\",\"styles_id\":\"instagram\"}"),
        "bloks_versioning_id": BLOKS_VERSION,
    }

    headers = {
        "User-Agent": APP_USER_AGENT,
        "accept-language": "en-IN, en-US",
        "x-bloks-version-id": BLOKS_VERSION,
        "x-fb-friendly-name": ("IgApi: bloks/async_action/"
                               "com.bloks.www.caa.ar.search.async/"),
        "x-ig-android-id": android,
        "x-ig-app-id": "567067343352427",
        "x-ig-app-locale": "en_IN",
        "x-ig-client-endpoint": "com.bloks.www.caa.ar.search",
        "x-ig-device-id": device,
        "x-ig-family-device-id": family,
        "x-ig-timezone-offset": str(int(datetime.now().astimezone()
                                        .utcoffset().total_seconds())),
        "x-mid": base64.urlsafe_b64encode(secrets.token_bytes(18))
                    .decode().rstrip("="),
        "x-pigeon-rawclienttime": str(time.time()),
        "x-pigeon-session-id": f"UFS-{uuid.uuid4()}-0",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Accept": "*/*",
    }
    return payload, headers


def send_request(payload, headers, proxy=None):
    """POST through the given proxy (None = direct). Every request goes out
    via the alive_proxies.txt pool when it exists."""
    body = urllib.parse.urlencode(payload).encode("utf-8")
    s = requests.Session()
    s.trust_env = False              # ignore system/env proxy settings
    try:
        proxies = {"http": proxy, "https": proxy} if proxy else None
        resp = s.post(URL, data=body, headers=headers, proxies=proxies,
                      timeout=(CONNECT_TIMEOUT, READ_TIMEOUT))
        return resp.status_code, resp.text
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"
    finally:
        s.close()


def classify(email, status_code, body):
    if status_code == 429:
        return "ig_error", "http_429"
    if status_code != 200:
        return "ig_error", f"http_{status_code}"
    try:
        data = json.loads(body)
    except Exception:
        return "ig_error", "non_json"
    if data.get("status") != "ok":
        return "ig_error", f"status_{data.get('status')}"

    low = body.lower()
    echoed = email.lower() in low
    has_screen = "embedded_payloads" in body
    size = len(body)

    if has_screen and size > 100000:
        return "ig_connected", f"len={size}"
    if (not echoed) and (not has_screen) and size < 100000:
        return "ig_not_connected", f"len={size}"
    return "ig_error", f"unexpected len={size}"


def run_recovery(email):
    deadline = time.time() + DEADLINE_SECS
    last, detail = "ig_error", "-"
    for i in range(1, ATTEMPTS + 1):
        payload, headers = build_request(email)
        proxy = next_proxy()
        try:
            status_code, body = send_request(payload, headers, proxy)
            # network-level failure = this exit is dead -> retire it from the
            # pool and burn through a few more (until the deadline)
            spin = 0
            while status_code == 0 and proxy and time.time() < deadline:
                retire_proxy(proxy)
                if spin >= PROXY_SPIN:
                    break
                nxt = next_proxy()
                if nxt is None:
                    proxy = None          # pool drained - refill is on the way
                    break
                proxy = nxt
                status_code, body = send_request(payload, headers, proxy)
                spin += 1
            last, detail = classify(email, status_code, body)
        except Exception as e:
            last, detail = "ig_error", type(e).__name__
        if ENGINE_VERBOSE:
            via = proxy.rsplit("//", 1)[-1] if proxy else "direct"
            print(f"   attempt {i}/{ATTEMPTS}: {last} ({detail})  via {via}",
                  flush=True)
        if last != "ig_error":
            return last
        if time.time() > deadline:
            break
        if i < ATTEMPTS:
            time.sleep(PAUSE_BETWEEN)
    return last


def send_reset_link(email: str) -> bool:
    """Blocking engine entry point - True only when Instagram really sent
    the reset link (same semantics as anu.py's old exit code 0)."""
    try:
        return run_recovery(email) == "ig_connected"
    except Exception:
        return False


# ========== NON-BLOCKING PARALLEL RESET EXECUTION (in-process) ==========
# Old design: spawned `python anu.py <email>` as a subprocess per reset.
# The merged engine above now runs inside this worker pool instead - no
# second file to ship, event loop never blocks, 25 parallel like before.
ENGINE_POOL = ThreadPoolExecutor(max_workers=MAX_PARALLEL_RESES,
                                 thread_name_prefix="igreset")


# ---------- automatic refill: pool low -> proxiech.py runs in background ----
def _refill_active() -> bool:
    return _refill_task is not None and not _refill_task.done()


async def refill_proxies():
    """Hunt with proxiech.py until PROXY_TARGET alive exits are saved, then
    swap the fresh list into memory. Background task - never blocks the bot."""
    global _last_refill_end
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, os.path.join(BOT_DIR, "proxiech.py"),
            "--target", str(PROXY_TARGET),
            "--workers", "70",
            "--deadline", "900",
            cwd=BOT_DIR,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await proc.wait()
    except Exception:
        pass
    finally:
        reload_proxies()
        _last_refill_end = time.time()


async def ensure_proxies(min_want=PROXY_REFILL_AT) -> bool:
    """Start a background refill when the pool dropped below min_want.
    Single-flight + cooldown; returns immediately (never blocks)."""
    global _refill_task
    if _refill_active():
        return False
    if len(load_proxies()) >= min_want:
        return False
    if time.time() - _last_refill_end < REFILL_COOLDOWN:
        return False
    _refill_task = asyncio.create_task(refill_proxies())
    return True


async def run_reset_flow(email: str) -> bool:
    """Run the Instagram recovery engine off-loop. True = link sent."""
    await ensure_proxies() # pool low? refill starts in background now
    if not load_proxies() and _refill_active():
        # pool drained while the refill hunt is running - wait a little so
        # the request still leaves through a proxy (never a bare direct call)
        try:
            await asyncio.wait_for(asyncio.shield(_refill_task), timeout=45)
        except Exception:
            pass
        if not load_proxies() and _refill_active():
            return False              # still hunting - skip, points auto-refund
    async with CONCURRENCY_LIMIT:
        try:
            loop = asyncio.get_running_loop()
            return await asyncio.wait_for(
                loop.run_in_executor(ENGINE_POOL, send_reset_link, email),
                timeout=25.0,
            )
        except Exception:
            return False

# ========== SINGLE PROCESS ==========
async def process_single(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    email = update.message.text.strip()
    
    if not is_valid_email(email):
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_menu", style="danger")]])
        await update.message.reply_text("❌ <b>Invalid Email!</b> Try again or go back:", reply_markup=kb, parse_mode="HTML")
        return SINGLE_INPUT 
        
    u_data = check_and_give_daily(user_id, user_obj=user)
    if u_data.get('points', 0) < 1:
        await update.message.reply_text(
            f"❌ <b>Out of Points!</b>\n\nYou have 0 points. You receive free daily points.\n\n<b>Buy more from:</b> {ADMIN_USERNAME}",
            parse_mode="HTML"
        )
        return CHOOSING_MODE

    update_user_stats(user_id, -1)
    status_msg = await update.message.reply_text(f"⏳ <b>Processing reset link for:</b> {email}...", parse_mode="HTML")
    
    success = await run_reset_flow(email)
    
    if success:
        update_user_stats(user_id, 0, success=True)
        await safe_edit(status_msg, f"✅ <b>Process Completed for {email}!</b>\n<i>(1 Point deducted)</i>")
    else:
        update_user_stats(user_id, 1, success=False) 
        await safe_edit(status_msg, f"❌ <b>Failed to send link for:</b> {email}\n<i>(Point refunded)</i>")
        
    await show_modes(update, context, "<b>What would you like to do next?</b>")
    return CHOOSING_MODE

# ========== MULTI PROCESS (PARALLEL FAST BULK) ==========
async def process_multi(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_id = user.id
    text = update.message.text
    raw_emails = text.replace('\n', ' ').split()
    valid_emails = [e for e in raw_emails if is_valid_email(e)]
    req_points = len(valid_emails)
    
    if not valid_emails:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_menu", style="danger")]])
        await update.message.reply_text("⚠️ <b>No valid emails found.</b> Try again:", reply_markup=kb, parse_mode="HTML")
        return MULTI_INPUT

    if req_points > 20:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back to Menu", callback_data="back_to_menu", style="danger")]])
        await update.message.reply_text("⚠️ <b>Limit Exceeded! Max 20 emails allowed at once.</b>", reply_markup=kb, parse_mode="HTML")
        return MULTI_INPUT

    u_data = check_and_give_daily(user_id, user_obj=user)
    current_points = u_data.get('points', 0)
    if current_points < req_points:
        await update.message.reply_text(
            f"❌ <b>Insufficient Points!</b>\n\n<b>Required:</b> {req_points}\n<b>Available:</b> {current_points}\n\n<b>Buy from:</b> {ADMIN_USERNAME}",
            parse_mode="HTML"
        )
        return CHOOSING_MODE

    update_user_stats(user_id, -req_points)
    status_msg = await update.message.reply_text(f"⏳ <b>Processing {req_points} emails...</b>", parse_mode="HTML")
    
    success_count, failed_count = 0, 0

    for email in valid_emails:
        if await run_reset_flow(email):
            success_count += 1
            update_user_stats(user_id, 0, success=True)
        else:
            failed_count += 1
            update_user_stats(user_id, 0, success=False)
            
        if (success_count + failed_count) % 4 == 0:
            await safe_edit(status_msg, f"⏳ <b>Live Processing...</b>\n✅ Success: {success_count}\n❌ Failed: {failed_count}")
            
    if failed_count > 0:
        update_user_stats(user_id, failed_count) 
        
    final_data = check_and_give_daily(user_id, user_obj=user)
    await safe_edit(
        status_msg,
        f"🏁 <b>Bulk Completed!</b>\n\n✅ <b>Successful:</b> {success_count}\n❌ <b>Failed (Refunded):</b> {failed_count}\n💳 <b>Remaining Points:</b> {final_data.get('points', 0)}"
    )
    await show_modes(update, context, "<b>What would you like to do next?</b>")
    return CHOOSING_MODE

# ========== MAIN LOOP (100+ CONCURRENT USERS READY) ==========
async def on_post_init(app: Application):
    """Top the IG proxy pool up to PROXY_TARGET in the background."""
    try:
        await ensure_proxies(PROXY_TARGET)
    except Exception:
        pass


def main():
    load_db()
    t_request = HTTPXRequest(
        connection_pool_size=256,
        read_timeout=30.0,
        write_timeout=30.0,
        connect_timeout=30.0,
        pool_timeout=30.0,
        http_version="1.1"
    )
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(t_request)
        .concurrent_updates(True)
        .post_init(on_post_init)
        .build()
    )

    app.add_error_handler(global_error_handler)

    app.add_handler(CommandHandler("points", my_points))
    app.add_handler(CommandHandler("id", my_points))
    app.add_handler(CommandHandler("add", admin_add_command))
    app.add_handler(CommandHandler("cut", admin_cut_command))
    app.add_handler(CommandHandler("ban", admin_ban_command))
    app.add_handler(CommandHandler("broadcast", admin_broadcast_command))
    app.add_handler(CommandHandler("createcode", admin_create_code_command))

    conv_handler = ConversationHandler(
        entry_points=[
            CommandHandler("start", start),
            CommandHandler("admin", admin_panel),
            CallbackQueryHandler(button_handler)
        ],
        states={
            CHOOSING_MODE: [CallbackQueryHandler(button_handler)],
            SINGLE_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_single), CallbackQueryHandler(button_handler)],
            MULTI_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_multi), CallbackQueryHandler(button_handler)],
            REDEEM_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_redeem), CallbackQueryHandler(button_handler)],
            ADM_ADD_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_adm_add), CallbackQueryHandler(button_handler)],
            ADM_CUT_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_adm_cut), CallbackQueryHandler(button_handler)],
            ADM_BAN_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_adm_ban), CallbackQueryHandler(button_handler)],
            ADM_CODE_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_adm_code), CallbackQueryHandler(button_handler)],
            ADM_BC_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_adm_broadcast), CallbackQueryHandler(button_handler)],
            ADM_LOOKUP_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_adm_lookup), CallbackQueryHandler(button_handler)],
            ADM_SET_DAILY_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_adm_set_daily), CallbackQueryHandler(button_handler)],
            ADM_SET_REF_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_adm_set_ref), CallbackQueryHandler(button_handler)],
            ADM_DEL_CODE_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, process_adm_del_code), CallbackQueryHandler(button_handler)],
        },
        fallbacks=[
            CommandHandler("start", start),
            CommandHandler("admin", admin_panel),
            CallbackQueryHandler(button_handler)
        ],
        per_user=True,
        per_chat=True
    )

    app.add_handler(conv_handler)
    ig_pool = load_proxies()
    ig_route = (f"{len(ig_pool)} proxy exits (alive_proxies.txt)" if ig_pool
                else "DIRECT - proxiech se pehle alive_proxies.txt banao")
    print(f"🤖 Bot Running (High-Concurrency 100+ Users Mode, Zero Logs)!")
    print(f"   IG route: {ig_route}")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
