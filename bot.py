import os
import discord
from discord.ext import commands
from dotenv import load_dotenv

# .env ファイルから環境変数をロード
load_dotenv()

# Discord Botのトークンを取得
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# Botのインテント（ボイスステートの変更など）を設定
intents = discord.Intents.default()
intents.message_content = True # メッセージの内容を読み取るためのインテント
intents.voice_states = True # ボイスチャンネルの参加/退出などの状態変化を検知
intents.members = True # メンバー情報にアクセスするため

bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f'{bot.user} がDiscordに接続しました！')
    print(f'Bot ID: {bot.user.id}')
    # 起動時にボイスチャンネルに参加するなどの初期処理をここに書く

@bot.event
async def on_voice_state_update(member, before, after):
    # ユーザーがボイスチャンネルに参加/退出した際に呼ばれる
    if member == bot.user:
        return # Bot自身の状態変更は無視

    if before.channel is None and after.channel is not None:
        print(f'{member.display_name} が {after.channel.name} に参加しました。')
        # ここでBotをボイスチャンネルに参加させるなどの処理を開始できる
        # 例: もし指定のチャンネルなら参加
        # if after.channel.id == YOUR_TARGET_VOICE_CHANNEL_ID:
        #     vc = await after.channel.connect()
        #     print(f"ボットが {after.channel.name} に接続しました。")
        #     # ここから音声データの受信を開始する

    elif before.channel is not None and after.channel is None:
        print(f'{member.display_name} が {before.channel.name} から退出しました。')
        # ボイスチャンネルから退出した際の処理

@bot.command()
async def hello(ctx):
    # !hello コマンドに応答するテスト
    await ctx.send(f'こんにちは、{ctx.author.display_name}さん！')

# ボットを実行
if DISCORD_BOT_TOKEN:
    bot.run(DISCORD_BOT_TOKEN)
else:
    print("エラー: Discord Botトークンが設定されていません。'.env'ファイルを確認してください。")
