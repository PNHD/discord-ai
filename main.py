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
    print('Pillow not installed; images sent uncompressed', flush=True)

app = Flask(__name__)

@app.route('/')
def home():
    return 'Co Giao AI online'

def run_flask():
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

BOT_ID = int(os.environ.get('BOT_ID', '1502278190788382770'))
N8N_URL = os.environ.get('N8N_WEBHOOK_URL', 'https://primary-production-5647d.up.railway.app/webhook/discord-ai')
PROCESSED = deque(maxlen=500)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True
bot = commands.Bot(command_prefix='!', intents=intents)

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
    return f'<@{BOT_ID}>' in (message.content or '') or f'<@!{BOT_ID}>' in (message.content or '')

def replies_to_bot(message):
    ref = message.reference
    if ref and ref.resolved and isinstance(ref.resolved, discord.Message):
        return ref.resolved.author.id == BOT_ID
    return False

def should_forward(message):
    return mentions_bot(message) or replies_to_bot(message)

def compress_image(raw, max_px=1280, quality=75):
    if not HAS_PIL:
        return raw, 'image/png'
    try:
        img = Image.open(io.BytesIO(raw)).convert('RGB')
        w, h = img.size
        if max(w, h) > max_px:
            ratio = max_px / max(w, h)
            img = img.resize((int(w * ratio), int(h * ratio)), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format='JPEG', quality=quality, optimize=True)
        return buf.getvalue(), 'image/jpeg'
    except Exception as exc:
        print(f'image compress failed: {exc}; sending original', flush=True)
        return raw, 'image/png'

async def download_attachments_b64(session, attachments):
    out = []
    for att in attachments or []:
        url = getattr(att, 'url', None) or getattr(att, 'proxy_url', None)
        filename = getattr(att, 'filename', None) or 'image.png'
        ctype = (getattr(att, 'content_type', '') or '').lower()
        if ctype and not ctype.startswith('image/'):
            print(f'skip non-image attachment: {filename} ({ctype})', flush=True)
            continue
        if not url:
            print(f'attachment has no url: {filename}', flush=True)
            continue
        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status != 200:
                    print(f'download failed {resp.status}: {filename}', flush=True)
                    continue
                raw = await resp.read()
                data, mime = await asyncio.get_running_loop().run_in_executor(None, compress_image, raw)
                name = filename.rsplit('.', 1)[0] + '.jpg' if mime == 'image/jpeg' else filename
                out.append({'data': base64.b64encode(data).decode('utf-8'), 'mime': mime, 'name': name})
                print(f'downloaded attachment: {filename} -> {name}', flush=True)
        except Exception as exc:
            print(f'attachment download error {filename}: {repr(exc)}', flush=True)
    return out

def get_activities(message):
    try:
        member = message.guild.get_member(message.author.id) if message.guild else None
        if not member:
            return []
        return [{'name': a.name, 'type': a.type.name, 'details': getattr(a, 'details', None), 'state': getattr(a, 'state', None)} for a in member.activities]
    except Exception as exc:
        print(f'get_activities failed: {exc}', flush=True)
        return []

async def build_payload(session, message):
    ref_data = None
    ref_b64 = []
    ref = message.reference
    if ref and ref.resolved and isinstance(ref.resolved, discord.Message):
        ref_msg = ref.resolved
        ref_b64 = await download_attachments_b64(session, ref_msg.attachments)
        ref_data = {
            'id': str(ref_msg.id),
            'message_id': str(ref_msg.id),
            'author_id': str(ref_msg.author.id),
            'author_name': getattr(ref_msg.author, 'display_name', str(ref_msg.author)),
            'content': ref_msg.content or '',
            'attachments': [{'proxy_url': a.proxy_url, 'url': a.url, 'filename': a.filename} for a in ref_msg.attachments],
        }

    direct_b64 = await download_attachments_b64(session, message.attachments)

    return {'body': {'body': {
        'content': message.content or '',
        'author': str(message.author.id),
        'author_name': getattr(message.author, 'display_name', str(message.author)),
        'channel_id': str(message.channel.id),
        'channel_name': getattr(message.channel, 'name', '') or '',
        'guild_name': message.guild.name if message.guild else '',
        'message_id': str(message.id),
        'attachments': [{'proxy_url': a.proxy_url, 'url': a.url, 'filename': a.filename} for a in message.attachments],
        'attachments_b64': json.dumps(direct_b64),
        'referenced_attachments_b64': json.dumps(ref_b64),
        'discord_activities': json.dumps(get_activities(message)),
        'referenced_message': ref_data,
    }}}

@bot.event
async def on_ready():
    print(f'Bot ready: {bot.user} ID={bot.user.id}', flush=True)
    print(f'N8N_URL={N8N_URL}', flush=True)
    print('Quiet mode: only bot mentions or replies to bot are forwarded to n8n', flush=True)
    print('Reply attachments are sent as referenced_attachments_b64', flush=True)

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    print(f"Seen channel={getattr(message.channel, 'name', '')} id={message.channel.id} author={message.author} content={repr((message.content or '')[:100])} attachments={len(message.attachments)}", flush=True)

    if message.id in PROCESSED:
        print(f'skip duplicate message {message.id}', flush=True)
        return
    PROCESSED.append(message.id)

    if not should_forward(message):
        print('skip: no bot mention and not a reply to bot', flush=True)
        return

    stop = asyncio.Event()
    typing_task = asyncio.create_task(keep_typing(message.channel, stop))
    try:
        async with aiohttp.ClientSession() as session:
            payload = await build_payload(session, message)
            direct_count = len(json.loads(payload['body']['body']['attachments_b64']))
            ref_count = len(json.loads(payload['body']['body']['referenced_attachments_b64']))
            print(f"POST n8n channel_id={payload['body']['body']['channel_id']} images={direct_count} reply_images={ref_count}", flush=True)
            async with session.post(N8N_URL, json=payload, timeout=aiohttp.ClientTimeout(total=180)) as resp:
                text = await resp.text()
                if resp.status != 200:
                    print(f'n8n error {resp.status}: {text[:500]}', flush=True)
                else:
                    print(f'n8n accepted: {text[:200]}', flush=True)
    except asyncio.TimeoutError:
        print('n8n timeout >180s', flush=True)
    except Exception as exc:
        print(f'bridge error: {repr(exc)}', flush=True)
    finally:
        stop.set()
        typing_task.cancel()

    await bot.process_commands(message)

if __name__ == '__main__':
    Thread(target=run_flask, daemon=True).start()
    bot.run(os.environ['DISCORD_TOKEN'])
