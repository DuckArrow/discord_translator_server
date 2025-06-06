import os
import discord
from discord.ext import commands
from dotenv import load_dotenv
import asyncio
import time
import wave # WAVファイル書き込み用

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

# 各ユーザーの音声ファイルを管理する辞書
# {user_id: wave.Wave_write object}
user_audio_files = {}
# 音声受信タスクへの参照を保持するグローバル変数
voice_listener_task = None

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


# 各ユーザーの音声データを処理するコールバック関数
# この関数は、ボイスクライアントがPCMデータを受信するたびに呼び出されます。
# data: PCMデータ (bytes)
# user: 発言したユーザーの discord.User または discord.Member オブジェクト
def process_user_audio(data, user):
    global user_audio_files
    
    user_id = user.id
    if user_id not in user_audio_files:
        # 新しいユーザーの音声ストリームが来た場合、新しいWAVファイルを作成
        # ファイル名をタイムスタンプとユーザーID、ユーザー名でユニークにする
        file_name = f"{user_id}_{user.display_name}_{int(time.time())}.wav"
        file_path = os.path.join(AUDIO_OUTPUT_DIR, file_name)
        
        wf = wave.open(file_path, 'wb')
        # Discordの音声データはPCM、48kHz、ステレオ（2チャンネル）、16bitです
        wf.setnchannels(2) # ステレオ
        wf.setsampwidth(2) # 16-bit = 2 bytes
        wf.setframerate(48000) # 48kHz
        user_audio_files[user_id] = wf
        print(f"録音開始: {user.display_name} ({user_id}) の音声 -> {file_path}")

    # 音声データをWAVファイルに書き込む
    user_audio_files[user_id].writeframes(data)


# 全ての音声ファイルを閉じるクリーンアップ関数
def cleanup_audio_files():
    global user_audio_files
    print("すべてのユーザーの音声ファイルをクリーンアップ中...")
    for user_id, wf in user_audio_files.items():
        wf.close()
        print(f"ユーザー {user_id} のWAVファイルを閉じました。")
    user_audio_files.clear()


# ボイスチャンネルへの接続と個別音声の録音開始
@bot.command()
async def join(ctx):
    global voice_listener_task
    
    if ctx.author.voice is None:
        await ctx.send("ボイスチャンネルに接続してください。")
        return

    if ctx.voice_client:
        # 既にボイスチャンネルにいる場合は、一度切断
        # 既存の録音がある場合は停止し、ファイルを閉じる
        if voice_listener_task and not voice_listener_task.done():
            voice_listener_task.cancel() # リスナータスクをキャンセル
            await ctx.send("既存の録音を停止しました。")
            print("既存のボイス録音を停止しました。")
            try:
                await voice_listener_task # キャンセルが完了するのを待つ
            except asyncio.CancelledError:
                pass # キャンセルは正常な終了なので無視
        cleanup_audio_files() # 全てのファイルを閉じる
        
        await ctx.voice_client.disconnect()
        await asyncio.sleep(0.5)

    voice_channel = ctx.author.voice.channel
    vc = await voice_channel.connect()
    await ctx.send(f'ボイスチャンネル **{voice_channel.name}** に接続しました！')
    print(f'Botがボイスチャンネル {voice_channel.name} に接続しました。')

    # vc.listen() で音声受信を開始し、process_user_audioをコールバックとして登録
    # listen() はタスクを返すので、そのタスクを保持しておき、停止時にキャンセルする
    voice_listener_task = vc.listen(process_user_audio)

    await ctx.send(f"ボイスチャンネルの各ユーザーの音声録音を開始しました。ファイルは`{AUDIO_OUTPUT_DIR}`に保存されます。")
    print(f"各ユーザーの音声録音開始: ターゲットディレクトリ `{AUDIO_OUTPUT_DIR}`")


@bot.command()
async def stop_record(ctx):
    global voice_listener_task
    
    if ctx.voice_client:
        if voice_listener_task and not voice_listener_task.done():
            voice_listener_task.cancel() # リスナータスクをキャンセル
            await ctx.send("録音を停止しました。録音ファイルはサーバーに保存されました。")
            print("ボイスチャンネルの録音を停止しました。")
            try:
                await voice_listener_task # キャンセルが完了するのを待つ
            except asyncio.CancelledError:
                pass # キャンセルは正常な終了なので無視
            cleanup_audio_files() # 全てのファイルを閉じる
        else:
            await ctx.send("現在録音していません。")
    else:
        await ctx.send("ボイスチャンネルに接続していません。")

@bot.command()
async def leave(ctx):
    global voice_listener_task
    
    if ctx.voice_client:
        if voice_listener_task and not voice_listener_task.done():
            voice_listener_task.cancel() # リスナータスクをキャンセル
            print("ボイスチャンネルの録音を停止しました。")
            try:
                await voice_listener_task # キャンセルが完了するのを待つ
            except asyncio.CancelledError:
                pass # キャンセルは正常な終了なので無視
        cleanup_audio_files() # 全てのファイルを閉じる
        await ctx.voice_client.disconnect()
        await ctx.send("ボイスチャンネルから切断しました。")
        print(f'Botがボイスチャンネルから切断しました。')
    else:
        await ctx.send("ボイスチャンネルに接続していません。")

if DISCORD_BOT_TOKEN:
    bot.run(DISCORD_BOT_TOKEN)
else:
    print("エラー: Discord Botトークンが設定されていません。'.env'ファイルを確認してください。")
