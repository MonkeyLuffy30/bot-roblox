\
import os
import json
import asyncio
import logging
from datetime import datetime, timedelta
import pytz

import aiohttp
import discord
from discord.ext import commands, tasks

# ------------------ Logging ------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
log = logging.getLogger("bot")

# ------------------ Config & Secrets ------------------
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
ROBLOX_COOKIE = os.getenv("ROBLOX_COOKIE")
ROBLOX_USER_ID = os.getenv("ROBLOX_USER_ID")  # string or number

if not DISCORD_TOKEN:
    log.critical("DISCORD_TOKEN environment variable is not set. Exiting.")
    raise SystemExit(1)
if not ROBLOX_COOKIE:
    log.critical("ROBLOX_COOKIE environment variable is not set. Exiting.")
    raise SystemExit(1)
if not ROBLOX_USER_ID:
    log.critical("ROBLOX_USER_ID environment variable is not set. Exiting.")
    raise SystemExit(1)

# Config file (channel ids)
CONFIG_PATH = "config.json"
if not os.path.exists(CONFIG_PATH):
    sample = {"status_channel_id": 123456789012345678, "notif_channel_id": 987654321098765432}
    with open(CONFIG_PATH, "w") as f:
        json.dump(sample, f, indent=4)
    log.info(f"Created sample {CONFIG_PATH}. Please edit it with channel IDs.")

with open(CONFIG_PATH, "r") as f:
    cfg = json.load(f)

try:
    STATUS_CHANNEL_ID = int(cfg.get("status_channel_id"))
    NOTIF_CHANNEL_ID = int(cfg.get("notif_channel_id"))
except Exception as e:
    log.critical("Invalid channel IDs in config.json. Please set valid integers.")
    raise SystemExit(1)

# Players file for manual add/hapus
PLAYERS_PATH = "players.json"
if not os.path.exists(PLAYERS_PATH):
    with open(PLAYERS_PATH, "w") as f:
        json.dump([], f)

with open(PLAYERS_PATH, "r") as f:
    try:
        manual_tracked = json.load(f)
    except Exception:
        manual_tracked = []

# State file to persist status message id across restarts (optional)
STATE_PATH = "state.json"
state = {}
if os.path.exists(STATE_PATH):
    try:
        with open(STATE_PATH, "r") as f:
            state = json.load(f)
    except Exception:
        state = {}

# ------------------ Bot & Globals ------------------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

JAKARTA = pytz.timezone("Asia/Jakarta")
START_TIME = datetime.now(JAKARTA)
RESTART_INTERVAL = 12 * 60 * 60  # seconds
UPDATE_INTERVAL = 5  # seconds to edit status message
BAR_LENGTH = 20

status_message_id = state.get("status_message_id")

# Presence tracking
online_since = {}   # {userId: datetime when observed online}
game_since = {}     # {userId: datetime when observed in-game}
last_online_set = set()

# aiohttp session (created on_ready)
http_session = None

# ------------------ Helper functions ------------------
def format_timedelta(td: timedelta) -> str:
    secs = int(td.total_seconds())
    hours, rem = divmod(secs, 3600)
    minutes, seconds = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"

def now_wib() -> datetime:
    return datetime.now(JAKARTA)

async def chunked(iterable, n=100):
    for i in range(0, len(iterable), n):
        yield iterable[i:i + n]

# ------------------ Roblox API calls ------------------
async def get_friends_list():
    \"\"\"Return list of friends' userIds (from account owning the cookie).\"\"\"
    url = "https://friends.roblox.com/v1/my/friends"
    friends = []
    cursor = None
    headers = {"Cookie": f".ROBLOSECURITY={ROBLOX_COOKIE}", "User-Agent": "DiscordRobloxBot/1.0"}

    while True:
        params = {"limit": 100}
        if cursor:
            params["cursor"] = cursor
        async with http_session.get(url, headers=headers, params=params) as resp:
            if resp.status != 200:
                log.warning(f"Failed to fetch friends list: {resp.status}")
                return []
            data = await resp.json()
        for item in data.get("data", []):
            friends.append(item.get("id"))
        cursor = data.get("nextPageCursor")
        if not cursor:
            break
    return friends

async def get_presences(user_ids):
    \"\"\"Return presence objects for provided user ids (list of ints).\"\"\"
    if not user_ids:
        return []
    url = "https://presence.roblox.com/v1/presence/users"
    headers = {"Content-Type": "application/json", "Cookie": f".ROBLOSECURITY={ROBLOX_COOKIE}", "User-Agent": "DiscordRobloxBot/1.0"}
    results = []
    for chunk in await chunked(list(user_ids), 100):
        payload = {"userIds": chunk}
        async with http_session.post(url, headers=headers, json=payload) as resp:
            if resp.status != 200:
                log.warning(f"Failed presence check (status {resp.status})")
                continue
            data = await resp.json()
            results.extend(data.get("userPresences", []))
    return results

async def get_usernames(user_ids):
    \"\"\"Batch get usernames for ids via users API. Returns dict id->username.\"\"\"
    res = {}
    url = "https://users.roblox.com/v1/users"
    headers = {"Content-Type": "application/json", "User-Agent": "DiscordRobloxBot/1.0"}
    for chunk in await chunked(list(user_ids), 100):
        async with http_session.post(url, headers=headers, json={"userIds": chunk}) as resp:
            if resp.status != 200:
                continue
            data = await resp.json()
            for u in data.get("data", []):
                res[int(u.get("id"))] = u.get("name") or u.get("displayName") or str(u.get("id"))
    return res

# ------------------ Core task: update status message ------------------
@tasks.loop(seconds=UPDATE_INTERVAL)
async def update_status_message():
    global status_message_id, last_online_set
    if bot.is_closed():
        return
    try:
        status_channel = bot.get_channel(STATUS_CHANNEL_ID)
        notif_channel = bot.get_channel(NOTIF_CHANNEL_ID)
        if status_channel is None or notif_channel is None:
            log.warning("One or more channels not found (check config.json)")
            return

        # build list of monitored user ids: friends + manual tracked
        friends = await get_friends_list()
        monitored = set(friends) | set(int(x) for x in manual_tracked)
        try:
            monitored.discard(int(ROBLOX_USER_ID))
        except Exception:
            pass

        presences = await get_presences(list(monitored))
        presence_map = {int(p.get("userId")): p for p in presences}

        # update online/game timers and prepare friend display
        display_lines = []
        current_online_set = set()
        ids_to_label = list(presence_map.keys())
        username_map = await get_usernames(ids_to_label) if ids_to_label else {}

        for uid, presence in presence_map.items():
            uid = int(uid)
            user_presence_type = presence.get("userPresenceType", 0)
            is_online = user_presence_type != 0
            last_location = presence.get("lastLocation") or "Tidak bermain"
            name = username_map.get(uid, str(uid))

            now = now_wib()
            if is_online:
                current_online_set.add(uid)
                if uid not in online_since:
                    online_since[uid] = now
                if user_presence_type == 2:
                    if uid not in game_since:
                        game_since[uid] = now
                else:
                    game_since.pop(uid, None)

                online_dur = now - online_since.get(uid, now)
                game_dur = (now - game_since[uid]) if uid in game_since else timedelta(0)

                display_lines.append((name, format_timedelta(online_dur), last_location, format_timedelta(game_dur)))
            else:
                if uid in last_online_set:
                    was_online_since = online_since.pop(uid, None)
                    was_game_since = game_since.pop(uid, None)
                    if was_online_since:
                        total_online = now - was_online_since
                    else:
                        total_online = timedelta(0)
                    total_game = (now - was_game_since) if was_game_since else timedelta(0)
                    try:
                        await notif_channel.send(
                            f"❌ **{name}** offline pada {now.strftime('%H:%M:%S %d/%m/%Y WIB')}\\n"
                            f"   🕒 Total Online: {format_timedelta(total_online)}\\n"
                            f"   🎯 Total Bermain: {format_timedelta(total_game)}"
                        )
                    except Exception as e:
                        log.warning(f"Failed to send offline notif: {e}")

        # detect new online users (send online notif)
        new_online = current_online_set - last_online_set
        for uid in new_online:
            name = username_map.get(uid, str(uid))
            t = now_wib()
            try:
                await notif_channel.send(f"✅ **{name}** baru saja online pada {t.strftime('%H:%M:%S %d/%m/%Y WIB')}")
            except Exception as e:
                log.warning(f"Failed to send online notif: {e}")

        # prepare friend list text (sorted by name)
        display_lines.sort(key=lambda x: x[0].lower())
        friends_text = ""
        for idx, (name, online_s, game_name, game_s) in enumerate(display_lines, start=1):
            friends_text += f"{idx}. **{name}**\\n   🕒 Online: {online_s}\\n   🎯 Game: {game_name}\\n   ⌛ Waktu Bermain: {game_s}\\n\\n"
        if not friends_text:
            friends_text = "_Tidak ada teman online_\\n"

        # Build status box + progress bar (WIB times)
        now = now_wib()
        uptime = now - START_TIME
        uptime_s = format_timedelta(uptime)
        elapsed = int(uptime.total_seconds())
        remaining = max(RESTART_INTERVAL - elapsed, 0)
        remaining_s = format_timedelta(timedelta(seconds=remaining))
        progress = min(max(elapsed / RESTART_INTERVAL, 0.0), 1.0)
        filled = int(progress * BAR_LENGTH)
        bar = "█" * filled + "░" * (BAR_LENGTH - filled)
        last_update = now.strftime("%H:%M:%S %d/%m/%Y WIB")

        status_box = (
            "╔════════════ 📊 STATUS BOT ════════════╗\\n"
            f"║ ⏳ Uptime        : {uptime_s:<20}║\\n"
            f"║ 👥 Teman Online  : {len(display_lines):<20}║\\n"
            f"║ 🕒 Update Terakhir: {last_update:<20}║\\n"
            f"║ 🔄 Restart Dalam : {remaining_s:<20}║\\n"
            f"║ [{bar}]                 ║\\n"
            "╚═══════════════════════════════════════╝\\n\\n"
            f"🎮 **DAFTAR TEMAN ONLINE**\\n{friends_text}"
        )

        # send or edit status message
        try:
            if status_message_id:
                try:
                    msg = await status_channel.fetch_message(status_message_id)
                    await msg.edit(content=f"```{status_box}```")
                except discord.NotFound:
                    sent = await status_channel.send(content=f"```{status_box}```")
                    status_message_id = sent.id
                except discord.Forbidden:
                    log.warning("Missing permissions to edit status message")
                except Exception as e:
                    log.warning(f"Failed to edit status message: {e}")
            else:
                sent = await status_channel.send(content=f"```{status_box}```")
                status_message_id = sent.id

            # persist status_message_id
            state["status_message_id"] = status_message_id
            with open(STATE_PATH, "w") as f:
                json.dump(state, f)
        except Exception as e:
            log.warning(f"Failed to send/update status message: {e}")

        # update last_online_set
        last_online_set.clear()
        last_online_set.update(current_online_set)

    except Exception as e:
        log.exception(f"Error in update_status_message loop: {e}")

# ------------------ Auto restart task ------------------
@tasks.loop(seconds=1)
async def auto_restart_check():
    try:
        now = now_wib()
        uptime = now - START_TIME
        elapsed = int(uptime.total_seconds())
        # 5 minutes warning
        if RESTART_INTERVAL - elapsed <= 300 and RESTART_INTERVAL - elapsed > 295:
            ch = bot.get_channel(NOTIF_CHANNEL_ID)
            if ch:
                await ch.send("⚠️ Bot akan restart otomatis dalam 5 menit!")
        if elapsed >= RESTART_INTERVAL:
            ch = bot.get_channel(NOTIF_CHANNEL_ID)
            if ch:
                await ch.send("♻️ Bot sedang restart otomatis... Bot akan kembali aktif dalam ~1 menit.")
            await asyncio.sleep(1)
            log.info("Performing automatic restart (execv)")
            os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        log.exception(f"Error in auto_restart_check: {e}")

# ------------------ Commands ------------------
@bot.command(name="add")
@commands.has_permissions(administrator=True)
async def cmd_add(ctx, user_id: int):
    if user_id in manual_tracked:
        return await ctx.send("⚠️ User sudah ada di daftar.")
    manual_tracked.append(user_id)
    with open(PLAYERS_PATH, "w") as f:
        json.dump(manual_tracked, f, indent=2)
    await ctx.send(f"✅ User `{user_id}` ditambahkan ke daftar pantauan.")

@bot.command(name="hapus")
@commands.has_permissions(administrator=True)
async def cmd_hapus(ctx, user_id: int):
    try:
        manual_tracked.remove(user_id)
        with open(PLAYERS_PATH, "w") as f:
            json.dump(manual_tracked, f, indent=2)
        await ctx.send(f"🗑️ User `{user_id}` dihapus dari daftar pantauan.")
    except ValueError:
        await ctx.send("⚠️ User tidak ditemukan di daftar.")

@bot.command(name="resetbot")
@commands.has_permissions(administrator=True)
async def cmd_resetbot(ctx):
    author = ctx.author
    await ctx.send(f"⚠️ {author.mention} memerintahkan restart bot. Restart akan dilakukan dalam 5 detik...")
    await asyncio.sleep(5)
    ch = bot.get_channel(NOTIF_CHANNEL_ID)
    if ch:
        await ch.send(f"🔄 Bot diminta restart oleh {author} — Bot akan kembali aktif dalam ~1 menit.")
    await asyncio.sleep(1)
    log.info(f"Manual restart by {author}")
    os.execv(sys.executable, [sys.executable] + sys.argv)

# ------------------ Events ------------------
@bot.event
async def on_ready():
    global http_session
    log.info(f"Logged in as {bot.user} (id: {bot.user.id})")
    http_session = aiohttp.ClientSession()
    # start tasks
    if not update_status_message.is_running():
        update_status_message.start()
    if not auto_restart_check.is_running():
        auto_restart_check.start()

@bot.event
async def on_disconnect():
    log.warning("Discord disconnected — will attempt to reconnect")

@bot.event
async def on_resumed():
    log.info("Resumed connection")

# ------------------ Graceful shutdown ------------------
async def close_http_session():
    global http_session
    if http_session:
        await http_session.close()

# ------------------ Run Bot ------------------
try:
    bot.run(DISCORD_TOKEN)
finally:
    try:
        asyncio.get_event_loop().run_until_complete(close_http_session())
    except Exception:
        pass
