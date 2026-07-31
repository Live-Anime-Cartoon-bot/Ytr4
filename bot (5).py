import os
import json
import gzip
import re
import asyncio
import time
import secrets as _secrets
import requests
import base64
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from telegram.constants import ParseMode

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
BOT_OWNER_ID = os.environ.get("BOT_OWNER_ID")
API_ID = os.environ.get("API_ID")
API_HASH = os.environ.get("API_HASH")
DATA_FOLDER = "app/assets/data"
USER_DATA_FILE = "user_data.json"
ADMINS_FILE = "admins.json"
VERIFY_TOKENS_FILE = "verify_tokens.json"
PLAYLIST_URL = os.environ.get(
    "PLAYLIST_URL",
    "https://raw.githubusercontent.com/Sflex0719/m3u/refs/heads/main/Zio.m3u"
)
_tz = os.environ.get("TIMEZONE", "Asia/Kolkata")
IST = ZoneInfo(_tz)

REC_LIMIT_SECONDS = int(os.environ.get("REC_LIMIT_SECONDS", "600"))
VERIFICATION_EXPIRY_SECONDS = int(os.environ.get("VERIFICATION_EXPIRY_SECONDS", "2400"))
SHORTLINK_URL = os.environ.get("SHORTLINK_URL", "https://shrinkme.io")
SHORTLINK_API = os.environ.get("SHORTLINK_API", "")
WORKING_GROUP = os.environ.get("WORKING_GROUP", "")
GROUP_LINK = os.environ.get("GROUP_LINK", "")
BOTUSERNAME = os.environ.get("BOTUSERNAME", "")
MAX_PROCESSES = int(os.environ.get("MAX_PROCESSES", "5"))
PAID_BOT_CONTACT = os.environ.get("PAID_BOT_CONTACT", "@LS_Ower_bot")

# Active process slot counter
_active_processes = 0

_channel_cache = None          # list of channel dicts
_channel_cache_ts = 0          # unix timestamp of last fetch
_CHANNEL_CACHE_TTL = 1800      # refresh every 30 minutes (tokens expire)

def _load_json(path, default=None):
    if os.path.exists(path):
        with open(path, "r") as f:
            return json.load(f)
    return default if default is not None else {}

def _save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)

def get_user_data():
    return _load_json(USER_DATA_FILE, {})

def save_user_data(data):
    _save_json(USER_DATA_FILE, data)

def get_admins():
    return _load_json(ADMINS_FILE, {})

def save_admins(data):
    _save_json(ADMINS_FILE, data)

def get_verify_tokens():
    return _load_json(VERIFY_TOKENS_FILE, {})

def save_verify_tokens(data):
    _save_json(VERIFY_TOKENS_FILE, data)

def is_verified(user_id):
    tokens = get_verify_tokens()
    entry = tokens.get(str(user_id))
    if not entry:
        return False
    verified_at = entry.get("verified_at")
    if not verified_at:
        return False
    elapsed = (datetime.now(IST) - datetime.fromisoformat(verified_at)).total_seconds()
    return elapsed < VERIFICATION_EXPIRY_SECONDS

def generate_verify_token(user_id):
    token = _secrets.token_hex(16)
    tokens = get_verify_tokens()
    tokens[str(user_id)] = {
        "token": token,
        "created_at": datetime.now(IST).isoformat(),
        "verified_at": None,
    }
    save_verify_tokens(tokens)
    return token

def mark_verified(user_id, token):
    tokens = get_verify_tokens()
    entry = tokens.get(str(user_id))
    if not entry or entry.get("token") != token:
        return False
    # Token must be used within 15 minutes of generation
    created_at = datetime.fromisoformat(entry["created_at"])
    if (datetime.now(IST) - created_at).total_seconds() > 900:
        return False
    entry["verified_at"] = datetime.now(IST).isoformat()
    save_verify_tokens(tokens)
    return True

def shorten_url(long_url):
    if not SHORTLINK_API:
        return long_url
    try:
        api_base = SHORTLINK_URL.rstrip("/")
        resp = requests.get(
            f"{api_base}/api",
            params={"api": SHORTLINK_API, "url": long_url},
            timeout=10
        )
        data = resp.json()
        # ShrinkMe returns {"status":"success","shortenedUrl":"..."}
        if data.get("status") == "success":
            return data.get("shortenedUrl", long_url)
        # Fallback: some APIs return the URL directly as a string field
        return data.get("short_url") or data.get("url") or long_url
    except Exception:
        return long_url

def is_owner(user_id):
    if not BOT_OWNER_ID:
        return False
    return str(user_id) == str(BOT_OWNER_ID)

def is_admin(user_id):
    admins = get_admins()
    return str(user_id) in admins

def get_user_role(user_id):
    if is_owner(user_id):
        return "owner"
    if is_admin(user_id):
        return "admin"
    return "user"

# ── Credential helpers ─────────────────────────

def load_credentials():
    creds_path = os.path.join(DATA_FOLDER, "creds.jtv")
    key_path = os.path.join(DATA_FOLDER, "credskey.jtv")
    if not os.path.exists(creds_path) or not os.path.exists(key_path):
        return None
    with open(key_path, "r") as f:
        key = int(f.read().strip())
    with open(creds_path, "r") as f:
        enc = f.read().strip()
    decoded = base64.b64decode(enc).decode("latin-1")
    decrypted = "".join(chr(ord(c) - key) for c in decoded)
    return json.loads(decrypted)

def save_credentials(jio_data, mobile):
    u_name = encrypt_data(mobile, "TS-JIOTV")
    os.makedirs(DATA_FOLDER, exist_ok=True)
    with open(os.path.join(DATA_FOLDER, "creds.jtv"), "w") as f:
        f.write(encrypt_data(json.dumps(jio_data), u_name))
    with open(os.path.join(DATA_FOLDER, "credskey.jtv"), "w") as f:
        f.write(u_name)

def encrypt_data(data, key):
    key = int(key)
    enc = "".join(chr(ord(c) + key) for c in data)
    return base64.b64encode(enc.encode("latin-1")).decode()

# ── JioTV API helpers ──────────────────────────

def parse_m3u(text):
    """Parse an M3U playlist into a list of channel dicts compatible with the rest of the bot."""
    channels = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if line.startswith("#EXTINF"):
            # Extract tvg attributes from the EXTINF line
            channel_id = re.search(r'tvg-id="([^"]*)"', line)
            channel_name = re.search(r'tvg-name="([^"]*)"', line)
            logo = re.search(r'tvg-logo="([^"]*)"', line)
            group = re.search(r'group-title="([^"]*)"', line)

            channel_id = channel_id.group(1) if channel_id else ""
            channel_name = channel_name.group(1) if channel_name else ""
            logo = logo.group(1) if logo else ""
            group = group.group(1) if group else "Other"

            # Collect KODIPROP / EXTVLCOPT / EXTHTTP lines and stream URL
            license_key = ""
            user_agent = ""
            cookie = ""
            stream_url = ""
            i += 1
            while i < len(lines):
                nxt = lines[i].strip()
                if nxt.startswith("#KODIPROP:inputstream.adaptive.license_key="):
                    license_key = nxt.split("=", 1)[1]
                elif nxt.startswith("#EXTVLCOPT:http-user-agent="):
                    user_agent = nxt.split("=", 1)[1]
                elif nxt.startswith("#EXTHTTP:"):
                    try:
                        http_data = json.loads(nxt[len("#EXTHTTP:"):])
                        cookie = http_data.get("cookie", "")
                    except Exception:
                        pass
                elif nxt and not nxt.startswith("#"):
                    stream_url = nxt
                    i += 1
                    break
                i += 1

            if channel_name and stream_url:
                channels.append({
                    "channel_id": channel_id,
                    "channel_name": channel_name,
                    "channelCategoryId": group,
                    "isCatchupAvailable": "False",
                    "logoUrl": logo,
                    "stream_url": stream_url,
                    "license_key": license_key,
                    "user_agent": user_agent,
                    "cookie": cookie,
                })
        else:
            i += 1
    return channels


def get_channels():
    global _channel_cache, _channel_cache_ts
    now = time.time()
    if _channel_cache and (now - _channel_cache_ts) < _CHANNEL_CACHE_TTL:
        return _channel_cache
    resp = requests.get(PLAYLIST_URL, timeout=15)
    resp.raise_for_status()
    _channel_cache = parse_m3u(resp.text)
    _channel_cache_ts = now
    return _channel_cache

def find_channel(name_query):
    channels = get_channels()
    q = name_query.lower().strip()
    for c in channels:
        if c["channel_name"].lower() == q:
            return c
    for c in channels:
        if q in c["channel_name"].lower():
            return c
    return None

def get_epg(channel_id, offset=0):
    url = f"https://jiotvapi.cdn.jio.com/apis/v1.3/getepg/get?offset={offset}&channel_id={channel_id}&langId=6"
    headers = {"user-agent": "okhttp/4.12.13", "Accept-Encoding": "gzip"}
    resp = requests.get(url, headers=headers, timeout=10)
    if resp.status_code == 200:
        try:
            data = gzip.decompress(resp.content)
            return json.loads(data)
        except Exception:
            try:
                return resp.json()
            except Exception:
                return None
    return None

def parse_time(time_str):
    time_str = time_str.strip().upper()
    for fmt in ["%I:%M%p", "%I:%M %p", "%H:%M"]:
        try:
            t = datetime.strptime(time_str, fmt)
            return t.hour, t.minute
        except ValueError:
            continue
    return None, None

def find_program_in_epg(channel_id, start_h, start_m, end_h, end_m):
    for offset in [0, -1, 1]:
        epg = get_epg(channel_id, offset)
        if not epg:
            continue
        for program in epg.get("epg", []):
            try:
                start_ts = int(program.get("startEpoch", 0))
                end_ts = int(program.get("endEpoch", 0))
                p_start = datetime.fromtimestamp(start_ts, tz=IST)
                p_end = datetime.fromtimestamp(end_ts, tz=IST)
                if p_start.hour == start_h and p_start.minute == start_m:
                    return program, p_start, p_end
                if p_end.hour == end_h and p_end.minute == end_m:
                    return program, p_start, p_end
            except Exception:
                continue
    return None, None, None

def jio_headers_from_creds(creds):
    return {
        "appname": "RJIL_JioTV",
        "os": "android",
        "devicetype": "phone",
        "content-type": "application/json",
        "user-agent": "okhttp/3.14.9"
    }

def send_jio_otp_api(mobile):
    url = "https://jiotvapi.media.jio.com/userservice/apis/v1/loginotp/send"
    headers = {
        "appname": "RJIL_JioTV",
        "os": "android",
        "devicetype": "phone",
        "content-type": "application/json",
        "user-agent": "okhttp/3.14.9"
    }
    payload = {"number": base64.b64encode(f"+91{mobile}".encode()).decode()}
    resp = requests.post(url, json=payload, headers=headers, timeout=10)
    if resp.status_code == 204:
        return {"status": "success", "message": "OTP sent successfully"}
    try:
        data = resp.json()
        return {"status": "error", "message": data.get("message", f"Error code {resp.status_code}")}
    except Exception:
        return {"status": "error", "message": f"Unknown error: {resp.status_code}"}

def verify_jio_otp_api(mobile, otp):
    url = "https://jiotvapi.media.jio.com/userservice/apis/v1/loginotp/verify"
    headers = {
        "appname": "RJIL_JioTV",
        "os": "android",
        "devicetype": "phone",
        "content-type": "application/json",
        "user-agent": "okhttp/3.14.9"
    }
    payload = {
        "number": base64.b64encode(f"+91{mobile}".encode()).decode(),
        "otp": otp,
        "deviceInfo": {
            "consumptionDeviceName": "RMX1945",
            "info": {
                "type": "android",
                "platform": {"name": "RMX1945"},
                "androidId": "tsjiotvbot123456"
            }
        }
    }
    resp = requests.post(url, json=payload, headers=headers, timeout=10)
    try:
        data = resp.json()
    except Exception:
        return {"status": "error", "message": f"Parse error: {resp.status_code}"}
    if data.get("ssoToken"):
        save_credentials(data, mobile)
        return {"status": "success", "message": "Login successful!"}
    msg = data.get("message", "")
    if not msg and "errors" in data and data["errors"]:
        msg = data["errors"][-1].get("message", "")
    return {"status": "error", "message": msg or f"Verify failed: {resp.status_code}"}

# ── Stream URL builders ────────────────────────

def get_stream_url(channel_id, creds=None):
    """Return (stream_url, cookie, user_agent) for a channel from the M3U playlist."""
    channels = get_channels()
    for ch in channels:
        if str(ch["channel_id"]) == str(channel_id):
            return ch.get("stream_url"), ch.get("cookie", ""), ch.get("user_agent", "")
    return None, "", ""

def get_catchup_url(channel_id, srno, begin, end, creds):
    access_token = creds.get("authToken", "")
    crm = creds.get("sessionAttributes", {}).get("user", {}).get("subscriberId", "")
    unique_id = creds.get("sessionAttributes", {}).get("user", {}).get("unique", "")
    device_id = creds.get("deviceId", "")
    post_data = f"stream_type=Catchup&channel_id={channel_id}&programId={srno}&showtime=000000&srno={srno}&begin={begin}&end={end}"
    headers = {
        "Host": "jiotvapi.media.jio.com",
        "Content-Type": "application/x-www-form-urlencoded",
        "appkey": "NzNiMDhlYzQyNjJm",
        "channel_id": str(channel_id),
        "userid": crm,
        "crmid": crm,
        "deviceId": device_id,
        "devicetype": "phone",
        "isott": "true",
        "languageId": "6",
        "lbcookie": "1",
        "os": "android",
        "dm": "Xiaomi 22101316UP",
        "osversion": "14",
        "srno": str(srno),
        "accesstoken": access_token,
        "subscriberid": crm,
        "uniqueId": unique_id,
        "usergroup": "tvYR7NSNn7rymo3F",
        "User-Agent": "okhttp/4.12.13",
        "versionCode": "452",
    }
    resp = requests.post(
        "https://jiotvapi.media.jio.com/playback/apis/v1/geturl?langId=6",
        data=post_data, headers=headers, timeout=10
    )
    data = resp.json()
    if data.get("code") == 200:
        return data.get("result")
    return None

# ── Role decorators ────────────────────────────

def owner_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if not is_owner(uid):
            await update.message.reply_text("❌ Sirf Owner yeh command use kar sakta hai.")
            return
        return await func(update, context)
    return wrapper

def owner_admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if not is_owner(uid) and not is_admin(uid):
            await update.message.reply_text("❌ Sirf Owner ya Admin yeh command use kar sakte hain.")
            return
        return await func(update, context)
    return wrapper

def require_login(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not load_credentials():
            await update.message.reply_text(
                "❌ JioTV login nahi hai.\nPehle `/login <mobile>` se OTP verify karo.",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        return await func(update, context)
    return wrapper

def require_verification(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if is_owner(uid) or is_admin(uid):
            return await func(update, context)
        if not is_verified(uid):
            await update.message.reply_text(
                "🔐 *Access ke liye verification zaroori hai!*\n\n"
                "Command: `/verify`",
                parse_mode=ParseMode.MARKDOWN
            )
            return
        return await func(update, context)
    return wrapper

async def _auto_delete(bot, chat_id, message_id, delay):
    await asyncio.sleep(delay)
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

async def check_process_slot(update: Update) -> bool:
    """Return True if a processing slot is free, False (after sending busy msg) if all slots taken."""
    global _active_processes
    uid = update.effective_user.id
    if is_owner(uid) or is_admin(uid):
        return True
    if _active_processes >= MAX_PROCESSES:
        await update.message.reply_text(
            f"⚠️ *Server Busy ({_active_processes}/{MAX_PROCESSES} Processes Running)*\n\n"
            "All processing slots are currently in use.\n\n"
            "⏳ Please wait a few minutes and try again.\n\n"
            "💎 Want instant access with higher limits and no waiting?\n"
            f"Upgrade to the Paid Bot.\n\n"
            f"👉 Contact: {PAID_BOT_CONTACT}",
            parse_mode=ParseMode.MARKDOWN
        )
        return False
    return True

# ── Bot commands ───────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # Handle deep link verification: /start verify_<user_id>_<token>
    if context.args and context.args[0].startswith("verify_"):
        parts = context.args[0].split("_", 2)
        if len(parts) == 3:
            _, uid_str, token = parts
            if uid_str == str(user.id):
                if mark_verified(user.id, token):
                    expiry_mins = VERIFICATION_EXPIRY_SECONDS // 60
                    await update.message.reply_text(
                        f"✅ *Verification Successful!*\n\n"
                        f"Access granted for *{expiry_mins} minutes*.\n"
                        f"Ab `/rec` use kar sakte ho.",
                        parse_mode=ParseMode.MARKDOWN
                    )
                else:
                    await update.message.reply_text(
                        "❌ *Verification failed.*\n"
                        "Link expire ho gaya ya invalid hai. Dobara `/verify` karo.",
                        parse_mode=ParseMode.MARKDOWN
                    )
            else:
                await update.message.reply_text("❌ Yeh verify link tumhare liye nahi hai.")
            return

    role = get_user_role(user.id)
    role_icon = {"owner": "👑", "admin": "👨\u200d✈\ufe0f", "user": "👤"}[role]

    text = (
        f"{role_icon} *JioTV+ ReBorn Bot*\n"
        f"Role: `{role.upper()}` | User: `{user.first_name}`\n\n"
        "*Commands:*\n"
        "🔑 `/login <mobile>` — OTP bhejo\n"
        "🔐 `/otp <code>` — OTP verify karo\n"
        "🛡 `/verify` — Access unlock karo (40 min)\n"
        "📼 `/rec <channel> MM:SS` ya `-t HH:MMAM - HH:MMPM`\n"
        "📋 `/channels` — Channels list\n"
        "🔍 `/search <name>` — Channel search\n"
        "ℹ\ufe0f `/myinfo` — Apna info dekho\n"
    )
    if role in ("owner", "admin"):
        text += "📢 `/broadcast <msg>` — Sabko message bhejo\n"
    if role == "owner":
        text += (
            "\n*Owner Commands:*\n"
            "🔢 `/addadmin <user_id>` — Admin add\n"
            "🗑 `/removeadmin <user_id>` — Admin remove\n"
            "👥 `/adminlist` — Admin list\n"
            "🌐 `/proxy` — Proxy URL (hidden)\n"
            "💾 `/setowner <user_id>` — Owner set\n"
        )
    text += (
        "\n*Example:*\n"
        "`/rec Pogo 01:00` ya `/rec Pogo -t 12:00PM - 01:00PM`"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def myinfo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    role = get_user_role(user.id)
    creds = load_credentials()

    jio_mobile = "❌ Not logged in"
    expiry = "N/A"
    if creds:
        try:
            mobile = creds.get("sessionAttributes", {}).get("user", {}).get("mobile", "")
            name = creds.get("sessionAttributes", {}).get("user", {}).get("commonName", "")
            jio_mobile = f"{name} ({mobile})"
            jwt = creds.get("authToken", "")
            if jwt:
                parts = jwt.split(".")
                if len(parts) > 1:
                    payload = json.loads(base64.b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
                    exp = payload.get("exp", 0)
                    exp_dt = datetime.fromtimestamp(exp, tz=IST)
                    expiry = exp_dt.strftime("%d-%b-%Y %I:%M %p")
        except Exception:
            pass

    text = (
        f"👤 *User Info*\n"
        f"Name: `{user.first_name}`\n"
        f"ID: `{user.id}`\n"
        f"Role: `{role.upper()}`\n"
        f"Username: @{user.username or 'N/A'}\n\n"
        f"📱 *JioTV Status*\n"
        f"Mobile: `{jio_mobile}`\n"
        f"Token Expiry: `{expiry}`"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def login_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Usage: `/login <10-digit mobile>`", parse_mode=ParseMode.MARKDOWN)
        return

    mobile = context.args[0].strip()
    if not re.match(r"^\d{10}$", mobile):
        await update.message.reply_text("❌ 10-digit mobile number daliye. Example: `/login 9876543210`", parse_mode=ParseMode.MARKDOWN)
        return

    msg = await update.message.reply_text(f"🔑 *{mobile}* pe OTP bhej rahe hain...", parse_mode=ParseMode.MARKDOWN)

    result = send_jio_otp_api(mobile)

    if result["status"] == "success":
        user_data = get_user_data()
        user_data[str(update.effective_user.id)] = {
            "mobile": mobile,
            "pending": True,
            "login_time": datetime.now(IST).isoformat()
        }
        save_user_data(user_data)
        await msg.edit_text(
            f"✅ OTP *{mobile}* pe bhej diya!\n"
            f"Ab `/otp <6-digit code>` se verify karo.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await msg.edit_text(f"❌ OTP fail: {result['message']}")


async def otp_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Usage: `/otp <6-digit code>`", parse_mode=ParseMode.MARKDOWN)
        return

    otp = context.args[0].strip()
    if not re.match(r"^\d{6}$", otp):
        await update.message.reply_text("❌ 6-digit OTP daliye. Example: `/otp 123456`", parse_mode=ParseMode.MARKDOWN)
        return

    user_data = get_user_data()
    user_entry = user_data.get(str(update.effective_user.id))
    if not user_entry or not user_entry.get("pending"):
        await update.message.reply_text("❌ Pehle `/login <mobile>` se OTP request karo.", parse_mode=ParseMode.MARKDOWN)
        return

    mobile = user_entry["mobile"]
    msg = await update.message.reply_text("🔐 OTP verify ho raha hai...")

    result = verify_jio_otp_api(mobile, otp)

    if result["status"] == "success":
        user_entry["pending"] = False
        user_entry["verified"] = True
        save_user_data(user_data)
        await msg.edit_text("✅ *JioTV Login Successful!*\nAb `/live` ya `/rec` commands use kar sakte ho.", parse_mode=ParseMode.MARKDOWN)
    else:
        await msg.edit_text(f"❌ Verify fail: {result['message']}\nDobara try karo: `/otp <code>`", parse_mode=ParseMode.MARKDOWN)


async def verify_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # Owner/admin don't need verification
    if is_owner(user.id) or is_admin(user.id):
        await update.message.reply_text("✅ Owner/Admin ko verification ki zaroorat nahi hai.")
        return

    # Already verified?
    if is_verified(user.id):
        tokens = get_verify_tokens()
        entry = tokens.get(str(user.id), {})
        verified_at = entry.get("verified_at")
        if verified_at:
            elapsed = (datetime.now(IST) - datetime.fromisoformat(verified_at)).total_seconds()
            remaining = int((VERIFICATION_EXPIRY_SECONDS - elapsed) / 60)
            await update.message.reply_text(
                f"✅ *Tum already verified ho!*\n"
                f"⏳ Access bacha hai: *{remaining} minutes*",
                parse_mode=ParseMode.MARKDOWN
            )
            return

    token = generate_verify_token(user.id)
    bot_username = BOTUSERNAME or context.bot.username
    deep_link = f"https://t.me/{bot_username}?start=verify_{user.id}_{token}"
    short_link = shorten_url(deep_link)

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Verify", url=short_link)],
        [InlineKeyboardButton("❓ How to Verify", callback_data="howto_verify")],
    ])
    msg = await update.message.reply_text(
        "🔐 *Verification Required*\n\n"
        "Click the Verify button below to unlock access for 40 minutes.\n\n"
        "⚠️ This verification message will be automatically deleted after 10 minutes.",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN
    )
    asyncio.create_task(_auto_delete(context.bot, update.effective_chat.id, msg.message_id, 600))


async def howto_verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.message.reply_text(
        "❓ *Verification Kaise Kare?*\n\n"
        "1️⃣ `/verify` command bhejo\n"
        "2️⃣ *✅ Verify* button dabao\n"
        "3️⃣ Jo page khule uspe ad skip karo ya task complete karo\n"
        "4️⃣ Last mein bot ka link aayega — usse open karo\n"
        "5️⃣ Bot bolega *Verification Successful!*\n\n"
        f"✅ Iske baad *{VERIFICATION_EXPIRY_SECONDS // 60} minute* tak access milega.\n\n"
        f"📌 Group: {GROUP_LINK}" if GROUP_LINK else
        "❓ *Verification Kaise Kare?*\n\n"
        "1️⃣ `/verify` command bhejo\n"
        "2️⃣ *✅ Verify* button dabao\n"
        "3️⃣ Jo page khule uspe ad skip karo ya task complete karo\n"
        "4️⃣ Last mein bot ka link aayega — usse open karo\n"
        "5️⃣ Bot bolega *Verification Successful!*\n\n"
        f"✅ Iske baad *{VERIFICATION_EXPIRY_SECONDS // 60} minute* tak access milega.",
        parse_mode=ParseMode.MARKDOWN
    )


def _fetch_cenc_key(stream_url: str, license_url: str, cookie: str, user_agent: str) -> str:
    """Fetch the ClearKey decryption key for a CENC-encrypted DASH stream.
    Returns key_hex string, or empty string on failure."""
    try:
        import base64 as _b64
        padding = lambda s: s + "=" * (-len(s) % 4)
        hdrs = {}
        if cookie:
            hdrs["Cookie"] = cookie
        if user_agent:
            hdrs["User-Agent"] = user_agent
        mpd = requests.get(stream_url, headers=hdrs, timeout=10).text
        kid_match = re.search(r'default_KID="([^"]+)"', mpd)
        if not kid_match:
            return ""
        kid_uuid = kid_match.group(1).replace("-", "")
        kid_bytes = bytes.fromhex(kid_uuid)
        kid_b64 = _b64.urlsafe_b64encode(kid_bytes).rstrip(b"=").decode()
        lic = requests.post(
            license_url,
            data=json.dumps({"kids": [kid_b64], "type": "temporary"}),
            headers={"Content-Type": "application/json"},
            timeout=5,
        ).json()
        key_b64 = lic["keys"][0]["k"]
        return _b64.urlsafe_b64decode(padding(key_b64)).hex()
    except Exception:
        return ""


async def record_stream(stream_url: str, duration_seconds: int, output_path: str,
                        cookie: str = "", user_agent: str = "",
                        license_url: str = "") -> tuple[bool, str]:
    """Run FFmpeg to record a stream segment. Returns (success, error_log)."""
    cmd = ["ffmpeg", "-y"]
    if cookie or user_agent:
        header_str = ""
        if cookie:
            header_str += f"Cookie: {cookie}\r\n"
        if user_agent:
            header_str += f"User-Agent: {user_agent}\r\n"
        cmd += ["-headers", header_str]
    # Fetch ClearKey decryption key if a license URL is provided
    if license_url:
        key_hex = await asyncio.get_event_loop().run_in_executor(
            None, _fetch_cenc_key, stream_url, license_url, cookie, user_agent
        )
        if key_hex:
            cmd += ["-cenc_decryption_key", key_hex]
    cmd += [
        "-i", stream_url,
        "-t", str(duration_seconds),
        "-c", "copy",
        "-movflags", "+faststart",
        output_path
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE
        )
        _, stderr = await proc.communicate()
        if proc.returncode == 0:
            return True, ""
        return False, stderr.decode(errors="replace")[-600:]
    except Exception as e:
        return False, str(e)


@require_verification
async def rec_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    full_text = " ".join(context.args)

    # Format 1: /rec <channel> HH:MM:SS  or  MM:SS  (duration-based)
    duration_match = re.match(
        r"^(.+?)\s+(\d{1,2}:\d{2}:\d{2}|\d{1,2}:\d{2})$",
        full_text.strip()
    )
    # Format 2: /rec <channel> -t HH:MMAM - HH:MMPM  (time-range-based)
    timerange_match = re.match(
        r"^(.+?)\s+-t\s+(\d{1,2}:\d{2}\s*[APap][Mm])\s*-\s*(\d{1,2}:\d{2}\s*[APap][Mm])$",
        full_text.strip()
    )

    if duration_match:
        channel_name = duration_match.group(1).strip()
        dur_str_raw = duration_match.group(2).strip()
        parts = dur_str_raw.split(":")
        if len(parts) == 3:
            duration_seconds = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
        else:
            duration_seconds = int(parts[0]) * 60 + int(parts[1])
        time_range_label = dur_str_raw
    elif timerange_match:
        channel_name = timerange_match.group(1).strip()
        start_time_str = timerange_match.group(2).strip()
        end_time_str = timerange_match.group(3).strip()
        start_h, start_m = parse_time(start_time_str)
        end_h, end_m = parse_time(end_time_str)
        if start_h is None or end_h is None:
            await update.message.reply_text("❌ Time format sahi nahi. Example: `12:00PM`", parse_mode=ParseMode.MARKDOWN)
            return
        start_total = start_h * 60 + start_m
        end_total = end_h * 60 + end_m
        if end_total < start_total:
            end_total += 24 * 60
        duration_seconds = (end_total - start_total) * 60
        time_range_label = f"{start_time_str} - {end_time_str}"
    else:
        await update.message.reply_text(
            "❌ *Format sahi nahi hai.*\n\n"
            "*Option 1* — Duration:\n`/rec <channel> MM:SS`\n`/rec <channel> HH:MM:SS`\n\n"
            "Example: `/rec Pogo 00:30` _(30 sec)_\n"
            "Example: `/rec Pogo 01:30:00` _(1.5 hrs)_\n\n"
            "*Option 2* — Time range:\n`/rec <channel> -t HH:MMAM - HH:MMPM`\n\n"
            "Example: `/rec Pogo -t 12:00PM - 01:00PM`",
            parse_mode=ParseMode.MARKDOWN
        )
        return

    limit_mins = REC_LIMIT_SECONDS // 60
    if duration_seconds > REC_LIMIT_SECONDS:
        await update.message.reply_text(
            f"❌ *Recording limit exceeded!*\n\n"
            f"Maximum: *{limit_mins} minutes*\n"
            f"Requested: *{duration_seconds // 60} min {duration_seconds % 60} sec*\n\n"
            f"Chota duration use karo.",
            parse_mode=ParseMode.MARKDOWN
        )
        return
    if duration_seconds <= 0:
        await update.message.reply_text("❌ Duration zero ya negative nahi ho sakta.", parse_mode=ParseMode.MARKDOWN)
        return

    if not await check_process_slot(update):
        return

    global _active_processes
    _active_processes += 1
    msg = await update.message.reply_text(
        f"🔍 *{channel_name}* stream dhundh raha hoon...",
        parse_mode=ParseMode.MARKDOWN
    )
    try:
        channel = find_channel(channel_name)
        if not channel:
            await msg.edit_text(f"❌ Channel *{channel_name}* nahi mila.\n`/search {channel_name}` try karo.", parse_mode=ParseMode.MARKDOWN)
            return

        stream_url = channel.get("stream_url", "")
        cookie = channel.get("cookie", "")
        user_agent = channel.get("user_agent", "")

        if not stream_url:
            await msg.edit_text(f"❌ *{channel['channel_name']}* ka stream URL nahi mila.", parse_mode=ParseMode.MARKDOWN)
            return

        # ── FFmpeg recording ──────────────────────────
        mins = duration_seconds // 60
        secs = duration_seconds % 60
        dur_str = f"{mins}m {secs}s" if secs else f"{mins}m"
        await msg.edit_text(
            f"⏺ *Recording...*\n"
            f"📺 {channel['channel_name']}\n"
            f"🕐 {time_range_label}\n"
            f"⏱ Duration: {dur_str}\n\n"
            f"_Please wait, FFmpeg chal raha hai..._",
            parse_mode=ParseMode.MARKDOWN
        )

        out_file = f"/tmp/jiotv_{update.effective_user.id}_{int(time.time())}.mp4"
        license_url = channel.get("license_key", "")
        ok, err_log = await record_stream(stream_url, duration_seconds, out_file,
                                          cookie=cookie, user_agent=user_agent,
                                          license_url=license_url)

        if not ok:
            short_err = err_log[-300:] if len(err_log) > 300 else err_log
            await msg.edit_text(
                f"❌ *Recording failed!*\n\n```{short_err}```",
                parse_mode=ParseMode.MARKDOWN
            )
            return

        file_size = os.path.getsize(out_file)
        size_mb = file_size / (1024 * 1024)

        caption = (
            f"📼 *{channel['channel_name']}*\n"
            f"🕐 {time_range_label}\n"
            f"⏱ {dur_str} | 📦 {size_mb:.1f} MB"
        )

        await msg.edit_text(
            f"📤 *Uploading...* ({size_mb:.1f} MB)\n📺 {channel['channel_name']}",
            parse_mode=ParseMode.MARKDOWN
        )

        try:
            with open(out_file, "rb") as f:
                await update.message.reply_video(
                    video=f,
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN,
                    supports_streaming=True,
                    read_timeout=300,
                    write_timeout=300,
                )
        except Exception:
            # Fallback: send as document if video upload fails
            with open(out_file, "rb") as f:
                await update.message.reply_document(
                    document=f,
                    caption=caption,
                    parse_mode=ParseMode.MARKDOWN,
                    read_timeout=300,
                    write_timeout=300,
                )

        await msg.delete()

        # Cleanup temp file
        if os.path.exists(out_file):
            os.remove(out_file)

    finally:
        _active_processes -= 1


async def channels_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("📋 Channels list load ho rahi hai...")
    try:
        channels = get_channels()
    except Exception:
        await msg.edit_text("❌ Channels load nahi ho sake. Baad mein try karo.")
        return

    categories = {}
    for ch in channels:
        cat = ch.get("channelCategoryId", "Other")
        categories.setdefault(cat, []).append(ch["channel_name"])

    keyboard = [[InlineKeyboardButton(cat, callback_data=f"cat_{cat}")] for cat in sorted(categories.keys())]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await msg.edit_text(
        f"📺 *JioTV Channels* ({len(channels)} total)\n\nCategory choose karo:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )


async def category_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    cat = query.data.replace("cat_", "")
    channels = get_channels()
    cat_channels = [c for c in channels if c.get("channelCategoryId") == cat]

    lines = [f"📺 *{cat}* ({len(cat_channels)} channels)\n"]
    for ch in cat_channels:
        catchup = " 📼" if ch.get("isCatchupAvailable") == "True" else ""
        lines.append(f"• {ch['channel_name']}{catchup}")

    text = "\n".join(lines)
    if len(text) > 4000:
        text = text[:4000] + "\n..."

    keyboard = [[InlineKeyboardButton("« Back", callback_data="back_categories")]]
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=ParseMode.MARKDOWN)


async def back_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    channels = get_channels()
    categories = {}
    for ch in channels:
        cat = ch.get("channelCategoryId", "Other")
        categories.setdefault(cat, []).append(ch["channel_name"])

    keyboard = [[InlineKeyboardButton(cat, callback_data=f"cat_{cat}")] for cat in sorted(categories.keys())]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await query.edit_message_text(
        f"📺 *JioTV Channels* ({len(channels)} total)\n\nCategory choose karo:",
        reply_markup=reply_markup,
        parse_mode=ParseMode.MARKDOWN
    )


async def search_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Usage: `/search <channel name>`", parse_mode=ParseMode.MARKDOWN)
        return

    query = " ".join(context.args).lower()
    channels = get_channels()
    results = [c for c in channels if query in c["channel_name"].lower()]

    if not results:
        await update.message.reply_text(f"❌ `{query}` se koi channel nahi mila.", parse_mode=ParseMode.MARKDOWN)
        return

    lines = [f"🔍 *Search: {query}* ({len(results)} results)\n"]
    for ch in results[:20]:
        catchup = " 📼" if ch.get("isCatchupAvailable") == "True" else ""
        lines.append(f"• `{ch['channel_name']}`{catchup}")

    if len(results) > 20:
        lines.append(f"\n...aur {len(results) - 20} aur channels hain.")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


# ── Owner-only commands ────────────────────────

@owner_only
async def setowner_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Usage: `/setowner <user_id>`", parse_mode=ParseMode.MARKDOWN)
        return
    new_owner = context.args[0].strip()
    os.environ["BOT_OWNER_ID"] = new_owner
    await update.message.reply_text(f"✅ Owner set to `{new_owner}`", parse_mode=ParseMode.MARKDOWN)


@owner_only
async def addadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Usage: `/addadmin <user_id>`", parse_mode=ParseMode.MARKDOWN)
        return
    uid = context.args[0].strip()
    admins = get_admins()
    admins[uid] = {"added_by": update.effective_user.id, "time": datetime.now(IST).isoformat()}
    save_admins(admins)
    await update.message.reply_text(f"✅ Admin added: `{uid}`", parse_mode=ParseMode.MARKDOWN)


@owner_only
async def removeadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Usage: `/removeadmin <user_id>`", parse_mode=ParseMode.MARKDOWN)
        return
    uid = context.args[0].strip()
    admins = get_admins()
    if uid in admins:
        del admins[uid]
        save_admins(admins)
        await update.message.reply_text(f"✅ Admin removed: `{uid}`", parse_mode=ParseMode.MARKDOWN)
    else:
        await update.message.reply_text(f"❌ `{uid}` admin list mein nahi hai.", parse_mode=ParseMode.MARKDOWN)


@owner_only
async def adminlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    admins = get_admins()
    if not admins:
        await update.message.reply_text("📌 Koi admin nahi hai.")
        return
    lines = ["👥 *Admin List*\n"]
    for uid, info in admins.items():
        lines.append(f"• `{uid}` (Added: {info.get('time', 'N/A')[:10]})")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


@owner_only
async def proxy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    proxy = os.environ.get("JIOTV_PROXY_URL", "only_owner")
    await update.message.reply_text(f"🌐 Proxy URL: `{proxy}`\n\n(Sirf owner ko visible)", parse_mode=ParseMode.MARKDOWN)


# ── Owner + Admin commands ─────────────────────

@owner_admin_only
async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("❌ Usage: `/broadcast <message>`", parse_mode=ParseMode.MARKDOWN)
        return
    message = " ".join(context.args)
    user_data = get_user_data()
    sent = 0
    failed = 0
    for uid in user_data:
        try:
            await context.bot.send_message(chat_id=uid, text=f"📢 *Broadcast*\n\n{message}", parse_mode=ParseMode.MARKDOWN)
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(f"✅ Broadcast sent: {sent} users\n❌ Failed: {failed} users")


# ── Main ───────────────────────────────────────

def main():
    if not BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN set nahi hai!")
        return

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(CommandHandler("login", login_cmd))
    app.add_handler(CommandHandler("otp", otp_cmd))
    app.add_handler(CommandHandler("verify", verify_cmd))
    app.add_handler(CommandHandler("rec", rec_cmd))
    app.add_handler(CommandHandler("channels", channels_cmd))
    app.add_handler(CommandHandler("search", search_cmd))
    app.add_handler(CommandHandler("myinfo", myinfo))

    # Owner + Admin
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))

    # Owner only
    app.add_handler(CommandHandler("setowner", setowner_cmd))
    app.add_handler(CommandHandler("addadmin", addadmin_cmd))
    app.add_handler(CommandHandler("removeadmin", removeadmin_cmd))
    app.add_handler(CommandHandler("adminlist", adminlist_cmd))
    app.add_handler(CommandHandler("proxy", proxy_cmd))

    app.add_handler(CallbackQueryHandler(category_callback, pattern="^cat_"))
    app.add_handler(CallbackQueryHandler(back_callback, pattern="^back_"))
    app.add_handler(CallbackQueryHandler(howto_verify_callback, pattern="^howto_verify$"))

    # Auto-register bot commands with Telegram (shows in "/" menu)
    commands = [
        BotCommand("start",    "Bot info aur commands dekho"),
        BotCommand("help",     "Bot info aur commands dekho"),
        BotCommand("login",    "JioTV login — mobile number bhejo"),
        BotCommand("otp",      "OTP verify karo"),
        BotCommand("verify",   "Access unlock karo (40 min)"),
        BotCommand("rec",      "Catchup/recording link lo"),
        BotCommand("channels", "Channels list dekho"),
        BotCommand("search",   "Channel search karo"),
        BotCommand("myinfo",   "Apna info dekho"),
        BotCommand("broadcast","(Admin) Sabko message bhejo"),
    ]
    async def post_init(application):
        await application.bot.set_my_commands(commands)
        logger.info("Bot commands Telegram mein register ho gaye.")

    app.post_init = post_init

    logger.info("JioTV Telegram Bot start ho raha hai...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
