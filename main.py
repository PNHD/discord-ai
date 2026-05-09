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
N8N_WEBHOOK_URL = "https://primary-production-5647d.up.railway.app/webhook/discord-ai"

@bot.event
async def on_ready():
    print(f"✅ Đã đăng nhập: {bot.user}")

@bot.event
async def on_message(message):
    if message.author == bot.user:
        return

    # Lấy ảnh từ Discord gửi sang n8n
    attachments = [{"proxy_url": a.proxy_url} for a in message.attachments]

    payload = {
        "body": {
            "body": {
                "content": message.content,
                "author": str(message.author.id),
                "channel_id": str(message.channel.id),
                "attachments": attachments
            }
        }
    }

    try:
        # Chỉ gửi dữ liệu đi, n8n sẽ xử lý việc trả lời
        response = requests.post(N8N_WEBHOOK_URL, json=payload)
        if response.status_code != 200:
            print(f"❌ n8n phản hồi lỗi: {response.status_code}")
            
    except Exception as e:
        print(f"❌ Lỗi kết nối n8n: {e}")

    await bot.process_commands(message)

# 3. Chạy cả Web Server và Bot cùng lúc
if __name__ == "__main__":
    t = Thread(target=run)
    t.daemon = True
    t.start()
    
    bot.run(os.environ["DISCORD_TOKEN"])
