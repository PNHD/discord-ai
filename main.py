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
    print("⚠️  Pillow not installed — images sent uncompressed", flush=True)

app = Flask(__name__)

@app.route("/")
def home():
    return "Cô Giáo AI đang online!"

def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

BOT_ID = int(os.environ.get("BOT_ID", "1502278190788382770"))
N8N_URL = os.environ.get(
    "N8N_WEBHOOK_URL",
    "https://primary-production-5647d.up.railway.app/webhook/discord-ai",
)

# Optional hard whitelist. Leave empty to allow all channels to be considered.
_raw_allowed = os.environ.get("ALLOWED_CHANNELS", "")
ALLOWED_CH = set()
for c in _raw_allowed.split(","):
    c = c.strip()
    if c.isdigit():
        ALLOWED_CH.add(int(c))

# Channels where messages are auto-forwarded even without mentioning/replying to the bot.
# You may override in Railway Variables:
# AUTO_CHANNEL_NAMES=test,chat-chung,dứa,di,whis
_raw_auto_names = os.environ.get("AUTO_CHANNEL_NAMES", "test,chat-chung,dứa,di,whis")
AUTO_CHANNEL_NAMES = {x.strip().lower() for x in _raw_auto_names.split(",") if x.strip()}

# Optional safer ID-based auto channels. Recommended when channel names contain emoji/unicode.
# AUTO_CHANNEL_IDS=1501929563339624519,1498510367284793405,...
_raw_auto_ids = os.environ.get("AUTO_CHANNEL_IDS", "")
AUTO_CHANNEL_IDS = set()
for c in _raw_auto_ids.split(","):
    c = c.strip()
    if c.isdigit():
        AUTO_CHANNEL_IDS.add(int(c))

_processed = deque(maxlen=500)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True
bot = commands.Bot(command_prefix="!", intents=intents)


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


def _channel_name(message: discord.Message) -> str:
    return str(getattr(message.channel, "name", "") or "").strip().lower()


def is_relevant(message: discord.Message) -> bool:
    # Study channels: auto-forward text/image messages without mention.
    if message.channel.id in AUTO_CHANNEL_IDS:
        return True

    if _channel_name(message) in AUTO_CHANNEL_NAMES:
        return True

    # Mention bot.
    if bot.user and bot.user in message.mentions:
        return True

    # Reply to bot.
    ref = message.reference
    if ref and ref.resolved and isinstance(ref.resolved, discord.Message):
        return ref.resolved.author.id == BOT_ID

    # If a message has attachments and is in a permitted channel, allow it.
    # This protects "send image + chấm bài" cases even if content is empty.
    if message.attachments and (not ALLOWED_CH or message.channel.id in ALLOWED_CH):
        return True

    return False


def get_activities(message: discord.Message) -> list:
    try:
        member = message.guild.get_member(message.author.id) if message.guild else None
        if not member:
            return []
        return [
            {
                "name": a.name,
                "type": a.type.name,
                "details": getattr(a, "details", None),
                "state": getattr(a, "state", None),
            }
            for a in member.activities
        ]
    except Exception as e:
        print(f"⚠️ get_activities failed: {e}", flush=True)
        return []


def compress_image(raw: bytes, max_px: int = 1280, quality: int = 75) -> tuple[bytes, str]:
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
        ratio_pct = int(len(compressed) / max(len(raw), 1) * 100)
        print(f"  Compressed {len(raw)//1024}KB → {len(compressed)//1024}KB ({ratio_pct}%)", flush=True)
        return compressed, "image/jpeg"
    except Exception as e:
        print(f"  Compress failed: {e}, sending original", flush=True)
        return raw, "image/png"


async def download_attachments_b64(session: aiohttp.ClientSession, attachments: list) -> list:
    results = []
    for att in attachments:
        # proxy_url sometimes fails/stales; url is often safer.
        url = getattr(att, "url", None) or getattr(att, "proxy_url", None)
        if not url:
            print(f"⚠️ Attachment has no URL: {getattr(att, 'filename', '')}", flush=True)
            continue

        filename = getattr(att, "filename", None) or "screenshot.png"
        content_type = (getattr(att, "content_type", "") or "").lower()

        # Skip non-image attachments for now, but log them.
        if content_type and not content_type.startswith("image/"):
            print(f"⚠️ Skip non-image attachment: {filename} ({content_type})", flush=True)
            continue

        try:
            timeout = aiohttp.ClientTimeout(total=30)
            async with session.get(url, timeout=timeout) as r:
                if r.status != 200:
                    print(f"⚠️ Download HTTP {r.status}: {filename} {url[:80]}", flush=True)
                    continue

                raw = await r.read()
                print(f"✓ Downloaded {filename}: {len(raw)//1024}KB", flush=True)

                compressed, mime = await asyncio.get_event_loop().run_in_executor(
                    None, compress_image, raw
                )
                out_name = filename.rsplit(".", 1)[0] + ".jpg" if mime == "image/jpeg" else filename
                results.append(
                    {
                        "data": base64.b64encode(compressed).decode("utf-8"),
                        "mime": mime,
                        "name": out_name,
                    }
                )
        except Exception as e:
            print(f"⚠️ Download failed ({filename}): {repr(e)}", flush=True)

    return results


async def build_payload(session: aiohttp.ClientSession, message: discord.Message) -> dict:
    referenced_message = None
    ref = message.reference
    if ref and ref.resolved and isinstance(ref.resolved, discord.Message):
        ref_msg = ref.resolved
        referenced_message = {
            "id": str(ref_msg.id),
            "message_id": str(ref_msg.id),
            "author_id": str(ref_msg.author.id),
            "author_name": getattr(ref_msg.author, "display_name", str(ref_msg.author)),
            "content": ref_msg.content or "",
            "attachments": [
                {"proxy_url": a.proxy_url, "url": a.url, "filename": a.filename}
                for a in ref_msg.attachments
            ],
        }

    attachments_b64 = await download_attachments_b64(session, message.attachments)

    return {
        "body": {
            "body": {
                "content": message.content or "",
                "author": str(message.author.id),
                "author_name": getattr(message.author, "display_name", str(message.author)),
                "channel_id": str(message.channel.id),
                "channel_name": getattr(message.channel, "name", "") or "",
                "guild_name": message.guild.name if message.guild else "",
                "message_id": str(message.id),
                "attachments": [
                    {"proxy_url": a.proxy_url, "url": a.url, "filename": a.filename}
                    for a in message.attachments
                ],
                "attachments_b64": json.dumps(attachments_b64),
                "discord_activities": json.dumps(get_activities(message)),
                "referenced_message": referenced_message,
            }
        }
    }


@bot.event
async def on_ready():
    print(f"✅ {bot.user} (ID: {bot.user.id})", flush=True)
    print(f"   Pillow available: {HAS_PIL}", flush=True)
    print(f"   N8N_URL: {N8N_URL}", flush=True)
    print(f"   ALLOWED_CH: {sorted(ALLOWED_CH) if ALLOWED_CH else 'ALL'}", flush=True)
    print(f"   AUTO_CHANNEL_NAMES: {sorted(AUTO_CHANNEL_NAMES)}", flush=True)
    print(f"   AUTO_CHANNEL_IDS: {sorted(AUTO_CHANNEL_IDS) if AUTO_CHANNEL_IDS else 'none'}", flush=True)


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    print(
        f"👀 Seen [{getattr(message.channel, 'name', '')}] "
        f"{message.channel.id} {message.author}: "
        f"content={repr((message.content or '')[:100])} attachments={len(message.attachments)}",
        flush=True,
    )

    if ALLOWED_CH and message.channel.id not in ALLOWED_CH:
        print(f"⏭️ Skip channel not allowed: {getattr(message.channel, 'name', '')} {message.channel.id}", flush=True)
        return

    if message.id in _processed:
        print(f"⏭️ Skip duplicate message: {message.id}", flush=True)
        return
    _processed.append(message.id)

    if not is_relevant(message):
        print(
            f"⏭️ Skip not relevant: channel={getattr(message.channel, 'name', '')} "
            f"id={message.channel.id}",
            flush=True,
        )
        return

    print(
        f"📨 Forward [{getattr(message.channel, 'name', '')}] "
        f"{message.author}: {(message.content or '')[:80]} | attachments={len(message.attachments)}",
        flush=True,
    )

    stop_typing = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing(message.channel, stop_typing))

    try:
        async with aiohttp.ClientSession() as sess:
            payload = await build_payload(sess, message)
            print(
                f"➡️ POST n8n: channel_id={payload['body']['body']['channel_id']} "
                f"content_len={len(payload['body']['body']['content'])} "
                f"images_b64_len={len(json.loads(payload['body']['body']['attachments_b64']))}",
                flush=True,
            )
            async with sess.post(
                N8N_URL,
                json=payload,
                timeout=aiohttp.ClientTimeout(total=180),
            ) as r:
                body = await r.text()
                if r.status != 200:
                    print(f"❌ n8n {r.status}: {body[:500]}", flush=True)
                else:
                    print(f"✅ n8n accepted: {body[:200]}", flush=True)
    except asyncio.TimeoutError:
        print("⚠️ n8n timeout >180s", flush=True)
    except aiohttp.ClientError as e:
        print(f"❌ n8n error: {repr(e)}", flush=True)
    except Exception as e:
        print(f"❌ Unexpected bridge error: {repr(e)}", flush=True)
    finally:
        stop_typing.set()
        typing_task.cancel()

    await bot.process_commands(message)


if __name__ == "__main__":
    Thread(target=run_flask, daemon=True).start()
    bot.run(os.environ["DISCORD_TOKEN"])
