import os
import discord
from discord.ext import commands
from dotenv import load_dotenv
import asyncio
import time

load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)

# 音声データ保存用のディレクトリ
AUDIO_OUTPUT_DIR = "recorded_audio"
os.makedirs(AUDIO_OUTPUT_DIR, exist_ok=True)

@bot.event
async def on_ready():
    print(f'{bot.user} がDiscordに接続しました！')
    print(f'Bot ID: {bot.user.id}')

@bot.event
async def on_voice_state_update(member, before, after):
    if member == bot.user:
        return
    # Botが接続しているチャンネルでのユーザーの出入りを監視する場合
    if bot.voice_clients: # BotがどこかのVCに接続している場合
        for vc in bot.voice_clients:
            if before.channel == vc.channel and after.channel != vc.channel:
                print(f'{member.display_name} が {before.channel.name} から退出しました。')
            elif before.channel != vc.channel and after.channel == vc.channel:
                print(f'{member.display_name} が {after.channel.name} に参加しました。')

@bot.command()
async def hello(ctx):
    await ctx.send(f'こんにちは、{ctx.author.display_name}さん！')

# ボイスチャンネルへの接続と全員の録音開始
@bot.command()
async def join(ctx):
    if ctx.author.voice is None:
        await ctx.send("ボイスチャンネルに接続してください。")
        return

    if ctx.voice_client:
        # 既にボイスチャンネルにいる場合は、一度切断
        if ctx.voice_client.is_recording(): # 以前録音中であれば停止
            ctx.voice_client.stop_recording()
        await ctx.voice_client.disconnect()
        await asyncio.sleep(0.5)

    voice_channel = ctx.author.voice.channel
    vc = await voice_channel.connect()
    await ctx.send(f'ボイスチャンネル **{voice_channel.name}** に接続しました！')
    print(f'Botがボイスチャンネル {voice_channel.name} に接続しました。')

    # 録音開始
    # 全員の音声データをまとめて録音し、タイムスタンプ付きのWAVファイルとして保存
    file_path = os.path.join(AUDIO_OUTPUT_DIR, f"all_audio_{int(time.time())}.wav")
    vc.start_recording(discord.sinks.WaveSink(), destination=file_path) # WaveSinkを使用
    await ctx.send(f"ボイスチャンネルの全員の録音を開始しました。ファイル: `{file_path}`")
    print(f"録音開始: {file_path}")

@bot.command()
async def stop_record(ctx):
    if ctx.voice_client and ctx.voice_client.is_recording():
        ctx.voice_client.stop_recording()
        await ctx.send("録音を停止しました。録音ファイルはサーバーに保存されました。")
        print("ボイスチャンネルの録音を停止しました。")
    else:
        await ctx.send("現在録音していません。")

@bot.command()
async def leave(ctx):
    if ctx.voice_client:
        if ctx.voice_client.is_recording():
            ctx.voice_client.stop_recording()
            print("ボイスチャンネルの録音を停止しました。")
        await ctx.voice_client.disconnect()
        await ctx.send("ボイスチャンネルから切断しました。")
        print(f'Botがボイスチャンネルから切断しました。')
    else:
        await ctx.send("ボイスチャンネルに接続していません。")

if DISCORD_BOT_TOKEN:
    bot.run(DISCORD_BOT_TOKEN)
else:
    print("エラー: Discord Botトークンが設定されていません。'.env'ファイルを確認してください。")
