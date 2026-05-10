import os
import asyncio
import aiohttp
import discord
import base64
from collections import deque
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

# ── Cấu hình Bot ─────────────────────────────────────────────────────────────
BOT_ID = int(os.environ.get("BOT_ID", "1502278190788382770"))
N8N_WEBHOOK_URL = os.environ.get(
    "N8N_WEBHOOK_URL",
    "https://primary-production-5647d.up.railway.app/webhook/discord-ai"
)

# Channel whitelist — để trống = cho phép tất cả channel
# Để giới hạn: thêm env var ALLOWED_CHANNELS=123456789,987654321
_raw = os.environ.get("ALLOWED_CHANNELS", "")
ALLOWED_CHANNELS = set(int(c) for c in _raw.split(",") if c.strip()) if _raw else set()

# Dedup: nhớ 200 message ID gần nhất để chặn duplicate
_processed: deque = deque(maxlen=200)

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# ── Helpers ───────────────────────────────────────────────────────────────────
def is_relevant(message: discord.Message) -> bool:
    """Chỉ xử lý khi bot được mention hoặc reply vào tin của bot."""
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

async def download_images(attachments: list, session: aiohttp.ClientSession) -> list:
    """Download tất cả ảnh, trả về list base64. Bỏ qua ảnh lỗi."""
    results = []
    for a in attachments:
        try:
            async with session.get(
                a.proxy_url,
                timeout=aiohttp.ClientTimeout(total=15)
            ) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    results.append({
                        "filename": a.filename,
                        "data": base64.b64encode(data).decode("utf-8"),
                        "content_type": resp.content_type or "image/png"
                    })
                else:
                    print(f"⚠️ Download ảnh lỗi {resp.status}: {a.filename}")
        except Exception as e:
            print(f"⚠️ Bỏ qua ảnh {a.filename}: {e}")
    return results

async def build_payload(message: discord.Message, session: aiohttp.ClientSession) -> dict:
    """Build payload gửi lên n8n, kèm ảnh base64 nếu có."""
    ref_author_id = ""
    if (
        message.reference
        and message.reference.resolved
        and isinstance(message.reference.resolved, discord.Message)
    ):
        ref_author_id = str(message.reference.resolved.author.id)

    # Download ảnh ngay tại bot — tránh n8n phải gọi Discord CDN
    images = []
    if message.attachments:
        images = await download_images(message.attachments, session)

    return {
        "body": {
            "body": {
                "content": message.content,
                "author": str(message.author.id),
                "channel_id": str(message.channel.id),
                "images": images,           # base64, sẵn sàng cho AI agent
                "has_images": len(images) > 0,
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
    # Bỏ qua tin của bot
    if message.author.bot:
        return

    # Channel whitelist
    if ALLOWED_CHANNELS and message.channel.id not in ALLOWED_CHANNELS:
        return

    # Chặn duplicate
    if message.id in _processed:
        return
    _processed.append(message.id)

    # Chỉ forward nếu mention hoặc reply bot
    if not is_relevant(message):
        return

    try:
        async with message.channel.typing():
            async with aiohttp.ClientSession() as session:
                # Download ảnh + build payload trong cùng 1 session
                payload = await build_payload(message, session)
                async with session.post(
                    N8N_WEBHOOK_URL,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=60)
                ) as resp:
                    if resp.status != 200:
                        print(f"❌ n8n lỗi {resp.status}: {await resp.text()}")
    except asyncio.TimeoutError:
        print("⚠️ n8n timeout (>30s)")
    except aiohttp.ClientError as e:
        print(f"❌ Lỗi kết nối n8n: {e}")

    await bot.process_commands(message)

# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    flask_thread = Thread(target=run_flask, daemon=True)
    flask_thread.start()
    bot.run(os.environ["DISCORD_TOKEN"])
