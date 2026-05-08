import os
import discord
import requests
from discord.ext import commands
from threading import Thread
from flask import Flask

# 1. Tạo Web Server mini để Railway không tắt Bot
app = Flask('')
@app.route('/')
def home():
    return "Cô Giáo AI đang online!"

def run():
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)))

# 2. Cấu hình Bot Discord
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# Link Production chuẩn của n8n
N8N_WEBHOOK_URL = "https://primary-production-5647d.up.railway.app/webhook-test/discord-ai"

@bot.event
async def on_ready():
    print(f"✅ Đã đăng nhập: {bot.user}")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    # Gửi dữ liệu sang n8n
    payload = {
        "body": {
            "content": message.content,
            "author": str(message.author),
            "channel_id": str(message.channel.id),
            "channel_name": str(message.channel.name)
        }
    }

    try:
        response = requests.post(N8N_WEBHOOK_URL, json=payload)
        if response.status_code == 200:
            data = response.json()
            # Lấy câu trả lời từ AI và gửi ngược lại Discord
            if isinstance(data, list) and len(data) > 0 and 'output' in data[0]:
                await message.channel.send(data[0]['output'])
            elif isinstance(data, dict) and 'output' in data:
                await message.channel.send(data['output'])
    except Exception as e:
        print(f"❌ Lỗi n8n: {e}")

    await bot.process_commands(message)

# 3. Chạy cả Web Server và Bot cùng lúc
if __name__ == "__main__":
    t = Thread(target=run)
    t.daemon = True
    t.start()
    
    # CHỈ CHẠY DÒNG NÀY MỘT LẦN DUY NHẤT Ở ĐÂY
    bot.run(os.environ["DISCORD_TOKEN"])
