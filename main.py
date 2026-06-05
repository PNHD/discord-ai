import os
import asyncio
import aiohttp
import discord
import json
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
    """Forward attachment metadata + CDN URLs only. No fetching, no base64."""
    out = []
    for a in attachments or []:
        try:
            out.append({
                "id": str(getattr(a, "id", "")),
                "filename": getattr(a, "filename", None),
                "content_type": getattr(a, "content_type", None),
                "url": getattr(a, "url", None),
                "proxy_url": getattr(a, "proxy_url", None),
                "size": getattr(a, "size", None),
                "width": getattr(a, "width", None),
                "height": getattr(a, "height", None),
            })
        except Exception as exc:
            print(f"ATTACHMENT_SERIALIZE_ERROR {repr(exc)}", flush=True)
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


def build_payload(message):
    """Build n8n webhook payload. Synchronous — no network calls."""
    try:
        direct_attachments = serialize_attachments(message.attachments)
    except Exception as exc:
        print(f"ATTACHMENT_BUILD_ERROR {repr(exc)}", flush=True)
        direct_attachments = []

    ref_data = None
    try:
        ref = message.reference
        if ref and ref.resolved and isinstance(ref.resolved, discord.Message):
            ref_msg = ref.resolved
            ref_attachments = serialize_attachments(ref_msg.attachments)
            ref_data = {
                "id": str(ref_msg.id),
                "message_id": str(ref_msg.id),
                "author_id": str(ref_msg.author.id),
                "author_name": getattr(ref_msg.author, "display_name", str(ref_msg.author)),
                "content": ref_msg.content or "",
                "attachments": ref_attachments,
                # n8n Fetch Attachments B64 node will fetch these from CDN
                "attachments_b64": [],
                "attachments_b64_meta": [],
            }
    except Exception as exc:
        print(f"REF_BUILD_ERROR {repr(exc)}", flush=True)

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
                # Attachment metadata with CDN URLs — n8n fetches bytes itself
                "attachments": direct_attachments,
                # Always empty — n8n Fetch Attachments B64 node handles this
                "attachments_b64": [],
                "attachments_b64_meta": [],
                "referenced_attachments_b64": [],
                "referenced_attachments_b64_meta": [],
                "discord_activities": json.dumps(get_activities(message)),
                "referenced_message": ref_data,
            }
        }
    }


@bot.event
async def on_ready():
    print(f"Bot ready: {bot.user} ID={bot.user.id}", flush=True)
    print(f"N8N_URL configured (value hidden)", flush=True)
    print("Mode: forward metadata+URLs only, no image fetch in Worker", flush=True)


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
        payload = build_payload(message)  # sync — no await, no network
        body = payload["body"]["body"]

        print("FORWARD_START", flush=True)
        print(f"ATTACHMENT_COUNT {len(body.get('attachments', []))}", flush=True)
        print(
            f"POST n8n channel_id={body.get('channel_id')} "
            f"message_id={body.get('message_id')} "
            f"attachments={len(body.get('attachments', []))}",
            flush=True,
        )

        timeout = aiohttp.ClientTimeout(total=45)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(N8N_URL, json=payload) as resp:
                text = await resp.text()
                print(f"POST_N8N_STATUS {resp.status}", flush=True)
                if resp.status != 200:
                    print(f"n8n error {resp.status}: {text[:300]}", flush=True)
                else:
                    print(f"n8n accepted", flush=True)

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
