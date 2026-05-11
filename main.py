import os, asyncio, aiohttp, discord
from collections import deque
from discord.ext import commands
from threading import Thread
from flask import Flask

app = Flask(__name__)

@app.route('/')
def home():
    return "Cô Giáo AI đang online!"

def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)))

BOT_ID     = int(os.environ.get("BOT_ID", "1502278190788382770"))
N8N_URL    = os.environ.get("N8N_WEBHOOK_URL",
             "https://primary-production-5647d.up.railway.app/webhook/discord-ai")
_raw       = os.environ.get("ALLOWED_CHANNELS", "")
ALLOWED_CH = set(int(c) for c in _raw.split(",") if c.strip()) if _raw else set()
_processed = deque(maxlen=200)

intents = discord.Intents.default()
intents.message_content = True
intents.members   = True
intents.presences = True
bot = commands.Bot(command_prefix='!', intents=intents)

# ── Typing indicator liên tục (refresh mỗi 8s) ───────────────────────────────
async def keep_typing(channel, stop: asyncio.Event):
    while not stop.is_set():
        try:
            await channel.trigger_typing()
        except Exception:
            pass
        try:
            await asyncio.wait_for(asyncio.shield(stop.wait()), timeout=8)
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
    return [{"name": a.name, "type": a.type.name,
             "details": getattr(a,"details",None),
             "state":   getattr(a,"state",None)}
            for a in member.activities]

def build_payload(message: discord.Message) -> dict:
    ref_author_id = ""
    ref = message.reference
    if ref and ref.resolved and isinstance(ref.resolved, discord.Message):
        ref_author_id = str(ref.resolved.author.id)

    return {
        "body": {
            "body": {
                "content"          : message.content,
                "author"           : str(message.author.id),
                "channel_id"       : str(message.channel.id),
                # Gửi URL ngay — n8n tự download (nhanh hơn download trong bot)
                "attachments"      : [{"proxy_url": a.proxy_url,
                                       "filename":  a.filename}
                                      for a in message.attachments],
                "discord_activities": get_activities(message),
                "referenced_message": {"author_id": ref_author_id} if ref_author_id else None,
            }
        }
    }

@bot.event
async def on_ready():
    print(f"✅ {bot.user} (ID: {bot.user.id})")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if ALLOWED_CH and message.channel.id not in ALLOWED_CH:
        return
    if message.id in _processed:
        return
    _processed.append(message.id)
    if not is_relevant(message):
        return

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing(message.channel, stop_typing))

    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.post(N8N_URL,
                                 json=build_payload(message),
                                 timeout=aiohttp.ClientTimeout(total=120)) as r:
                if r.status != 200:
                    print(f"❌ n8n {r.status}: {await r.text()}")
    except asyncio.TimeoutError:
        print("⚠️ n8n timeout >120s")
    except aiohttp.ClientError as e:
        print(f"❌ n8n error: {e}")
    finally:
        stop_typing.set()
        typing_task.cancel()

    await bot.process_commands(message)

if __name__ == "__main__":
    Thread(target=run_flask, daemon=True).start()
    bot.run(os.environ["DISCORD_TOKEN"])
