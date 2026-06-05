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

MAX_B64_ATTACHMENTS      = 3
MAX_ATTACHMENT_BYTES     = 3 * 1024 * 1024   # 3 MB per image
MAX_TOTAL_B64_BYTES      = 8 * 1024 * 1024   # 8 MB total across all images
ATTACHMENT_FETCH_TIMEOUT = 5                 # seconds per image

PROCESSED = deque(maxlen=500)

intents = discord.Intents.default()
intents.message_content = True
intents.members         = True
intents.presences       = True
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
    """True if the direct reply target is the bot (resolved or by message_id lookup)."""
    ref = message.reference
    if not ref:
        return False
    # Fast path: cache already resolved it
    if ref.resolved and isinstance(ref.resolved, discord.Message):
        return ref.resolved.author.id == BOT_ID
    # Slow path: we can't know without fetching, assume True so on_message fetches it
    return False


def should_forward(message):
    return mentions_bot(message) or replies_to_bot(message)


# ── attachment metadata serialiser ───────────────────────────────────────────
def serialize_attachments(attachments) -> list:
    """Return a list of attachment metadata dicts with CDN URLs.  No network calls."""
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


# ── reply resolution ──────────────────────────────────────────────────────────
async def resolve_ref_message(message) -> discord.Message | None:
    """
    Resolve the referenced (replied-to) message with full attachment data.

    Three-step process, logged in detail:

    Step 1 – Use ref.resolved if it's a real discord.Message.
             Log what it resolved to.

    Step 2 – If resolved is missing or is only a PartialMessage, call
             channel.fetch_message(ref.message_id) to get the full object.

    Step 3 – Walk-up: if the resolved message was sent by the bot itself
             and has NO attachments, but DOES have its own reference,
             fetch THAT parent message — because the user likely replied
             to the bot's reply, and the actual image is one level deeper
             (e.g. Dứa's original homework message).

    Returns the best discord.Message we could find, or None.
    """
    ref = message.reference
    if not ref:
        return None

    ref_msg_id = getattr(ref, "message_id", None)
    print(
        f"REF_DEBUG message_id={ref_msg_id} "
        f"cached_message={ref.cached_message!r} "
        f"resolved_type={type(ref.resolved).__name__}",
        flush=True,
    )

    # ── step 1: use cached resolved if it is a full Message ──────────────────
    resolved = ref.resolved
    if resolved and isinstance(resolved, discord.Message):
        print(
            f"REF_RESOLVED_CACHE author={resolved.author} "
            f"author_id={resolved.author.id} "
            f"attachments={len(resolved.attachments)}",
            flush=True,
        )
        ref_msg = resolved
    else:
        # ── step 2: fetch the message explicitly ──────────────────────────────
        ref_msg = None
        if ref_msg_id:
            try:
                ref_msg = await message.channel.fetch_message(ref_msg_id)
                print(
                    f"REF_FETCHED author={ref_msg.author} "
                    f"author_id={ref_msg.author.id} "
                    f"attachments={len(ref_msg.attachments)}",
                    flush=True,
                )
            except Exception as exc:
                print(f"REF_FETCH_ERROR message_id={ref_msg_id} {repr(exc)}", flush=True)
                return None
        else:
            print("REF_NO_MESSAGE_ID cannot resolve reference", flush=True)
            return None

    # ── step 3: walk-up if resolved is bot's own reply with no attachments ───
    # Scenario: Ba Đăng replied to the BOT's grading reply.
    #           The bot's message has no attachments.
    #           The attachments are in the message the BOT was originally replying to
    #           (i.e., Dứa's homework message one level up).
    if (
        ref_msg is not None
        and ref_msg.author.id == BOT_ID
        and len(ref_msg.attachments) == 0
        and ref_msg.reference is not None
    ):
        parent_id = getattr(ref_msg.reference, "message_id", None)
        print(
            f"REF_WALKUP bot_msg_has_no_attachments=True "
            f"bot_msg_id={ref_msg.id} "
            f"walking_up_to_parent_id={parent_id}",
            flush=True,
        )
        if parent_id:
            try:
                parent_msg = await message.channel.fetch_message(parent_id)
                print(
                    f"REF_PARENT_FETCHED author={parent_msg.author} "
                    f"author_id={parent_msg.author.id} "
                    f"attachments={len(parent_msg.attachments)}",
                    flush=True,
                )
                # Only use the parent if it actually has attachments
                if len(parent_msg.attachments) > 0:
                    ref_msg = parent_msg
                    print("REF_WALKUP_ACCEPTED using parent as ref_msg", flush=True)
                else:
                    print("REF_WALKUP_SKIPPED parent also has no attachments", flush=True)
            except Exception as exc:
                print(f"REF_PARENT_FETCH_ERROR parent_id={parent_id} {repr(exc)}", flush=True)

    print(
        f"REF_FINAL author={ref_msg.author if ref_msg else None} "
        f"author_id={ref_msg.author.id if ref_msg else None} "
        f"attachments={len(ref_msg.attachments) if ref_msg else 0}",
        flush=True,
    )
    return ref_msg


# ── image fetching ────────────────────────────────────────────────────────────
def _is_image(meta: dict) -> bool:
    ct = (meta.get("content_type") or "").lower()
    fn = (meta.get("filename") or "").lower()
    return ct.startswith("image/") or fn.endswith(
        (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff")
    )


async def fetch_one_image(
    session: aiohttp.ClientSession,
    meta: dict,
    label: str,
) -> dict | None:
    """
    Fetch one image. proxy_url first, fallback url.
    Returns { data_url, mime, name, url } or None.  Never raises.
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

    b64      = base64.b64encode(data).decode("ascii")
    data_url = f"data:{mime};base64,{b64}"
    print(f"FETCH_OK {label} {len(data)} bytes mime={mime}", flush=True)
    return {"data_url": data_url, "mime": mime, "name": name, "url": url}


async def fetch_images(
    session: aiohttp.ClientSession,
    attachments_meta: list,
    label_prefix: str,
) -> tuple[list, list]:
    """
    Fetch up to MAX_B64_ATTACHMENTS images, respecting MAX_TOTAL_B64_BYTES budget.
    Returns (b64_list, meta_list) — always lists, never None.
    """
    b64_list    = []
    meta_list   = []
    total_bytes = 0

    image_metas = [m for m in (attachments_meta or []) if _is_image(m)]

    for i, meta in enumerate(image_metas[:MAX_B64_ATTACHMENTS]):
        label  = f"{label_prefix}_{i}"
        result = await fetch_one_image(session, meta, label)
        if result is None:
            continue

        approx_bytes = len(result["data_url"]) * 3 // 4
        if total_bytes + approx_bytes > MAX_TOTAL_B64_BYTES:
            print(f"FETCH_SKIP budget_exceeded {label}", flush=True)
            break

        total_bytes += approx_bytes
        b64_list.append(result)
        meta_list.append({"mime": result["mime"], "name": result["name"], "url": result["url"]})

    return b64_list, meta_list


# ── activity helper ───────────────────────────────────────────────────────────
def get_activities(message) -> list:
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
    Uses resolve_ref_message() for correct reply resolution (including walk-up).
    Fetches images for direct and referenced attachments.
    Never raises.
    """
    # ── direct attachments metadata ───────────────────────────────────────────
    direct_meta: list = []
    try:
        direct_meta = serialize_attachments(message.attachments)
    except Exception as exc:
        print(f"ATTACHMENT_BUILD_ERROR {repr(exc)}", flush=True)

    # ── resolve referenced message with full walk-up logic ───────────────────
    ref_msg:  discord.Message | None = None
    ref_meta: list = []
    ref_data: dict | None = None

    try:
        ref_msg = await resolve_ref_message(message)
    except Exception as exc:
        print(f"RESOLVE_REF_ERROR {repr(exc)}", flush=True)

    if ref_msg is not None:
        try:
            ref_meta = serialize_attachments(ref_msg.attachments)
            ref_data = {
                "id":           str(ref_msg.id),
                "message_id":   str(ref_msg.id),
                "author_id":    str(ref_msg.author.id),
                "author_name":  getattr(ref_msg.author, "display_name", str(ref_msg.author)),
                "content":      ref_msg.content or "",
                "attachments":  ref_meta,
                # b64 filled in after fetch below
                "attachments_b64":      [],
                "attachments_b64_meta": [],
            }
        except Exception as exc:
            print(f"REF_SERIALIZE_ERROR {repr(exc)}", flush=True)

    # ── fetch images (parallel, non-blocking) ────────────────────────────────
    direct_b64_list: list = []
    direct_b64_meta: list = []
    ref_b64_list:    list = []
    ref_b64_meta:    list = []

    try:
        connector = aiohttp.TCPConnector(ssl=False)
        async with aiohttp.ClientSession(connector=connector) as session:
            results = await asyncio.gather(
                fetch_images(session, direct_meta, "direct"),
                fetch_images(session, ref_meta,    "ref"),
                return_exceptions=True,
            )

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

    # ── build top-level referenced_attachment_urls for n8n compatibility ─────
    ref_attachment_urls = [
        m.get("proxy_url") or m.get("url") or ""
        for m in ref_meta
        if m.get("proxy_url") or m.get("url")
    ]

    # ── final summary log (no CDN URLs or tokens) ────────────────────────────
    print(
        f"PAYLOAD_READY "
        f"direct_attachments={len(direct_meta)} "
        f"direct_b64_kept={len(direct_b64_list)} "
        f"ref_attachments={len(ref_meta)} "
        f"ref_b64_kept={len(ref_b64_list)} "
        f"ref_author={'[bot]' if (ref_msg and ref_msg.author.id == BOT_ID) else ('[user]' if ref_msg else '[none]')}",
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
                # ── direct attachments ────────────────────────────────────────
                "attachments":                direct_meta,
                "attachments_b64":            direct_b64_list,
                "attachments_b64_meta":       direct_b64_meta,
                "attachments_b64_kept_count": len(direct_b64_list),
                # ── referenced-message attachments (top-level for Edit Fields1) ─
                "referenced_attachments":              json.dumps(ref_meta),
                "referenced_attachment_urls":          json.dumps(ref_attachment_urls),
                "referenced_attachments_b64":          ref_b64_list,
                "referenced_attachments_b64_meta":     ref_b64_meta,
                "referenced_attachments_b64_kept_count": len(ref_b64_list),
                # ── misc ──────────────────────────────────────────────────────
                "discord_activities": json.dumps(get_activities(message)),
                # Full referenced message object (includes its own attachments + b64)
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
        f"attachments={len(message.attachments)} "
        f"has_reference={message.reference is not None}",
        flush=True,
    )

    if message.id in PROCESSED:
        print(f"skip duplicate message {message.id}", flush=True)
        return
    PROCESSED.append(message.id)

    # ── routing ───────────────────────────────────────────────────────────────
    # Note: for reply messages where resolved is not cached, we can't tell if it's
    # a reply-to-bot without fetching.  We forward all reply messages that mention
    # the bot; resolve_ref_message handles the fetch inside build_payload.
    if not should_forward(message):
        # Second chance: if message has a reference but resolved isn't cached,
        # we need to fetch to decide whether it's a reply-to-bot.
        ref = message.reference
        if ref and ref.message_id and not mentions_bot(message):
            try:
                target = await message.channel.fetch_message(ref.message_id)
                if target.author.id != BOT_ID:
                    print("skip: reply target is not bot and no bot mention", flush=True)
                    return
                print(f"REF_ROUTING fetched ref to confirm reply-to-bot", flush=True)
            except Exception as exc:
                print(f"REF_ROUTING_FETCH_ERROR {repr(exc)} — skipping message", flush=True)
                return
        else:
            print("skip: no bot mention and not a reply to bot", flush=True)
            return

    stop        = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing(message.channel, stop))

    try:
        payload = await build_payload(message)
        body    = payload["body"]["body"]

        print("FORWARD_START", flush=True)
        print(
            f"POST n8n "
            f"channel_id={body.get('channel_id')} "
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
