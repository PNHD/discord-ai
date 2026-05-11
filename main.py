import os, asyncio, aiohttp, discord, base64, json
from collections import deque, defaultdict
from discord.ext import commands
from threading import Thread
from flask import Flask
from datetime import datetime, timedelta

app = Flask(__name__)

@app.route('/')
def home():
    return "Cô Giáo AI đang online!"

@app.route('/health')
def health_check():
    uptime = (datetime.now() - health_monitor.start_time).total_seconds()
    return {
        "status": "ok",
        "uptime_seconds": int(uptime),
        "message_count": health_monitor.message_count,
        "error_count": health_monitor.error_count,
        "bot_id": BOT_ID,
        "discord_user": bot.user.name if bot.user else "NOT_READY",
    }, 200

def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)))

BOT_ID     = int(os.environ.get("BOT_ID", "1502278190788382770"))
N8N_URL    = os.environ.get("N8N_WEBHOOK_URL",
             "https://primary-production-5647d.up.railway.app/webhook/discord-ai")
_raw       = os.environ.get("ALLOWED_CHANNELS", "")
ALLOWED_CH = set(int(c) for c in _raw.split(",") if c.strip()) if _raw else set()
_processed = deque(maxlen=200)

# ── Configuration Constants ──────────────────────────────────────────────
ATTACHMENT_SIZE_LIMIT_MB = 10
TOTAL_SIZE_LIMIT_MB = 25
MAX_ATTACHMENTS = 5

intents = discord.Intents.default()
intents.message_content = True
intents.members   = True
intents.presences = True
bot = commands.Bot(command_prefix='!', intents=intents)

# ── Health Monitoring ────────────────────────────────────────────────────
class HealthMonitor:
    def __init__(self):
        self.start_time = datetime.now()
        self.message_count = 0
        self.error_count = 0
        self.last_message_time = None

health_monitor = HealthMonitor()

# ── Rate Limiting ────────────────────────────────────────────────────────
class RateLimiter:
    def __init__(self, limit: int = 5, window_seconds: int = 60):
        self.limit = limit
        self.window = timedelta(seconds=window_seconds)
        self.messages = defaultdict(list)  # user_id -> [timestamps]
    
    def is_allowed(self, user_id: int) -> bool:
        now = datetime.now()
        
        # Remove old entries
        self.messages[user_id] = [
            t for t in self.messages[user_id]
            if now - t < self.window
        ]
        
        # Check limit
        if len(self.messages[user_id]) >= self.limit:
            return False
        
        # Record this message
        self.messages[user_id].append(now)
        return True

rate_limiter = RateLimiter(limit=5, window_seconds=60)

# ── Typing Indicator ─────────────────────────────────────────────────────
async def keep_typing(channel, stop: asyncio.Event):
    while not stop.is_set():
        try:
            await channel.trigger_typing()
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=8)
        except asyncio.TimeoutError:
            pass

def is_relevant(message: discord.Message) -> bool:
    if bot.user in message.mentions:
        return True
    ref = message.reference
    if ref and ref.resolved and isinstance(ref.resolved, discord.Message):
        return ref.resolved.author.id == BOT_ID
    return False

def get_activities(message: discord.Message) -> list:
    member = message.guild.get_member(message.author.id) if message.guild else None
    if not member:
        return []
    
    activities = []
    for a in member.activities:
        activities.append({
            "name": a.name or "",
            "type": a.type.name or "unknown",
            "details": getattr(a, "details", None) or "",
            "state": getattr(a, "state", None) or "",
        })
    return activities

async def validate_attachments(attachments: list) -> tuple[bool, str]:
    """Check attachment count, individual size, and total size."""
    if not attachments:
        return True, ""
    
    if len(attachments) > MAX_ATTACHMENTS:
        return False, f"Tối đa {MAX_ATTACHMENTS} ảnh mỗi lần nhé!"
    
    total_mb = 0
    for att in attachments:
        size_mb = att.size / (1024 * 1024)
        
        if size_mb > ATTACHMENT_SIZE_LIMIT_MB:
            return False, f"Ảnh '{att.filename}' quá nặng ({size_mb:.1f}MB > {ATTACHMENT_SIZE_LIMIT_MB}MB)"
        
        total_mb += size_mb
    
    if total_mb > TOTAL_SIZE_LIMIT_MB:
        return False, f"Tổng ảnh quá nặng ({total_mb:.1f}MB)"
    
    return True, ""

async def download_single_attachment(session: aiohttp.ClientSession, att) -> dict | None:
    url = att.proxy_url
    if not url:
        return None
    try:
        async with session.get(
            url,
            timeout=aiohttp.ClientTimeout(total=15)
        ) as r:
            if r.status != 200:
                print(f"⚠️ Attachment {r.status}: {url[:60]}")
                return None
            raw  = await r.read()
            mime = r.headers.get("Content-Type", "image/png")
            name = att.filename or "screenshot.png"
            return {
                "data": base64.b64encode(raw).decode("utf-8"),
                "mime": mime,
                "name": name,
            }
    except Exception as e:
        print(f"⚠️ Download failed ({att.filename}): {e}")
        return None

async def download_attachments_b64(session, attachments):
    results = []
    for att in attachments:
        url = att.proxy_url
        print(f"DEBUG: Processing attachment URL: {url}")
        if not url:
            print(f"DEBUG: Skipping attachment, missing URL: {att}")
            continue
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                print(f"DEBUG: HTTP Status for {url}: {r.status}")
                if r.status != 200:
                    continue
                raw = await r.read()
                mime = r.headers.get("Content-Type", "image/png")
                name = att.filename or "screenshot.png"
                encoded_data = base64.b64encode(raw).decode("utf-8")
                results.append({
                    "data": encoded_data,
                    "mime": mime,
                    "name": name,
                })
                print(f"DEBUG: Downloaded and encoded {name}")
        except Exception as e:
            print(f"⚠️ Download failed ({att.filename}): {e}")
    print(f"DEBUG: Total attachments downloaded: {len(results)}")
    return results

async def build_payload(session: aiohttp.ClientSession,
                        message: discord.Message) -> dict:
    ref_author_id = ""
    ref = message.reference
    if ref and ref.resolved and isinstance(ref.resolved, discord.Message):
        ref_author_id = str(ref.resolved.author.id)

    # Download images in the bot — avoid n8n making external HTTP calls
    attachments_b64 = await download_attachments_b64(session, message.attachments)

    # ✅ FIXED: Add third level of "body" nesting
    return {
        "body": {
            "body": {
                "body": {  # ← FIXED: Added this level
                    "content": message.content,
                    "author": str(message.author.id),
                    "channel_id": str(message.channel.id),
                    # Keep URL list for reference / If-node check
                    "attachments": [{"proxy_url": a.proxy_url,
                                    "filename": a.filename}
                                   for a in message.attachments],
                    # Base64-encoded images — n8n decodes these directly
                    "attachments_b64": json.dumps(attachments_b64),
                    "discord_activities": json.dumps(get_activities(message)),
                    "referenced_message": {"author_id": ref_author_id} if ref_author_id else None,
                }
            }
        }
    }

@bot.event
async def on_ready():
    print(f"✅ {bot.user} (ID: {bot.user.id})")

@bot.event
async def on_message(message: discord.Message):
    health_monitor.message_count += 1
    health_monitor.last_message_time = datetime.now().isoformat()
    
    if message.author.bot:
        return
    if ALLOWED_CH and message.channel.id not in ALLOWED_CH:
        return
    if message.id in _processed:
        return
    _processed.append(message.id)
    
    # ✅ NEW: Rate limit check
    if not rate_limiter.is_allowed(message.author.id):
        try:
            await message.channel.send(
                "⏸️ Yên một tí, cô đang xử lý nhiều bài của con rồi. "
                "Đợi cô xong bài này mới ra bài tiếp nhé! 🌱"
            )
        except Exception as e:
            print(f"⚠️ Failed to send rate limit message: {e}")
        return
    
    # ✅ NEW: Validate attachments
    valid, reason = await validate_attachments(message.attachments)
    if not valid:
        try:
            await message.channel.send(f"❌ {reason}")
        except Exception as e:
            print(f"⚠️ Failed to send validation error: {e}")
        return
    
    if not is_relevant(message):
        return

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing(message.channel, stop_typing))

    try:
        async with aiohttp.ClientSession() as sess:
            payload = await build_payload(sess, message)
            async with sess.post(
                N8N_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=120)
            ) as r:
                if r.status != 200:
                    response_text = await r.text()
                    print(f"❌ n8n {r.status}: {response_text}")
                    health_monitor.error_count += 1
                    
                    # ✅ NEW: User feedback for errors
                    try:
                        await message.channel.send(
                            "❌ Lỗi kết nối với cô giáo. Ba Đăng vui lòng check workflow! "
                            f"(Error: n8n {r.status})"
                        )
                    except Exception as e:
                        print(f"⚠️ Failed to send error message: {e}")
                    return
                
                # Success — no need to send anything, n8n already sent reply
                print(f"✅ n8n processed message from {message.author}")
                
    except asyncio.TimeoutError:
        print("⚠️ n8n timeout >120s")
        health_monitor.error_count += 1
        
        # ✅ NEW: User feedback for timeout
        try:
            await message.channel.send(
                "⏱️ Cô giáo đang suy nghĩ quá lâu... thử lại sau nhé! 🤔"
            )
        except Exception as e:
            print(f"⚠️ Failed to send timeout message: {e}")
            
    except aiohttp.ClientError as e:
        print(f"❌ n8n error: {e}")
        health_monitor.error_count += 1
        
        # ✅ NEW: User feedback for network errors
        try:
            await message.channel.send(
                f"❌ Lỗi mạng: cô không kết nối được. Ba Đăng vui lòng check!"
            )
        except Exception as e:
            print(f"⚠️ Failed to send network error message: {e}")
            
    except Exception as e:
        print(f"❌ Unexpected error: {e}")
        health_monitor.error_count += 1
        
        # ✅ NEW: Catch unexpected errors
        try:
            await message.channel.send(
                f"❌ Lỗi không mong muốn. Ba Đăng vui lòng check!"
            )
        except:
            pass
            
    finally:
        stop_typing.set()
        try:
            typing_task.cancel()
        except:
            pass

    await bot.process_commands(message)

if __name__ == "__main__":
    Thread(target=run_flask, daemon=True).start()
    bot.run(os.environ["DISCORD_TOKEN"])
