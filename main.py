import os
import asyncio
import aiohttp
import discord
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

def build_payload(message: discord.Message, channel_id: str) -> dict:
    """Build payload gửi lên n8n. channel_id có thể là thread hoặc channel gốc."""
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
                # Gửi channel_id của thread (nếu có) để bot reply đúng chỗ
                "channel_id": channel_id,
                # Gửi toàn bộ attachments thay vì chỉ [0]
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

async def get_or_create_thread(message: discord.Message) -> str:
    """
    Tạo thread riêng cho cuộc hội thoại với bot.
    - Nếu đang ở TextChannel → tạo thread trên tin nhắn đó
    - Nếu đã ở trong thread → dùng luôn channel hiện tại
    - Trả về channel_id để n8n biết reply vào đâu
    """
    if isinstance(message.channel, discord.Thread):
        # Đã trong thread rồi, dùng luôn
        return str(message.channel.id)

    if isinstance(message.channel, discord.TextChannel):
        try:
            thread = await message.create_thread(
                name=f"🎓 {message.author.display_name}",
                auto_archive_duration=60  # tự archive sau 60 phút không hoạt động
            )
            return str(thread.id)
        except discord.Forbidden:
            # Bot không có quyền tạo thread → reply ở channel gốc
            print("⚠️ Không có quyền tạo thread, dùng channel gốc")
        except discord.HTTPException as e:
            print(f"⚠️ Lỗi tạo thread: {e}")

    return str(message.channel.id)

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

    # Tạo / lấy thread → lấy channel_id đúng để n8n reply vào đó
    channel_id = await get_or_create_thread(message)

    payload = build_payload(message, channel_id)

    try:
        # Typing indicator trong lúc chờ n8n xử lý
        async with message.channel.typing():
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    N8N_WEBHOOK_URL,
                    json=payload,
                    timeout=aiohttp.ClientTimeout(total=30)
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
