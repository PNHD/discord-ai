import os
import asyncio
import aiohttp
import discord
import json
import base64

from collections import deque
from discord.ext import commands
from threading import Thread
from flask import Flask

app = Flask(__name__)


@app.route("/")
def home():
    return "Co Giao AI online"


def run_flask():
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))


BOT_ID = int(os.environ.get("BOT_ID", "1502278190788382770"))
N8N_URL = os.environ.get(
    "N8N_WEBHOOK_URL",
    "https://primary-production-5647d.up.railway.app/webhook/discord-ai",
)

# Attachment forwarding controls.
# n8n grading workflow still relies on image payloads being available as base64.
# Keep URL metadata, but also send bounded base64 data URLs so AI/image nodes can see the image.
MAX_B64_ATTACHMENTS = int(os.environ.get("MAX_B64_ATTACHMENTS", "6"))
MAX_ATTACHMENT_BYTES = int(os.environ.get("MAX_ATTACHMENT_BYTES", str(6 * 1024 * 1024)))
MAX_TOTAL_B64_BYTES = int(os.environ.get("MAX_TOTAL_B64_BYTES", str(18 * 1024 * 1024)))
ATTACHMENT_FETCH_TIMEOUT = int(os.environ.get("ATTACHMENT_FETCH_TIMEOUT", "12"))

PROCESSED = deque(maxlen=500)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True

bot = commands.Bot(command_prefix="!", intents=intents)


async def keep_typing(channel, stop):
    while not stop.is_set():
        try:
            await channel.trigger_typing()
        except Exception:
            pass

        try:
            await asyncio.wait_for(asyncio.shield(stop.wait()), timeout=8)
        except asyncio.TimeoutError:
            pass


def mentions_bot(message):
    if bot.user and bot.user in message.mentions:
        return True

    content = message.content or ""
    return f"<@{BOT_ID}>" in content or f"<@!{BOT_ID}>" in content


def replies_to_bot(message):
    ref = message.reference
    if ref and ref.resolved and isinstance(ref.resolved, discord.Message):
        return ref.resolved.author.id == BOT_ID
    return False


def should_forward(message):
    return mentions_bot(message) or replies_to_bot(message)


def serialize_attachments(attachments):
    out = []

    for a in attachments or []:
        out.append(
            {
                "id": str(getattr(a, "id", "")),
                "proxy_url": getattr(a, "proxy_url", None),
                "url": getattr(a, "url", None),
                "filename": getattr(a, "filename", None),
                "content_type": getattr(a, "content_type", None),
                "size": getattr(a, "size", None),
                "width": getattr(a, "width", None),
                "height": getattr(a, "height", None),
            }
        )

    return out


def is_image_attachment(attachment):
    content_type = (getattr(attachment, "content_type", None) or "").lower()
    filename = (getattr(attachment, "filename", None) or "").lower()

    if content_type.startswith("image/"):
        return True

    return filename.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif"))


async def attachment_to_b64(session, attachment, total_state):
    """Return a compact base64 payload for image attachments, or None if skipped."""
    if not is_image_attachment(attachment):
        return None

    size = int(getattr(attachment, "size", 0) or 0)
    if size > MAX_ATTACHMENT_BYTES:
        print(
            f"ATTACHMENT_B64_SKIP too_large filename={getattr(attachment, 'filename', '')} size={size}",
            flush=True,
        )
        return None

    if total_state["count"] >= MAX_B64_ATTACHMENTS:
        print("ATTACHMENT_B64_SKIP max_count", flush=True)
        return None

    if total_state["bytes"] + size > MAX_TOTAL_B64_BYTES:
        print(
            f"ATTACHMENT_B64_SKIP max_total current={total_state['bytes']} add={size}",
            flush=True,
        )
        return None

    url = getattr(attachment, "url", None) or getattr(attachment, "proxy_url", None)
    if not url:
        return None

    timeout = aiohttp.ClientTimeout(total=ATTACHMENT_FETCH_TIMEOUT)
    try:
        async with session.get(url, timeout=timeout) as resp:
            if resp.status != 200:
                print(f"ATTACHMENT_B64_FETCH_STATUS {resp.status} url={url}", flush=True)
                return None

            data = await resp.read()

    except Exception as exc:
        print(f"ATTACHMENT_B64_FETCH_ERROR {repr(exc)}", flush=True)
        return None

    if not data:
        return None

    if len(data) > MAX_ATTACHMENT_BYTES:
        print(
            f"ATTACHMENT_B64_SKIP downloaded_too_large filename={getattr(attachment, 'filename', '')} bytes={len(data)}",
            flush=True,
        )
        return None

    if total_state["bytes"] + len(data) > MAX_TOTAL_B64_BYTES:
        print(
            f"ATTACHMENT_B64_SKIP downloaded_max_total current={total_state['bytes']} add={len(data)}",
            flush=True,
        )
        return None

    content_type = getattr(attachment, "content_type", None) or "image/jpeg"
    b64 = base64.b64encode(data).decode("ascii")
    data_url = f"data:{content_type};base64,{b64}"

    total_state["count"] += 1
    total_state["bytes"] += len(data)

    print(
        f"ATTACHMENT_B64_OK filename={getattr(attachment, 'filename', '')} bytes={len(data)} total={total_state['bytes']}",
        flush=True,
    )

    return {
        "id": str(getattr(attachment, "id", "")),
        "filename": getattr(attachment, "filename", None),
        "content_type": content_type,
        "size": len(data),
        "url": getattr(attachment, "url", None),
        "proxy_url": getattr(attachment, "proxy_url", None),
        "data_url": data_url,
        "base64": b64,
    }


async def serialize_attachments_b64(attachments, total_state):
    """Fetch image attachments as base64, bounded by count/size limits."""
    out = []
    if not attachments:
        return out

    async with aiohttp.ClientSession() as session:
        for attachment in attachments:
            encoded = await attachment_to_b64(session, attachment, total_state)
            if encoded:
                out.append(encoded)

    return out


def get_activities(message):
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
    except Exception as exc:
        print(f"get_activities failed: {exc}", flush=True)
        return []


async def build_payload(message):
    ref_data = None
    ref = message.reference
    total_b64_state = {"count": 0, "bytes": 0}

    direct_attachments = serialize_attachments(message.attachments)
    direct_attachments_b64_meta = await serialize_attachments_b64(message.attachments, total_b64_state)
    direct_attachments_b64 = [x["data_url"] for x in direct_attachments_b64_meta]

    referenced_attachments_b64_meta = []
    referenced_attachments_b64 = []

    if ref and ref.resolved and isinstance(ref.resolved, discord.Message):
        ref_msg = ref.resolved
        ref_attachments = serialize_attachments(ref_msg.attachments)
        referenced_attachments_b64_meta = await serialize_attachments_b64(
            ref_msg.attachments, total_b64_state
        )
        referenced_attachments_b64 = [x["data_url"] for x in referenced_attachments_b64_meta]

        ref_data = {
            "id": str(ref_msg.id),
            "message_id": str(ref_msg.id),
            "author_id": str(ref_msg.author.id),
            "author_name": getattr(ref_msg.author, "display_name", str(ref_msg.author)),
            "content": ref_msg.content or "",
            "attachments": ref_attachments,
            "attachments_b64": referenced_attachments_b64,
            "attachments_b64_meta": referenced_attachments_b64_meta,
        }

    return {
        "body": {
            "body": {
                "content": message.content or "",
                "author": str(message.author.id),
                "author_id": str(message.author.id),
                "author_name": getattr(message.author, "display_name", str(message.author)),
                "channel_id": str(message.channel.id),
                "channel_name": getattr(message.channel, "name", "") or "",
                "guild_name": message.guild.name if message.guild else "",
                "message_id": str(message.id),
                # URL metadata for lightweight handling / logs.
                "attachments": direct_attachments,
                # Backward-compatible list of data URLs, used by older n8n expressions.
                "attachments_b64": direct_attachments_b64,
                # Rich metadata for newer n8n expressions.
                "attachments_b64_meta": direct_attachments_b64_meta,
                # Referenced image payloads for reply-based grading.
                "referenced_attachments_b64": referenced_attachments_b64,
                "referenced_attachments_b64_meta": referenced_attachments_b64_meta,
                "discord_activities": json.dumps(get_activities(message)),
                "referenced_message": ref_data,
            }
        }
    }


@bot.event
async def on_ready():
    print(f"Bot ready: {bot.user} ID={bot.user.id}", flush=True)
    print(f"N8N_URL={N8N_URL}", flush=True)
    print("Quiet mode: only bot mentions or replies to bot are forwarded to n8n", flush=True)
    print(
        "Attachment mode: URL + bounded base64 "
        f"count={MAX_B64_ATTACHMENTS} per_file={MAX_ATTACHMENT_BYTES} total={MAX_TOTAL_B64_BYTES}",
        flush=True,
    )


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    print(
        f"Seen channel={getattr(message.channel, 'name', '')} "
        f"id={message.channel.id} "
        f"author={message.author} "
        f"content={repr((message.content or '')[:100])} "
        f"attachments={len(message.attachments)}",
        flush=True,
    )

    if message.id in PROCESSED:
        print(f"skip duplicate message {message.id}", flush=True)
        return

    PROCESSED.append(message.id)

    if not should_forward(message):
        print("skip: no bot mention and not a reply to bot", flush=True)
        return

    stop = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing(message.channel, stop))

    try:
        payload = await build_payload(message)
        body = payload["body"]["body"]

        print("FORWARD_START", flush=True)
        print(f"ATTACHMENT_COUNT {len(body.get('attachments', []))}", flush=True)
        print(f"ATTACHMENT_B64_COUNT {len(body.get('attachments_b64', []))}", flush=True)
        print(f"REFERENCED_ATTACHMENT_B64_COUNT {len(body.get('referenced_attachments_b64', []))}", flush=True)
        print(
            f"POST n8n channel_id={body.get('channel_id')} message_id={body.get('message_id')}",
            flush=True,
        )

        timeout = aiohttp.ClientTimeout(total=45)

        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(N8N_URL, json=payload) as resp:
                text = await resp.text()
                print(f"POST_N8N_STATUS {resp.status}", flush=True)
                print(f"POST_N8N_RESPONSE {text[:500]}", flush=True)

                if resp.status != 200:
                    print(f"n8n error {resp.status}: {text[:500]}", flush=True)
                else:
                    print(f"n8n accepted: {text[:200]}", flush=True)

    except asyncio.TimeoutError:
        print("POST_N8N_TIMEOUT >45s", flush=True)

    except Exception as exc:
        print(f"bridge error: {repr(exc)}", flush=True)

    finally:
        stop.set()
        typing_task.cancel()

    await bot.process_commands(message)


if __name__ == "__main__":
    Thread(target=run_flask, daemon=True).start()
    bot.run(os.environ["DISCORD_TOKEN"])
