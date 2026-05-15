import os, asyncio, aiohttp, discord, base64, json, io
from collections import deque
from discord.ext import commands
from threading import Thread
from flask import Flask

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    print("⚠️  Pillow not installed — images sent uncompressed")

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


# ── Typing indicator liên tục (refresh mỗi 8s) ──────────────────────────────
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
             "details": getattr(a, "details", None),
             "state":   getattr(a, "state",   None)}
            for a in member.activities]


def compress_image(raw: bytes, max_px: int = 1280, quality: int = 75) -> tuple[bytes, str]:
    """
    Compress image to JPEG, resize so longest side ≤ max_px.
    Returns (compressed_bytes, mime_type).
    Falls back to original bytes if Pillow not available.
    """
    if not HAS_PIL:
        return raw, "image/png"
    try:
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        w, h = img.size
        if max(w, h) > max_px:
            ratio = max_px / max(w, h)
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        compressed = buf.getvalue()
        ratio_pct = int(len(compressed) / len(raw) * 100)
        print(f"  Compressed {len(raw)//1024}KB → {len(compressed)//1024}KB ({ratio_pct}%)")
        return compressed, "image/jpeg"
    except Exception as e:
        print(f"  Compress failed: {e}, sending original")
        return raw, "image/png"


async def download_attachments_b64(session: aiohttp.ClientSession,
                                   attachments: list) -> list:
    """Download, compress, and base64-encode each image attachment."""
    results = []
    for att in attachments:
        url = att.proxy_url
        if not url:
            continue
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=15)) as r:
                if r.status != 200:
                    print(f"⚠️ {r.status}: {url[:60]}")
                    continue
                raw  = await r.read()
                name = att.filename or "screenshot.png"
                print(f"✓ Downloaded {name}: {len(raw)//1024}KB")
                compressed, mime = await asyncio.get_event_loop().run_in_executor(
                    None, compress_image, raw
                )
                results.append({
                    "data": base64.b64encode(compressed).decode("utf-8"),
                    "mime": mime,
                    "name": name.rsplit(".", 1)[0] + ".jpg" if mime == "image/jpeg" else name,
                })
        except Exception as e:
            print(f"⚠️ Download failed ({att.filename}): {e}")
    return results


async def build_payload(session: aiohttp.ClientSession,
                        message: discord.Message) -> dict:
    ref_author_id = ""
    ref = message.reference
    if ref and ref.resolved and isinstance(ref.resolved, discord.Message):
        ref_author_id = str(ref.resolved.author.id)

    attachments_b64 = await download_attachments_b64(session, message.attachments)

    return {
        "body": {
            "body": {
                "content"           : message.content,
                "author"            : str(message.author.id),
                "author_name"       : message.author.display_name,
                "channel_id"        : str(message.channel.id),
                "channel_name"      : message.channel.name if hasattr(message.channel, 'name') else "",
                "guild_name"        : message.guild.name if message.guild else "",
                "message_id"        : str(message.id),
                "attachments"       : [{"proxy_url": a.proxy_url, "filename": a.filename}
                                       for a in message.attachments],
                # JSON string → n8n stores as string → JSON.parse works correctly
                "attachments_b64"   : json.dumps(attachments_b64),
                "discord_activities": json.dumps(get_activities(message)),
                "referenced_message": {"author_id": ref_author_id} if ref_author_id else None,
            }
        }
    }


@bot.event
async def on_ready():
    print(f"✅ {bot.user} (ID: {bot.user.id})")
    print(f"   Pillow available: {HAS_PIL}")


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

    print(f"📨 [{message.channel}] {message.author}: {message.content[:80]}"
          f" | attachments={len(message.attachments)}")

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing(message.channel, stop_typing))

    try:
        async with aiohttp.ClientSession() as sess:
            payload = await build_payload(sess, message)
            async with sess.post(N8N_URL, json=payload,
                                 timeout=aiohttp.ClientTimeout(total=120)) as r:
                if r.status != 200:
                    body = await r.text()
                    print(f"❌ n8n {r.status}: {body[:200]}")
                else:
                    print(f"✅ n8n accepted")
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
