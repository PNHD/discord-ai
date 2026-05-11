import os, asyncio, aiohttp, discord, base64
from collections import deque
from discord.ext import commands
from threading import Thread
from flask import Flask

app = Flask(__name__)

@app.route('/')
def home():
    return "Cô Giáo AI đang online! 🌱"

def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)))

# --- Cấu hình từ Environment ---
BOT_ID     = int(os.environ.get("BOT_ID", "1502278190788382770"))
N8N_URL    = os.environ.get("N8N_WEBHOOK_URL", "https://primary-production-5647d.up.railway.app/webhook/discord-ai")
_raw       = os.environ.get("ALLOWED_CHANNELS", "")
ALLOWED_CH = set(int(c) for c in _raw.split(",") if c.strip()) if _raw else set()
_processed = deque(maxlen=200)

intents = discord.Intents.default()
intents.message_content = True
intents.members   = True 
intents.presences = True
bot = commands.Bot(command_prefix='!', intents=intents)

async def keep_typing(channel, stop: asyncio.Event):
    while not stop.is_set():
        try:
            await channel.trigger_typing()
        except Exception: pass
        try:
            await asyncio.wait_for(asyncio.shield(stop.wait()), timeout=8)
        except asyncio.TimeoutError: pass

def is_relevant(message: discord.Message) -> bool:
    if bot.user in message.mentions: return True
    ref = message.reference
    if ref and ref.resolved and isinstance(ref.resolved, discord.Message):
        return ref.resolved.author.id == BOT_ID
    return False

async def download_attachments_b64(session: aiohttp.ClientSession, attachments: list) -> list:
    results = []
    for att in attachments:
        if not att.proxy_url: continue
        try:
            async with session.get(att.proxy_url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status == 200:
                    raw = await r.read()
                    results.append({
                        "data": base64.b64encode(raw).decode("utf-8"),
                        "mime": r.headers.get("Content-Type", "image/png"),
                        "name": att.filename or "image.png",
                    })
        except Exception as e: print(f"⚠️ Lỗi tải ảnh: {e}")
    return results

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or message.id in _processed: return
    if ALLOWED_CH and message.channel.id not in ALLOWED_CH: return
    if not is_relevant(message): return

    _processed.append(message.id)
    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing(message.channel, stop_typing))

    try:
        async with aiohttp.ClientSession() as sess:
            # Thu thập dữ liệu
            ref_author_id = str(message.reference.resolved.author.id) if (message.reference and message.reference.resolved) else ""
            att_b64 = await download_attachments_b64(sess, message.attachments)
            
            payload = {
                "body": {
                    "body": {
                        "content": message.content,
                        "author": str(message.author.id),
                        "channel_id": str(message.channel.id),
                        "attachments_b64": att_b64,
                        "referenced_message": {"author_id": ref_author_id} if ref_author_id else None
                    }
                }
            }
            async with sess.post(N8N_URL, json=payload, timeout=aiohttp.ClientTimeout(total=120)) as r:
                if r.status != 200: print(f"❌ n8n Error {r.status}")
    finally:
        stop_typing.set()
        typing_task.cancel()

if __name__ == "__main__":
    Thread(target=run_flask, daemon=True).start()
    bot.run(os.environ["DISCORD_TOKEN"])
