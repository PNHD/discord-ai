import os
import discord
import requests
from discord.ext import commands
from threading import Thread
from flask import Flask # Thêm cái này

# Tạo một app Flask nhỏ để Railway không tắt bot
app = Flask('')
@app.route('/')
def home():
    return "Bot is alive!"

def run():
    app.run(host='0.0.0.0', port=int(os.environ.get("PORT", 8080)))

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix='!', intents=intents)

# THAY LINK PRODUCTION CỦA ÔNG VÀO ĐÂY
N8N_WEBHOOK_URL = "https://primary-production-5647d.up.railway.app/webhook/discord-ai"

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

@bot.event
async def on_message(message):
    # Không tự trả lời tin nhắn của chính bot
    if message.author == bot.user:
        return

    # Gửi tin nhắn sang n8n
    payload = {
        "body": {
            "content": message.content,
            "author": str(message.author),
            "channel_id": str(message.channel.id),
            "channel_name": str(message.channel.name)
        }
    }

    try:
        # Gửi sang n8n và đợi kết quả
        response = requests.post(N8N_WEBHOOK_URL, json=payload)
        
        # Nếu n8n có trả về text (output), thì bot nhắn lại lên Discord
        # Lưu ý: n8n Node AI Agent thường trả về JSON có trường 'output'
        if response.status_code == 200:
            data = response.json()
            # Tùy vào node cuối của n8n, nhưng thường là lấy data[0]['output'] hoặc data['output']
            # Ở đây tôi làm an toàn: nếu n8n trả lời, mình lấy nội dung đó
            if isinstance(data, list) and 'output' in data[0]:
                await message.channel.send(data[0]['output'])
            elif isinstance(data, dict) and 'output' in data:
                await message.channel.send(data['output'])
    except Exception as e:
        print(f"Lỗi gửi n8n: {e}")

    await bot.process_commands(message)

@bot.command()
async def ping(ctx):
    await ctx.send('pong')

# XÓA CÁI DÒNG bot.run(os.environ["DISCORD_TOKEN"]) CŨ Ở ĐÂY ĐI

# PHẢI LÀ NHƯ THẾ NÀY:
if __name__ == "__main__":
    t = Thread(target=run)
    t.daemon = True # Thêm dòng này để server Flask tắt cùng bot
    t.start() 
    
    # Chỉ chạy bot.run DUY NHẤT một lần ở cuối cùng này thôi
    bot.run(os.environ["DISCORD_TOKEN"])
