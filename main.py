import os
import asyncio
import base64
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


# ── config ────────────────────────────────────────────────────────────────────
BOT_ID = int(os.environ.get("BOT_ID", "1502278190788382770"))
N8N_URL = os.environ.get(
    "N8N_WEBHOOK_URL",
    "https://primary-production-5647d.up.railway.app/webhook/discord-ai",
)

MAX_B64_ATTACHMENTS    = 3
MAX_ATTACHMENT_BYTES   = 3 * 1024 * 1024   # 3 MB per image
MAX_TOTAL_B64_BYTES    = 8 * 1024 * 1024   # 8 MB total across all images
ATTACHMENT_FETCH_TIMEOUT = 5               # seconds per image

PROCESSED = deque(maxlen=500)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True
bot = commands.Bot(command_prefix="!", intents=intents)


# ── typing indicator ──────────────────────────────────────────────────────────
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


# ── routing helpers ───────────────────────────────────────────────────────────
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


# ── attachment metadata (no bytes) ────────────────────────────────────────────
def serialize_attachments(attachments):
    """Return list of attachment metadata dicts (CDN URLs, no fetching)."""
    out = []
    for a in attachments or []:
        try:
            out.append({
                "id":           str(getattr(a, "id", "")),
                "filename":     getattr(a, "filename", None),
                "content_type": getattr(a, "content_type", None),
                "url":          getattr(a, "url", None),
                "proxy_url":    getattr(a, "proxy_url", None),
                "size":         getattr(a, "size", None),
                "width":        getattr(a, "width", None),
                "height":       getattr(a, "height", None),
            })
        except Exception as exc:
            print(f"ATTACHMENT_SERIALIZE_ERROR {repr(exc)}", flush=True)
    return out


# ── image fetching ────────────────────────────────────────────────────────────
def _is_image(attachment_meta: dict) -> bool:
    """Return True if the attachment looks like an image."""
    ct = (attachment_meta.get("content_type") or "").lower()
    fn = (attachment_meta.get("filename") or "").lower()
    if ct.startswith("image/"):
        return True
    return fn.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"))


async def fetch_one_image(
    session: aiohttp.ClientSession,
    meta: dict,
    label: str,
) -> dict | None:
    """
    Fetch a single attachment as base64.
    Uses proxy_url first, falls back to url.
    Returns { data_url, mime, name, url } or None on failure.
    Never raises.
    """
    url = meta.get("proxy_url") or meta.get("url") or ""
    if not url:
        print(f"FETCH_SKIP no_url {label}", flush=True)
        return None

    mime = (meta.get("content_type") or "image/jpeg").split(";")[0].strip()
    name = meta.get("filename") or f"{label}.jpg"

    try:
        timeout = aiohttp.ClientTimeout(total=ATTACHMENT_FETCH_TIMEOUT)
        async with session.get(url, timeout=timeout) as resp:
            if resp.status != 200:
                print(f"FETCH_ERROR {label} http={resp.status}", flush=True)
                return None
            data = await resp.read()
    except asyncio.TimeoutError:
        print(f"FETCH_TIMEOUT {label} >{ATTACHMENT_FETCH_TIMEOUT}s", flush=True)
        return None
    except Exception as exc:
        print(f"FETCH_ERROR {label} {repr(exc)}", flush=True)
        return None

    if len(data) > MAX_ATTACHMENT_BYTES:
        print(f"FETCH_SKIP too_large {label} {len(data)} bytes", flush=True)
        return None

    b64 = base64.b64encode(data).decode("ascii")
    data_url = f"data:{mime};base64,{b64}"

    print(f"FETCH_OK {label} {len(data)} bytes mime={mime}", flush=True)
    return {
    "data": b64,
    "data_url": data_url,
    "mime": mime,
    "name": name,
    "url": url,
}


async def fetch_images(
    session: aiohttp.ClientSession,
    attachments_meta: list,
    label_prefix: str,
) -> tuple[list, list]:
    """
    Fetch up to MAX_B64_ATTACHMENTS images from a list of attachment metadata dicts.
    Respects MAX_TOTAL_B64_BYTES budget cumulatively.
    Returns (b64_list, meta_list) — both are always lists, never None.
    b64_list items: { data_url, mime, name, url }
    meta_list items: { mime, name, url }
    """
    b64_list  = []
    meta_list = []
    total_bytes = 0

    image_metas = [m for m in (attachments_meta or []) if _is_image(m)]

    for i, meta in enumerate(image_metas[:MAX_B64_ATTACHMENTS]):
        label = f"{label_prefix}_{i}"
        result = await fetch_one_image(session, meta, label)
        if result is None:
            continue

        # crude byte estimate from base64 length (~75% of raw)
        approx_bytes = len(result["data_url"]) * 3 // 4
        if total_bytes + approx_bytes > MAX_TOTAL_B64_BYTES:
            print(f"FETCH_SKIP budget_exceeded {label}", flush=True)
            break

        total_bytes += approx_bytes
        b64_list.append(result)
        meta_list.append({
            "mime": result["mime"],
            "name": result["name"],
            "url":  result["url"],
        })

    return b64_list, meta_list


# ── activity helper ───────────────────────────────────────────────────────────
def get_activities(message):
    try:
        member = message.guild.get_member(message.author.id) if message.guild else None
        if not member:
            return []
        return [
            {
                "name":    a.name,
                "type":    a.type.name,
                "details": getattr(a, "details", None),
                "state":   getattr(a, "state", None),
            }
            for a in member.activities
        ]
    except Exception as exc:
        print(f"get_activities failed: {exc}", flush=True)
        return []


# ── payload builder ───────────────────────────────────────────────────────────
async def build_payload(message) -> dict:
    """
    Build the full n8n webhook payload.
    Fetches images for direct attachments and referenced-message attachments.
    Never raises — failures are logged and skipped.
    """
    # ── direct attachments ────────────────────────────────────────────────────
    direct_meta = []
    try:
        direct_meta = serialize_attachments(message.attachments)
    except Exception as exc:
        print(f"ATTACHMENT_BUILD_ERROR {repr(exc)}", flush=True)

    # ── referenced message ────────────────────────────────────────────────────
    ref_data    = None
    ref_meta    = []
    try:
        ref = message.reference
        if ref and ref.resolved and isinstance(ref.resolved, discord.Message):
            ref_msg  = ref.resolved
            ref_meta = serialize_attachments(ref_msg.attachments)
            ref_data = {
                "id":          str(ref_msg.id),
                "message_id":  str(ref_msg.id),
                "author_id":   str(ref_msg.author.id),
                "author_name": getattr(ref_msg.author, "display_name", str(ref_msg.author)),
                "content":     ref_msg.content or "",
                "attachments": ref_meta,
                # b64 filled in below after fetch
                "attachments_b64":      [],
                "attachments_b64_meta": [],
            }
    except Exception as exc:
        print(f"REF_BUILD_ERROR {repr(exc)}", flush=True)

    # ── fetch images (non-blocking — errors logged, never re-raised) ──────────
    direct_b64_list, direct_b64_meta = [], []
    ref_b64_list,    ref_b64_meta    = [], []

    try:
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            tasks = [
                fetch_images(session, direct_meta, "direct"),
                fetch_images(session, ref_meta,    "ref"),
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            if isinstance(results[0], Exception):
                print(f"FETCH_DIRECT_ERROR {repr(results[0])}", flush=True)
            else:
                direct_b64_list, direct_b64_meta = results[0]

            if isinstance(results[1], Exception):
                print(f"FETCH_REF_ERROR {repr(results[1])}", flush=True)
            else:
                ref_b64_list, ref_b64_meta = results[1]

    except Exception as exc:
        print(f"FETCH_SESSION_ERROR {repr(exc)}", flush=True)

    # ── patch ref_data with fetched b64 ──────────────────────────────────────
    if ref_data is not None:
        ref_data["attachments_b64"]      = ref_b64_list
        ref_data["attachments_b64_meta"] = ref_b64_meta

    # ── summary log (no URLs or tokens) ──────────────────────────────────────
    print(
        f"PAYLOAD_READY "
        f"direct_attachments={len(direct_meta)} "
        f"direct_b64_kept={len(direct_b64_list)} "
        f"ref_attachments={len(ref_meta)} "
        f"ref_b64_kept={len(ref_b64_list)}",
        flush=True,
    )

    return {
        "body": {
            "body": {
                "content":      message.content or "",
                "author":       str(message.author.id),
                "author_id":    str(message.author.id),
                "author_name":  getattr(message.author, "display_name", str(message.author)),
                "channel_id":   str(message.channel.id),
                "channel_name": getattr(message.channel, "name", "") or "",
                "guild_name":   message.guild.name if message.guild else "",
                "message_id":   str(message.id),
                # Direct attachment metadata (CDN URLs intact for n8n fallback)
                "attachments":            direct_meta,
                # Direct attachment base64 — fetched here in worker
                "attachments_b64":        direct_b64_list,
                "attachments_b64_meta":   direct_b64_meta,
                "attachments_b64_kept_count": len(direct_b64_list),
                # Referenced-message attachment base64 at top level for Edit Fields1
                "referenced_attachments":              json.dumps(ref_meta),
                "referenced_attachments_b64":          ref_b64_list,
                "referenced_attachments_b64_meta":     ref_b64_meta,
                "referenced_attachments_b64_kept_count": len(ref_b64_list),
                "discord_activities": json.dumps(get_activities(message)),
                "referenced_message": ref_data,
            }
        }
    }


# ── bot events ────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"Bot ready: {bot.user} ID={bot.user.id}", flush=True)
    print("N8N_URL configured (value hidden)", flush=True)
    print(
        f"Image fetch config: MAX_B64_ATTACHMENTS={MAX_B64_ATTACHMENTS} "
        f"MAX_ATTACHMENT_BYTES={MAX_ATTACHMENT_BYTES // 1024 // 1024}MB "
        f"MAX_TOTAL_B64_BYTES={MAX_TOTAL_B64_BYTES // 1024 // 1024}MB "
        f"ATTACHMENT_FETCH_TIMEOUT={ATTACHMENT_FETCH_TIMEOUT}s",
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

    stop         = asyncio.Event()
    typing_task  = asyncio.create_task(keep_typing(message.channel, stop))

    try:
        # build_payload fetches images; errors are caught inside and never re-raised
        payload = await build_payload(message)
        body    = payload["body"]["body"]

        print("FORWARD_START", flush=True)
        print(
            f"POST n8n channel_id={body.get('channel_id')} "
            f"message_id={body.get('message_id')} "
            f"direct_attachments={len(body.get('attachments', []))} "
            f"direct_b64_kept={body.get('attachments_b64_kept_count', 0)} "
            f"ref_b64_kept={body.get('referenced_attachments_b64_kept_count', 0)}",
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
                    print("n8n accepted", flush=True)

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
