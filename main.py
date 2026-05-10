import os
import asyncio
import aiohttp
import discord
from discord.ext import commands
from threading import Thread
from flask import Flask

# ── Web server mini để Railway giữ process alive ──────────────────────────────
app = Flask(__name__)

@app.route('/')
def home():
    return "Cô Giáo AI đang online!"

def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)))

# ── Cấu hình Discord Bot ──────────────────────────────────────────────────────
BOT_ID = int(os.environ.get("BOT_ID", "1502278190788382770"))
N8N_WEBHOOK_URL = os.environ.get(
    "N8N_WEBHOOK_URL",
    "https://primary-production-5647d.up.railway.app/webhook/discord-ai"
)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# ── Helpers ───────────────────────────────────────────────────────────────────
def is_relevant(message: discord.Message) -> bool:
    """Chỉ xử lý khi bot được mention hoặc reply vào tin của bot."""
    # Mention bot trong nội dung
    if bot.user in message.mentions:
        return True
    # Reply vào tin của bot
    if (
        message.reference
        and message.reference.resolved
        and isinstance(message.reference.resolved, discord.Message)
        and message.reference.resolved.author.id == BOT_ID
    ):
        return True
    return False

def build_payload(message: discord.Message) -> dict:
    """Build payload gửi lên n8n."""
    # Referenced message author (để n8n verify reply-to-bot)
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

# ── Events ────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"✅ Đã đăng nhập: {bot.user} (ID: {bot.user.id})")

@bot.event
async def on_message(message: discord.Message):
    # Bỏ qua tin của chính bot
    if message.author.bot:
        return

    # Lọc sớm: chỉ forward nếu relevant
    if not is_relevant(message):
        return

    payload = build_payload(message)

    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                N8N_WEBHOOK_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    print(f"❌ n8n lỗi {resp.status}: {await resp.text()}")
    except asyncio.TimeoutError:
        print("⚠️ n8n timeout (>10s)")
    except aiohttp.ClientError as e:
        print(f"❌ Lỗi kết nối n8n: {e}")

    await bot.process_commands(message)

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    bot.run(os.environ["DISCORD_TOKEN"])
