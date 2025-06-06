import os
import discord
from discord.ext import commands
from dotenv import load_dotenv
import asyncio # asyncioをインポート

# .env ファイルから環境変数をロード
load_dotenv()

# Discord Botのトークンを取得
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# Botのインテント（ボイスステートの変更など）を設定
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True # SERVER MEMBERS INTENT をDeveloper Portalで有効にする必要あり

bot = commands.Bot(command_prefix='!', intents=intents)

@bot.event
async def on_ready():
    print(f'{bot.user} がDiscordに接続しました！')
    print(f'Bot ID: {bot.user.id}')
    # 起動時にボイスチャンネルに参加するなどの初期処理は、後で必要に応じて追加

@bot.event
async def on_voice_state_update(member, before, after):
    # ユーザーがボイスチャンネルに参加/退出した際に呼ばれる
    if member == bot.user:
        return # Bot自身の状態変更は無視

    # 誰かがボイスチャンネルに参加した場合のログ
    if before.channel is None and after.channel is not None:
        print(f'{member.display_name} が {after.channel.name} に参加しました。')
    # 誰かがボイスチャンネルから退出した場合のログ
    elif before.channel is not None and after.channel is None:
        print(f'{member.display_name} が {before.channel.name} から退出しました。')
    # 誰かがボイスチャンネルを移動した場合のログ
    elif before.channel is not None and after.channel is not None and before.channel != after.channel:
        print(f'{member.display_name} が {before.channel.name} から {after.channel.name} に移動しました。')

@bot.command()
async def hello(ctx):
    await ctx.send(f'こんにちは、{ctx.author.display_name}さん！')

@bot.command()
async def join(ctx):
    # コマンドを実行したユーザーがボイスチャンネルにいるか確認
    if ctx.author.voice is None:
        await ctx.send("ボイスチャンネルに接続してください。")
        return

    # 既にBotがボイスチャンネルにいる場合は、一度切断
    if ctx.voice_client:
        await ctx.voice_client.disconnect()

    # ユーザーがいるボイスチャンネルに接続
    voice_channel = ctx.author.voice.channel
    vc = await voice_channel.connect()
    await ctx.send(f'ボイスチャンネル **{voice_channel.name}** に接続しました！')
    print(f'Botがボイスチャンネル {voice_channel.name} に接続しました。')

    # ここから音声データの受信処理を開始する（次のステップで実装）
    # vc.start_recording() や vc.listen() などを使う
    # そのためにFFmpegが必要

@bot.command()
async def leave(ctx):
    # Botがボイスチャンネルにいるか確認
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("ボイスチャンネルから切断しました。")
        print(f'Botがボイスチャンネルから切断しました。')
    else:
        await ctx.send("ボイスチャンネルに接続していません。")

# ボットを実行
if DISCORD_BOT_TOKEN:
    bot.run(DISCORD_BOT_TOKEN)
else:
    print("エラー: Discord Botトークンが設定されていません。'.env'ファイルを確認してください。")
