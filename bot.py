import re
import time
import uuid
import asyncio
import io
import os
import tempfile
import aiohttp
import pytz
from datetime import datetime, timedelta
from pyrogram import Client, filters, idle
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.types import BotCommand

# ─── Credentials ─────────────────────────────────────────────────────────────
API_ID    = int(os.environ["API_ID"])
API_HASH  = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

IST = pytz.timezone(os.environ.get("TIMEZONE", "Asia/Kolkata"))

# ─── Shortener / Verification Config ─────────────────────────────────────────
SHORTENER_API   = "65aa5be4d757fb7242fff9dde00f6cd5d4acc977"
SHORTENER_URL   = "http://shortxlinks.in"
BOT_USERNAME    = os.environ.get("BOT_USERNAME", "LS_Ower_bot")
TOKEN_VALIDITY  = 20 * 60   # 20 minutes in seconds
GROUP_ID        = int(os.environ.get("GROUP_ID", "-1003726271113"))

# ─── Verification State ───────────────────────────────────────────────────────
PENDING_TOKENS  = {}   # {token_id: {"user_id": int, "created_at": float}}
VERIFIED_USERS  = {}   # {user_id: {"expires_at": float}}
PREMIUM_USERS   = {}   # {user_id: {"expires_at": float}}


# ─── Load Airtel Channels from File ──────────────────────────────────────────
def load_airtel_channels(file_path="Airtel_Selected_Channel_Links.txt"):
    """Parse channel name → URL from the Airtel links text file."""
    channels = {}
    script_dir = os.path.dirname(os.path.abspath(__file__))
    full_path = os.path.join(script_dir, file_path)

    if not os.path.exists(full_path):
        print(f"⚠️  Warning: {full_path} not found!")
        return channels

    with open(full_path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    current_channel = None
    seen = set()
    for line in lines:
        line = line.strip()
        if not line or line.startswith("[AIRTEL"):
            continue
        if line.startswith("http"):
            if current_channel and current_channel not in seen:
                channels[current_channel.lower()] = line
                seen.add(current_channel)
            current_channel = None
        else:
            current_channel = line

    print(f"✅ Loaded {len(channels)} Airtel channels.")
    return channels

# ─── Channel Databases ────────────────────────────────────────────────────────
CHANNELS_DISHTV = {
    "pogo":            "http://ksrtech.fun/xtra.php/158792.ts",
    "discovery kids":  "http://line.sweetv.xyz/play/live.php?mac=00:1A:79:00:03:B2&stream=1540017&extension=ts&play_token=eFCOqzrsPI",
    "nick jr":         "http://line.andpor.com:80/play/live.php?mac=00:1A:79:C2:82:1D&stream=1540021&extension=ts&play_token=Z8zOJCukPa",
}

CHANNELS_AIRTEL = load_airtel_channels()

PROVIDER_MAP = {
    "dishtv": CHANNELS_DISHTV,
    "airtel": CHANNELS_AIRTEL,
}

# ─── Quality Map ─────────────────────────────────────────────────────────────
QUALITIES = {
    "140p":  "256:140",
    "360p":  "640:360",
    "480p":  "854:480",
    "576p":  "1024:576",
    "1080p": "1920:1080",
}

# Target encoding bitrate for each output variant. 1080p is an upscale when
# the source is native 576p; it cannot add detail that is not in the source.
VIDEO_BITRATES = {
    "140p":  "250k",
    "360p":  "800k",
    "480p":  "1400k",
    "576p":  "2500k",
    "1080p": "5000k",
}

# ─── Config ───────────────────────────────────────────────────────────────────
# OWNER_IDS supports a comma-separated list of Telegram user IDs, e.g. "123,456"
OWNER_IDS     = {int(x.strip()) for x in os.environ.get("OWNER_IDS", "5856009289").split(",")}
MAX_REC_LIMIT = timedelta(minutes=30)
WATERMARK_URL = "https://iili.io/Cew1rV1.png"

# ─── In-Memory State ─────────────────────────────────────────────────────────
ACTIVE_TASKS    = {}  # {task_id: {"user_id", "channel", "platform", "quality", "status", "process"}}
SCHEDULED_TASKS = {}  # {task_id: {...}}
PENDING_DL      = {}  # {key: {"platform", "channel", "duration_str", "scheduled"}}


def has_premium_access(user_id: int) -> bool:
    if user_id in OWNER_IDS:
        return True
    premium = PREMIUM_USERS.get(user_id)
    if not premium:
        return False
    if time.time() < premium["expires_at"]:
        return True
    del PREMIUM_USERS[user_id]
    return False


def is_group_or_owner(message) -> bool:
    """Normal users may use bot commands only in the configured group."""
    return message.chat.id == GROUP_ID or has_premium_access(message.from_user.id)


async def reject_private_user(message) -> bool:
    """Return True after notifying a normal user that private chat is disabled."""
    if is_group_or_owner(message):
        return False
    await message.reply_text(
        "🚫 **Private chat is disabled for normal users.**\n\n"
        "Please use this bot in the authorized group."
    )
    return True


async def reject_non_owner(message) -> bool:
    if message.from_user.id in OWNER_IDS:
        return False
    await message.reply_text("🚫 **Owner-only command.**")
    return True

# ─── Regex ────────────────────────────────────────────────────────────────────
# Supports both formats:
#   /dl -Dishtv -c Pogo -t 00:00:30
#   /dl -Airtel -c Sony Pal -t 30:00
#   /dl -Airtel -c "Zee TV HD" -t 00:30:00
#   /dl -Airtel -c "Sony Pal" -t 10:00:00 - 10:30:00
DL_PATTERN = (
    r"^/dl\s+(?:-(Airtel|Dishtv)\s+)?"    # provider flag optional; default = DishTV
    r"-c\s+"
    r'(?:"([^"]+)"|([^\-\n]+?))'          # quoted or unquoted channel name
    r"\s+-t\s+(\d{1,2}:\d{2}(?::\d{2})?)"  # start time / duration (MM:SS or HH:MM:SS)
    r"(?:\s*-\s*(\d{2}:\d{2}:\d{2}))?$"   # optional end time for scheduling
)

app = Client(
    "live_recorder_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)


# ─── Verification Helpers ─────────────────────────────────────────────────────
def is_user_verified(user_id):
    """Check if user has an active, non-expired verification token."""
    # Owners have permanent premium access and never need verification.
    if user_id in OWNER_IDS:
        return True

    if user_id in PREMIUM_USERS:
        if time.time() < PREMIUM_USERS[user_id]["expires_at"]:
            return True
        del PREMIUM_USERS[user_id]

    if user_id in VERIFIED_USERS:
        if time.time() < VERIFIED_USERS[user_id]["expires_at"]:
            return True
        else:
            del VERIFIED_USERS[user_id]  # Expired token remove kar dega
    return False


async def create_unique_short_link(user_id):
    """Har baar ek unique Random Token ID generate karke shortener link banayega."""
    # Unique 8-character Random Token
    unique_token_id = str(uuid.uuid4())[:8]

    # Dynamic Start Link (Deep-linking)
    target_url = f"https://telegram.me/{BOT_USERNAME}?start=verify_{unique_token_id}"

    # Shortener API Call
    api_endpoint = f"{SHORTENER_URL}/api?api={SHORTENER_API}&url={target_url}"

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_endpoint, timeout=aiohttp.ClientTimeout(total=8)) as resp:
                data = await resp.json(content_type=None)
                short_link = data.get("shortenedUrl") or data.get("short_url") or data.get("link")

        if short_link:
            # Token ID store karo verification check ke liye
            PENDING_TOKENS[unique_token_id] = {
                "user_id":    user_id,
                "created_at": time.time()
            }
            return short_link
    except Exception as e:
        print(f"API Error: {e}")

    return None


# ─── Helpers ──────────────────────────────────────────────────────────────────
def parse_duration(time_str: str):
    """Accept MM:SS or HH:MM:SS → (timedelta, 'HH:MM:SS' string) or (None, None)."""
    parts = list(map(int, time_str.split(":")))
    if len(parts) == 2:
        h, m, s = 0, parts[0], parts[1]
    elif len(parts) == 3:
        h, m, s = parts
    else:
        return None, None
    td = timedelta(hours=h, minutes=m, seconds=s)
    total = int(td.total_seconds())
    hh, rem = divmod(total, 3600)
    mm, ss  = divmod(rem, 60)
    return td, f"{hh:02}:{mm:02}:{ss:02}"


def parse_premium_duration(duration_str: str):
    """Parse premium access duration such as 12hours, 12h, 7days, or 30m."""
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]+)\s*", duration_str)
    if not match:
        return None, None

    amount = float(match.group(1))
    unit = match.group(2).lower()
    units = {
        "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
        "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
        "d": 86400, "day": 86400, "days": 86400,
    }
    if amount <= 0 or unit not in units:
        return None, None

    seconds = int(amount * units[unit])
    if seconds <= 0:
        return None, None
    return seconds, f"{amount:g} {unit}"


def hms_to_td(s: str) -> timedelta:
    h, m, sec = map(int, s.split(":"))
    return timedelta(hours=h, minutes=m, seconds=sec)


def td_to_hms(td: timedelta) -> str:
    total = int(td.total_seconds())
    h, r  = divmod(total, 3600)
    m, s  = divmod(r, 60)
    return f"{h:02}:{m:02}:{s:02}"


def now_ist() -> datetime:
    return datetime.now(IST).replace(tzinfo=None)


def generate_progress_message(channel_name, resolution, provider,
                               percentage, elapsed, speed, user, user_id, task_id):
    filled = int(percentage // 10)
    bar    = "■" * filled + "□" * (10 - filled)
    fname  = f"[{channel_name}].{resolution}.{provider}-DL.AAC.2.0.H264.-@Ls_ower_bot.mp4"
    return (
        f"📄 **File:**\n`{fname}`\n\n"
        f"**Progress:**\n[{bar}] {percentage}%\n\n"
        f"**Status:** Downloading\n"
        f"**Elapsed:** {elapsed}\n"
        f"**Speed:** {speed} MB/s\n\n\n"
        f"**Platform:** {provider}\n"
        f"**User:** {user}\n"
        f"**User ID:** `{user_id}`\n\n"
        f"❌ **Cancel Command:**\n`/cancel {task_id}`"
    )


def quality_keyboard(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("140p",  callback_data=f"dl|{key}|140p"),
            InlineKeyboardButton("360p",  callback_data=f"dl|{key}|360p"),
        ],
        [
            InlineKeyboardButton("480p",  callback_data=f"dl|{key}|480p"),
            InlineKeyboardButton("576p",  callback_data=f"dl|{key}|576p"),
        ],
        [
            InlineKeyboardButton("1080p", callback_data=f"dl|{key}|1080p"),
        ],
    ])


def upload_destination_keyboard(key: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📩 Private Chat", callback_data=f"upload|{key}|private"
            ),
            InlineKeyboardButton(
                "👥 Group", callback_data=f"upload|{key}|group"
            ),
        ],
    ])


# ─── /start ───────────────────────────────────────────────────────────────────
@app.on_message(filters.command("start"))
async def cmd_start(client, message):
    user_id = message.from_user.id
    args    = message.command

    if await reject_private_user(message):
        return

    # ── Deep-link verification callback: /start verify_TOKENID ──────────────
    if len(args) > 1 and args[1].startswith("verify_"):
        token_id = args[1].replace("verify_", "")

        if token_id in PENDING_TOKENS:
            token_data = PENDING_TOKENS[token_id]

            # Security check: token must belong to this user
            if token_data["user_id"] == user_id:
                VERIFIED_USERS[user_id] = {
                    "expires_at": time.time() + TOKEN_VALIDITY
                }
                del PENDING_TOKENS[token_id]   # One-time use – consume token

                await message.reply_text(
                    "🎉 **You have successfully verified!**\n\n"
                    "📺 **DishTV & Airtel Recorder Features Unlocked**\n"
                    "⏱ **Max Recording Time:** 30 minutes\n"
                    "⏳ Your token is valid for **20 minutes**."
                )
                return
            else:
                await message.reply_text(
                    "❌ **Verification Failed!** This token was generated for another user."
                )
                return
        else:
            await message.reply_text(
                "⚠️ **Invalid or Expired Token!** Please use `/verify` to generate a new link."
            )
            return

    # ── Default welcome ──────────────────────────────────────────────────────
    if message.chat.id == GROUP_ID:
        await message.reply_text(
            "📡 **Live Stream Recorder Bot**\n\n"
            "Group commands are enabled here.\n"
            "Use `/help` for the full command list."
        )
        return

    await message.reply_text(
        "📡 **Live Stream Recorder Bot**\n\n"
        "Record live TV from **Airtel** & **DishTV** directly to Telegram.\n\n"
        "🔐 Use `/verify` to unlock recording features.\n"
        "📖 /help — full command list"
    )


# ─── /verify ──────────────────────────────────────────────────────────────────
@app.on_message(filters.command("verify"))
async def cmd_verify(client, message):
    user_id = message.from_user.id

    if await reject_private_user(message):
        return

    if user_id in OWNER_IDS:
        await message.reply_text(
            "👑 **Owner Premium Access**\n\n"
            "✅ Verification is not required for your account.\n"
            "📺 **DishTV & Airtel Recorder Features Unlocked**\n"
            "⏱ **Max Recording Time:** 30 minutes"
        )
        return

    # Already verified?
    if is_user_verified(user_id):
        expiry = PREMIUM_USERS.get(user_id, VERIFIED_USERS.get(user_id, {})).get("expires_at")
        remaining_mins = max(0, int((expiry - time.time()) / 60)) if expiry else 0
        access_label = "Premium access" if user_id in PREMIUM_USERS else "Verification"
        await message.reply_text(
            f"✅ **{access_label} is active!**\n\n"
            f"📺 **DishTV & Airtel Recorder**\n"
            f"⏱ **Max Recording Limit:** 30 minutes\n"
            f"⏳ Your token expires in `{remaining_mins} minutes`."
        )
        return

    await message.reply_text("⏳ Generating your unique verification link...")

    # Generate a fresh unique link every time
    short_link = await create_unique_short_link(user_id)

    if not short_link:
        await message.reply_text(
            "❌ Failed to generate verification link. Please try again later."
        )
        return

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Verify Unique Token", url=short_link)]
    ])

    await message.reply_text(
        "🔐 **New Verification Token Generated!**\n\n"
        "Click the link below to complete verification. "
        "Every link is unique and one-time use only.\n\n"
        "⏳ **Token Validity:** 20 Minutes\n"
        "⏱ **Max Recording Duration:** 30 Minutes",
        reply_markup=keyboard
    )


# ─── Premium management (owner only) ─────────────────────────────────────────
@app.on_message(filters.command("premium_add"))
async def cmd_premium_add(client, message):
    if await reject_non_owner(message):
        return

    args = message.command
    if len(args) != 3:
        await message.reply_text(
            "Usage: `/premium_add <user_id> <duration>`\n"
            "Examples: `/premium_add 7902337172 12hours` or `7days`"
        )
        return

    try:
        user_id = int(args[1])
        duration_seconds, duration_label = parse_premium_duration(args[2])
        if user_id <= 0 or duration_seconds is None:
            raise ValueError
    except ValueError:
        await message.reply_text(
            "❌ User ID must be positive and duration must look like "
            "`12hours`, `7days`, or `30m`."
        )
        return

    expires_at = time.time() + duration_seconds
    PREMIUM_USERS[user_id] = {"expires_at": expires_at}
    expiry_text = datetime.fromtimestamp(expires_at, IST).strftime("%Y-%m-%d %H:%M IST")
    await message.reply_text(
        f"✅ **Premium added**\n\n"
        f"👤 User ID: `{user_id}`\n"
        f"📅 Duration: `{duration_label}`\n"
        f"⏳ Expires: `{expiry_text}`"
    )


@app.on_message(filters.command("premium_expire"))
async def cmd_premium_expire(client, message):
    if await reject_non_owner(message):
        return

    args = message.command
    if len(args) != 2:
        await message.reply_text("Usage: `/premium_expire <user_id>`")
        return

    try:
        user_id = int(args[1])
    except ValueError:
        await message.reply_text("❌ User ID must be a number.")
        return

    if PREMIUM_USERS.pop(user_id, None) is None:
        await message.reply_text(f"ℹ️ No active premium found for `{user_id}`.")
        return
    await message.reply_text(f"✅ Premium expired for user `{user_id}`.")


@app.on_message(filters.command("premium_total"))
async def cmd_premium_total(client, message):
    if await reject_non_owner(message):
        return

    now = time.time()
    expired_ids = [
        user_id for user_id, data in PREMIUM_USERS.items()
        if data["expires_at"] <= now
    ]
    for user_id in expired_ids:
        del PREMIUM_USERS[user_id]

    await message.reply_text(
        f"👑 **Premium Users: `{len(PREMIUM_USERS)}`**\n"
        f"Use `/premium_add <user_id> <days>` to add access."
    )


# ─── /help ────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("help"))
async def cmd_help(client, message):
    if await reject_private_user(message):
        return
    await message.reply_text(
        "📖 **Bot Help & Commands**\n\n"
        "⏰ All times are in IST (Asia/Kolkata).\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "🔐 **Verification** (required before recording):\n"
        "`/verify` — get a one-time verification link\n\n"
        "**📥 DishTV** (no flag needed):\n"
        "`/dl -c Pogo -t 00:00:30`\n\n"
        "**📥 Airtel** (flag required):\n"
        "`/dl -Airtel -c Sony Pal -t 00:00:50`\n"
        "`/dl -Airtel -c \"Zee TV HD\" -t 30:00`\n\n"
        "**📅 Scheduled (IST range):**\n"
        "`/dl -Airtel -c Sony Pal -t 10:00:00 - 10:30:00`\n\n"
        "**📋 Channel lists:**\n"
        "`/channels -Airtel` | `/channels -Dishtv`\n\n"
        "**🔄 Task management:**\n"
        "`/status` — active recordings\n"
        "`/myschedules` — pending schedules\n"
        "`/cancel <task_id>` — stop a task\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "• Max recording: **30 minutes**",
        disable_web_page_preview=True,
    )


# ─── /channels ────────────────────────────────────────────────────────────────
@app.on_message(filters.command("channels"))
async def cmd_channels(client, message):
    if await reject_private_user(message):
        return
    args = message.command
    if len(args) < 2:
        await message.reply_text("Usage: `/channels -Airtel` or `/channels -Dishtv`")
        return
    platform = args[1].lstrip("-").lower()
    if platform not in PROVIDER_MAP:
        await message.reply_text("Usage: `/channels -Airtel` or `/channels -Dishtv`")
        return
    ch_dict = PROVIDER_MAP[platform]
    if not ch_dict:
        await message.reply_text(f"⚠️ No channels configured for **{platform.title()}**.")
        return
    lines = [f"📺 **{platform.title()} Channels ({len(ch_dict)}):**\n"]
    for name in ch_dict:
        lines.append(f"• `{name.title()}`")
    # Telegram message limit: split if too long
    text = "\n".join(lines)
    if len(text) > 4000:
        chunks = [lines[0]]
        for entry in lines[1:]:
            if sum(len(c) for c in chunks) + len(entry) > 3800:
                await message.reply_text("\n".join(chunks))
                chunks = []
            chunks.append(entry)
        if chunks:
            await message.reply_text("\n".join(chunks))
    else:
        await message.reply_text(text)


# ─── /dl ──────────────────────────────────────────────────────────────────────
@app.on_message(filters.command("dl"))
async def cmd_dl(client, message):
    user_id = message.from_user.id

    if await reject_private_user(message):
        return

    # ── Verification gate ─────────────────────────────────────────────────────
    if not is_user_verified(user_id):
        await message.reply_text(
            "🔒 **Access Denied!**\n\n"
            "You need to verify before recording.\n"
            "Use `/verify` to get your verification link."
        )
        return

    match = re.match(DL_PATTERN, message.text, re.IGNORECASE)
    if not match:
        await message.reply_text(
            "❌ **Invalid Format!**\n\n"
            "**DishTV** (no flag needed):\n"
            "`/dl -c Pogo -t 00:00:30`\n\n"
            "**Airtel** (flag required):\n"
            "`/dl -Airtel -c Sony Pal -t 00:00:50`\n"
            "`/dl -Airtel -c \"Zee TV HD\" -t 30:00`\n"
            "`/dl -Airtel -c Sony Pal -t 10:00:00 - 10:30:00`"
        )
        return

    platform     = (match.group(1) or "Dishtv").lower()  # default to DishTV
    channel_name = (match.group(2) or match.group(3)).strip().lower()
    time_start   = match.group(4)   # MM:SS or HH:MM:SS
    time_end     = match.group(5)   # HH:MM:SS or None

    ch_dict = PROVIDER_MAP[platform]

    if channel_name not in ch_dict:
        available = ", ".join(f"`{c.title()}`" for c in list(ch_dict)[:10])
        more = f" (+{len(ch_dict)-10} more)" if len(ch_dict) > 10 else ""
        await message.reply_text(
            f"❌ **Channel not found in {platform.title()}!**\n"
            f"Showing first 10: {available}{more}\n\n"
            f"Use `/channels -{platform.title()}` for full list."
        )
        return

    # ── Time range (scheduled) ──
    if time_end:
        t_start = hms_to_td(time_start if len(time_start.split(":")) == 3 else f"00:{time_start}")
        t_end   = hms_to_td(time_end)
        if t_end <= t_start:
            await message.reply_text("❌ End time must be after start time.")
            return
        duration    = t_end - t_start
        duration_str = td_to_hms(duration)

        now    = now_ist()
        now_td = timedelta(hours=now.hour, minutes=now.minute, seconds=now.second)

        if t_start > now_td:
            task_id = str(uuid.uuid4())[:8].upper()
            SCHEDULED_TASKS[task_id] = {
                "user_id":      user_id,
                "channel":      channel_name,
                "platform":     platform,
                "start_hms":    time_start,
                "end_hms":      time_end,
                "duration_str": duration_str,
                "quality":      None,
                "chat_id":      message.chat.id,
            }
            key = f"{user_id}_{message.id}"
            PENDING_DL[key] = {
                "platform":     platform,
                "channel":      channel_name,
                "duration_str": duration_str,
                "scheduled":    task_id,
            }
            await message.reply_text(
                f"🗓 **Recording Scheduled!**\n\n"
                f"📡 **Provider:** `{platform.title()}`\n"
                f"📺 **Channel:** `{channel_name.title()}`\n"
                f"⏰ **Start:** `{time_start} IST`\n"
                f"⏹ **End:** `{time_end} IST`\n"
                f"⏱ **Duration:** `{duration_str}`\n"
                f"🆔 **Task ID:** `{task_id}`\n\n"
                f"👇 **Select Quality:**",
                reply_markup=quality_keyboard(key),
            )
            return
        # Start time passed → record immediately for computed duration
    else:
        duration, duration_str = parse_duration(time_start)
        if not duration:
            await message.reply_text("❌ Invalid time format. Use `MM:SS` or `HH:MM:SS`.")
            return

    if duration > MAX_REC_LIMIT:
        await message.reply_text(
            "⚠️ **Max Limit Exceeded!**\n"
            "Maximum duration is `00:30:00` (30 minutes)."
        )
        return

    key = f"{user_id}_{message.id}"
    PENDING_DL[key] = {
        "platform":     platform,
        "channel":      channel_name,
        "duration_str": duration_str,
        "scheduled":    None,
    }
    await message.reply_text(
        f"📡 **Provider:** `{platform.title()}`\n"
        f"📺 **Channel:** `{channel_name.title()}`\n"
        f"⏱ **Duration:** `{duration_str}`\n\n"
        f"👇 **Select Quality:**",
        reply_markup=quality_keyboard(key),
    )


# ─── Quality callback → FFmpeg ────────────────────────────────────────────────
@app.on_callback_query(filters.regex(r"^dl\|"))
async def cb_quality(client, callback_query):
    if callback_query.message.chat.id != GROUP_ID and not has_premium_access(callback_query.from_user.id):
        await callback_query.answer(
            "Please use the bot in the authorized group.",
            show_alert=True,
        )
        return

    _, key, quality = callback_query.data.split("|")

    pending = PENDING_DL.get(key)
    if not pending:
        await callback_query.answer("Session expired. Send /dl again.", show_alert=True)
        return

    platform     = pending["platform"]
    channel_name = pending["channel"]
    duration_str = pending["duration_str"]
    scheduled_id = pending.get("scheduled")

    stream_url = PROVIDER_MAP[platform].get(channel_name)
    if not stream_url:
        await callback_query.message.edit_text("❌ Stream URL not found.")
        return

    if has_premium_access(callback_query.from_user.id) and not scheduled_id:
        pending["quality"] = quality
        await callback_query.message.edit_text(
            f"✅ **Quality selected:** `{quality}`\n\n"
            "📤 **Where should I upload the recording?**",
            reply_markup=upload_destination_keyboard(key),
        )
        return

    PENDING_DL.pop(key, None)

    if scheduled_id and scheduled_id in SCHEDULED_TASKS:
        SCHEDULED_TASKS[scheduled_id]["quality"] = quality
        await callback_query.message.edit_text(
            f"✅ **Scheduled!** Quality set to `{quality}`\n"
            f"🆔 Task ID: `{scheduled_id}`\n"
            f"Recording will start at the scheduled IST time."
        )
        asyncio.create_task(run_scheduled(client, scheduled_id))
        return

    task_id     = str(uuid.uuid4())[:8].upper()
    user        = callback_query.from_user
    user_display = f"@{user.username}" if user.username else user.first_name
    user_id_disp = user.id

    ACTIVE_TASKS[task_id] = {
        "user_id":  user.id,
        "channel":  channel_name,
        "platform": platform,
        "quality":  quality,
        "status":   "Recording...",
        "process":  None,
        "cancelled": False,
    }

    await callback_query.message.edit_text(
        f"⏳ **Recording Started**\n\n"
        f"📡 **Provider:** `{platform.title()}`\n"
        f"📺 **Channel:** `{channel_name.title()}`\n"
        f"🎬 **Quality:** `{quality}`\n"
        f"⏱ **Duration:** `{duration_str}`\n"
        f"🆔 **Task ID:** `{task_id}`\n\n"
        f"_Please wait..._"
    )

    await do_ffmpeg(
        client, callback_query.message, task_id,
        stream_url, duration_str, QUALITIES[quality],
        channel_name, quality, platform,
        user_display, user_id_disp,
    )


@app.on_callback_query(filters.regex(r"^upload\|"))
async def cb_upload_destination(client, callback_query):
    _, key, destination = callback_query.data.split("|")

    if not has_premium_access(callback_query.from_user.id):
        await callback_query.answer("Premium access required.", show_alert=True)
        return

    pending = PENDING_DL.pop(key, None)
    if not pending or not pending.get("quality"):
        await callback_query.answer("Session expired. Send /dl again.", show_alert=True)
        return

    quality = pending["quality"]
    platform = pending["platform"]
    channel_name = pending["channel"]
    duration_str = pending["duration_str"]
    stream_url = PROVIDER_MAP[platform].get(channel_name)
    if not stream_url:
        await callback_query.message.edit_text("❌ Stream URL not found.")
        return

    upload_chat_id = (
        callback_query.from_user.id if destination == "private" else GROUP_ID
    )
    destination_label = "Private Chat" if destination == "private" else "Group"
    task_id = str(uuid.uuid4())[:8].upper()
    user = callback_query.from_user
    user_display = f"@{user.username}" if user.username else user.first_name

    ACTIVE_TASKS[task_id] = {
        "user_id": user.id,
        "channel": channel_name,
        "platform": platform,
        "quality": quality,
        "status": f"Recording ({destination_label})...",
        "process": None,
        "cancelled": False,
    }

    await callback_query.message.edit_text(
        f"⏳ **Recording Started**\n\n"
        f"📡 **Provider:** `{platform.title()}`\n"
        f"📺 **Channel:** `{channel_name.title()}`\n"
        f"🎬 **Quality:** `{quality}`\n"
        f"📤 **Upload:** `{destination_label}`\n"
        f"⏱ **Duration:** `{duration_str}`\n"
        f"🆔 **Task ID:** `{task_id}`\n\n"
        "_Please wait..._"
    )

    await do_ffmpeg(
        client, callback_query.message, task_id, stream_url, duration_str,
        QUALITIES[quality], channel_name, quality, platform,
        user_display, user.id, upload_chat_id,
    )


# ─── FFmpeg runner (in-memory, live progress) ────────────────────────────────
async def do_ffmpeg(client, msg, task_id, stream_url, duration_str,
                    scale, channel_name, quality, platform,
                    user_display="Unknown", user_id_disp="",
                    upload_chat_id=None):

    fname = (
        f"[{channel_name.title()}].{quality}"
        f".{platform.title()}-DL.AAC.2.0.H264.-@Ls_ower_bot.mp4"
    )
    if platform == "dishtv":
        filter_cx = (
            "[1:v]scale=100:-1[watermark];"
            "[0:v][watermark]overlay=main_w-overlay_w-60:main_h-overlay_h-20[outv]"
        )
        output_filename = os.path.join(tempfile.gettempdir(), f"recording_{task_id}.mp4")
        cmd = [
            "ffmpeg", "-y",
            "-rw_timeout", "30000000",
            "-progress", "pipe:2",
            "-nostats",
            "-reconnect", "1",
            "-reconnect_at_eof", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-i", stream_url,
            "-i", WATERMARK_URL,
            "-t", duration_str,
            "-filter_complex", filter_cx,
            "-map", "[outv]", "-map", "0:a?",
            "-s", scale,
            "-c:v", "libx264", "-crf", "23", "-preset", "ultrafast",
            "-c:a", "aac",
            output_filename,
        ]
    else:
        output_filename = None
        cmd = [
            "ffmpeg", "-y",
            "-rw_timeout", "30000000",
            "-progress", "pipe:2",
            "-nostats",
            "-i", stream_url,
            "-t", duration_str,
            "-vf", f"scale={scale}",
            "-map", "0:v", "-map", "0:a?",
            "-c:v", "libx264", "-preset", "slow",
            "-b:v", "330k",
            "-c:a", "aac", "-b:a", "48k",
            "-metadata:s:a:0", "title=@LittleSinghamChannel Hindi",
            "-metadata:s:a:0", "handler_name=@LittleSinghamChannel Hindi",
            "-metadata:s:a:0", "language=hin",
            "-metadata:s:a:1", "title=@LittleSinghamChannel Tamil",
            "-metadata:s:a:1", "handler_name=@LittleSinghamChannel Tamil",
            "-metadata:s:a:1", "language=tam",
            "-metadata:s:a:2", "title=@LittleSinghamChannel Telugu",
            "-metadata:s:a:2", "handler_name=@LittleSinghamChannel Telugu",
            "-metadata:s:a:2", "language=tel",
            "-f", "mp4", "-movflags", "frag_keyframe+empty_moov",
            "pipe:1",
        ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    if task_id in ACTIVE_TASKS:
        ACTIVE_TASKS[task_id]["process"] = process

    total_secs  = max(1, hms_to_td(duration_str).total_seconds())
    video_buf   = io.BytesIO()
    if task_id in ACTIVE_TASKS:
        # Keep the bytes collected so far available if /cancel is used.
        ACTIVE_TASKS[task_id]["video_buf"] = video_buf
    last_update = asyncio.get_event_loop().time()
    last_pct    = [0]
    last_elapsed= ["00:00:00"]
    last_speed  = ["0.00"]

    # ── Read stdout (video bytes) ──────────────────────────────────────────
    async def drain_stdout():
        while True:
            chunk = await process.stdout.read(65536)
            if not chunk:
                break
            video_buf.write(chunk)

    # ── Read stderr (progress lines) ──────────────────────────────────────
    async def drain_stderr():
        nonlocal last_update
        while True:
            line = await process.stderr.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="ignore")

            progress_seconds = None
            t = re.search(r"out_time=(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?", text)
            if t:
                progress_seconds = (
                    int(t.group(1)) * 3600
                    + int(t.group(2)) * 60
                    + int(t.group(3))
                )
            else:
                t_ms = re.search(r"out_time_ms=(\d+)", text)
                if t_ms:
                    progress_seconds = int(t_ms.group(1)) // 1_000_000
                else:
                    t_legacy = re.search(r"time=(\d{2}):(\d{2}):(\d{2})", text)
                    if t_legacy:
                        progress_seconds = (
                            int(t_legacy.group(1)) * 3600
                            + int(t_legacy.group(2)) * 60
                            + int(t_legacy.group(3))
                        )
            s = re.search(r"speed=\s*([\d.]+)x", text)
            b = re.search(r"bitrate=\s*([\d.]+)kbits/s", text)

            if progress_seconds is not None:
                hours, remainder = divmod(progress_seconds, 3600)
                minutes, seconds = divmod(remainder, 60)
                last_elapsed[0] = f"{hours:02}:{minutes:02}:{seconds:02}"
                last_pct[0] = min(99, int(progress_seconds / total_secs * 100))
            if b:
                mb_s             = float(b.group(1)) / 8 / 1024
                last_speed[0]    = f"{mb_s:.2f}"
            elif s:
                last_speed[0]    = f"{float(s.group(1)):.2f}"

            now = asyncio.get_event_loop().time()
            if now - last_update >= 5:
                last_update = now
                try:
                    await msg.edit_text(
                        generate_progress_message(
                            channel_name = channel_name.title(),
                            resolution   = quality,
                            provider     = platform.title(),
                            percentage   = last_pct[0],
                            elapsed      = last_elapsed[0],
                            speed        = last_speed[0],
                            user         = user_display,
                            user_id      = user_id_disp,
                            task_id      = task_id,
                        )
                    )
                except Exception:
                    pass

    # Never leave a task stuck forever if the IPTV server or watermark host
    # stops responding. Allow extra time for slow 1080p encoding.
    recording_timeout = total_secs + 120
    try:
        await asyncio.wait_for(
            asyncio.gather(drain_stdout(), drain_stderr()),
            timeout=recording_timeout,
        )
        await process.wait()
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()
        ACTIVE_TASKS.pop(task_id, None)
        await msg.edit_text(
            f"❌ **Recording Timed Out**\n\n"
            f"🆔 `{task_id}` | 📺 `{channel_name.title()}`\n"
            f"Stream server ne response nahi diya. Please try again."
        )
        return
    if output_filename and os.path.exists(output_filename):
        try:
            with open(output_filename, "rb") as recorded_file:
                video_buf = io.BytesIO(recorded_file.read())
        except OSError:
            pass

    was_cancelled = bool(ACTIVE_TASKS.get(task_id, {}).get("cancelled"))
    ACTIVE_TASKS.pop(task_id, None)

    if process.returncode != 0:
        if was_cancelled and video_buf.getbuffer().nbytes > 0:
            video_buf.seek(0)
            video_buf.name = fname
            await msg.reply_video(
                video=video_buf,
                caption=(
                    f"⚠️ **Partial Recording**\n\n"
                    f"📺 **Channel:** `{channel_name.title()}`\n"
                    f"🎬 **Quality:** `{quality}`\n"
                    f"🆔 **Task ID:** `{task_id}`\n"
                    f"⏹ **Status:** Cancelled by user"
                ),
                supports_streaming=True,
            )
            await msg.edit_text(
                f"⏹ **Recording cancelled.** Partial video uploaded.\n"
                f"🆔 `{task_id}`"
            )
            return
        await msg.edit_text(
            f"❌ **Recording Failed**\n\n"
            f"🆔 `{task_id}` | 📺 `{channel_name.title()}`"
        )
        return

    # ── Generate thumbnail from memory ─────────────────────────────────────
    await msg.edit_text("🖼 **Generating thumbnail...**")
    thumbnail_buf = None
    try:
        thumbnail_process = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y",
            "-i", "pipe:0",
            "-frames:v", "1",
            "-vf", "scale=320:-1",
            "-f", "image2pipe",
            "-vcodec", "mjpeg",
            "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        thumbnail_bytes, _ = await thumbnail_process.communicate(video_buf.getvalue())
        if thumbnail_process.returncode == 0 and thumbnail_bytes:
            thumbnail_buf = io.BytesIO(thumbnail_bytes)
            thumbnail_buf.name = "thumbnail.jpg"
    except Exception:
        thumbnail_buf = None

    # ── Upload from memory ─────────────────────────────────────────────────
    video_buf.seek(0)
    # Pyrogram reads the upload filename from the file-like object's name.
    video_buf.name = fname
    caption = (
        f"✅ **Recording Complete**\n\n"
        f"📡 **Provider:** `{platform.title()}`\n"
        f"📺 **Channel:** `{channel_name.title()}`\n"
        f"🎬 **Quality:** `{quality}`\n"
        f"⏱ **Duration:** `{duration_str}`\n"
        f"🆔 **Task ID:** `{task_id}`"
    )
    upload_args = {
        "video": video_buf,
        "caption": caption,
        "supports_streaming": True,
    }
    if thumbnail_buf is not None:
        thumbnail_buf.seek(0)
        upload_args["thumb"] = thumbnail_buf
    if upload_chat_id is None or upload_chat_id == msg.chat.id:
        await msg.reply_video(**upload_args)
    else:
        await client.send_video(upload_chat_id, **upload_args)
    await msg.edit_text(f"✅ **Done!** `{task_id}` — file sent above.")
    if output_filename:
        try:
            os.remove(output_filename)
        except OSError:
            pass


# ─── Scheduled runner ─────────────────────────────────────────────────────────
async def run_scheduled(client, task_id: str):
    task = SCHEDULED_TASKS.get(task_id)
    if not task:
        return
    now    = now_ist()
    now_td = timedelta(hours=now.hour, minutes=now.minute, seconds=now.second)
    start  = hms_to_td(task["start_hms"] if len(task["start_hms"].split(":")) == 3
                       else f"00:{task['start_hms']}")
    wait_s = max(0, (start - now_td).total_seconds())
    await asyncio.sleep(wait_s)

    quality   = task.get("quality") or "480p"
    platform  = task["platform"]
    channel   = task["channel"]
    duration  = task["duration_str"]
    stream_url = PROVIDER_MAP[platform].get(channel)

    SCHEDULED_TASKS.pop(task_id, None)
    ACTIVE_TASKS[task_id] = {
        "user_id":  task["user_id"],
        "channel":  channel,
        "platform": platform,
        "quality":  quality,
        "status":   "Recording (scheduled)...",
        "process":  None,
    }
    try:
        notif = await client.send_message(
            task["chat_id"],
            f"⏰ **Scheduled Recording Started**\n\n"
            f"📡 `{platform.title()}` | 📺 `{channel.title()}`\n"
            f"🎬 `{quality}` | ⏱ `{duration}`\n"
            f"🆔 `{task_id}`",
        )
        await do_ffmpeg(client, notif, task_id, stream_url, duration,
                        QUALITIES[quality], channel, quality, platform,
                        "Scheduled", task["user_id"])
    except Exception as e:
        print(f"Scheduled task {task_id} error: {e}")


# ─── /status ──────────────────────────────────────────────────────────────────
@app.on_message(filters.command("status"))
async def cmd_status(client, message):
    if await reject_private_user(message):
        return
    user_id = message.from_user.id
    tasks = {tid: t for tid, t in ACTIVE_TASKS.items() if t["user_id"] == user_id}
    if not tasks:
        await message.reply_text("ℹ️ No active recordings right now.")
        return
    lines = ["🔄 **Active Recordings:**\n"]
    for tid, t in tasks.items():
        lines.append(
            f"• `{tid}` | 📺 `{t['channel'].title()}` ({t['platform'].title()}) "
            f"| 🎬 `{t['quality']}` | _{t['status']}_"
        )
    await message.reply_text("\n".join(lines))


# ─── /myschedules ─────────────────────────────────────────────────────────────
@app.on_message(filters.command("myschedules"))
async def cmd_myschedules(client, message):
    if await reject_private_user(message):
        return
    user_id = message.from_user.id
    tasks = {tid: t for tid, t in SCHEDULED_TASKS.items() if t["user_id"] == user_id}
    if not tasks:
        await message.reply_text("ℹ️ No scheduled recordings.")
        return
    lines = ["🗓 **Your Scheduled Recordings:**\n"]
    for tid, t in tasks.items():
        q = t.get("quality") or "Not selected"
        lines.append(
            f"• `{tid}` | 📺 `{t['channel'].title()}` ({t['platform'].title()}) "
            f"| ⏰ `{t['start_hms']} - {t['end_hms']} IST` | 🎬 `{q}`"
        )
    await message.reply_text("\n".join(lines))


# ─── /cancel ──────────────────────────────────────────────────────────────────
@app.on_message(filters.command("cancel"))
async def cmd_cancel(client, message):
    if await reject_private_user(message):
        return
    args = message.command
    if len(args) < 2:
        await message.reply_text("Usage: `/cancel <task_id>`")
        return
    task_id = args[1].upper()
    if task_id in ACTIVE_TASKS:
        task = ACTIVE_TASKS[task_id]
        proc = task.get("process")
        if proc:
            try:
                task["cancelled"] = True
                proc.send_signal(__import__("signal").SIGINT)
            except Exception:
                pass
        else:
            task["cancelled"] = True
        await message.reply_text(
            f"⏹ Recording `{task_id}` cancellation requested.\n"
            "The partial video will be uploaded if data was saved."
        )
    elif task_id in SCHEDULED_TASKS:
        SCHEDULED_TASKS.pop(task_id, None)
        await message.reply_text(f"🗑 Scheduled task `{task_id}` removed.")
    else:
        await message.reply_text("❌ Task ID not found or already completed.")


# ─── Telegram command menu ────────────────────────────────────────────────────
BOT_COMMANDS = [
    BotCommand("start", "Start the bot"),
    BotCommand("help", "Show available commands"),
    BotCommand("verify", "Generate a verification link"),
    BotCommand("dl", "Record a TV channel"),
    BotCommand("channels", "List provider channels"),
    BotCommand("status", "Show active recordings"),
    BotCommand("myschedules", "Show scheduled recordings"),
    BotCommand("cancel", "Cancel a recording"),
    BotCommand("premium_add", "Add premium access (owner only)"),
    BotCommand("premium_expire", "Expire premium access (owner only)"),
    BotCommand("premium_total", "Count premium users (owner only)"),
]


async def main():
    print("Starting Live Recorder Bot...")
    await app.start()
    await app.set_bot_commands(BOT_COMMANDS)
    print("✅ Telegram command menu registered.")
    await idle()
    await app.stop()


# ─── Run ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    app.run(main())
