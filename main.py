import os
import asyncio
import aiohttp
import discord
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

BOT_ID = int(os.environ.get("BOT_ID", "1502278190788382770"))
N8N_WEBHOOK_URL = os.environ.get(
    "N8N_WEBHOOK_URL",
    "https://primary-production-5647d.up.railway.app/webhook/discord-ai"
)

_raw = os.environ.get("ALLOWED_CHANNELS", "")
ALLOWED_CHANNELS = set(int(c) for c in _raw.split(",") if c.strip()) if _raw else set()

_processed: deque = deque(maxlen=200)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

def is_relevant(message: discord.Message) -> bool:
    if bot.user in message.mentions:
        return True
    if (
        message.reference
        and message.reference.resolved
        and isinstance(message.reference.resolved, discord.Message)
        and message.reference.resolved.author.id == BOT_ID
    ):
        return True
    return False

def build_payload(message: discord.Message) -> dict:
    ref_author_id = ""
    if (
        message.reference
        and message.reference.resolved
        and isinstance(message.reference.resolved, discord.Message)
    ):
        ref_author_id = str(message.reference.resolved.author.id)

    return {
        "body": {
            "body": {
                "content": message.content,
                "author": str(message.author.id),
                "channel_id": str(message.channel.id),
                "attachments": [
                    {"proxy_url": a.proxy_url, "filename": a.filename}
                    for a in message.attachments
                ],
                "referenced_message": {
                    "author_id": ref_author_id
                } if ref_author_id else None
            }
        }
    }

@bot.event
async def on_ready():
    print(f"✅ Đã đăng nhập: {bot.user} (ID: {bot.user.id})")

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if ALLOWED_CHANNELS and message.channel.id not in ALLOWED_CHANNELS:
        return
    if message.id in _processed:
        return
    _processed.append(message.id)
    if not is_relevant(message):
        return

    try:
        async with message.channel.typing():
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    N8N_WEBHOOK_URL,
                    json=build_payload(message),
                    timeout=aiohttp.ClientTimeout(total=30)
                ) as resp:
                    if resp.status != 200:
                        print(f"❌ n8n lỗi {resp.status}: {await resp.text()}")
    except asyncio.TimeoutError:
        print("⚠️ n8n timeout (>30s)")
    except aiohttp.ClientError as e:
        print(f"❌ Lỗi kết nối n8n: {e}")

    await bot.process_commands(message)

if __name__ == "__main__":
    Thread(target=run_flask, daemon=True).start()
    bot.run(os.environ["DISCORD_TOKEN"])
