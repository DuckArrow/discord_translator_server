import os
import discord
from discord.ext import commands
from dotenv import load_dotenv
import asyncio
import time
# import wave # WAVファイル書き込み用 (音声録音機能を無効化するためコメントアウト)

# .env ファイルから環境変数をロード
load_dotenv()

# Discord Botのトークンを取得
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# Botのインテントを設定
# Pycordでは、voice_statesとmembersインテントは特権インテントなので、
# Developer Portalで有効にする必要があります (既に設定済みのはずです)
intents = discord.Intents.default()
intents.message_content = True # メッセージ内容の読み取り
intents.voice_states = True    # ボイスチャンネルの状態変化
intents.members = True         # メンバー情報へのアクセス (ユーザー名取得などに必要)

# Botの初期化
bot = commands.Bot(command_prefix='!', intents=intents)

# 音声データ保存用のディレクトリは、録音機能を無効化するため不要 (コメントアウト)
# AUDIO_OUTPUT_DIR = "recorded_audio"
# os.makedirs(AUDIO_OUTPUT_DIR, exist_ok=True)

@bot.event
async def on_ready():
    """BotがDiscordに接続したときに呼ばれるイベント"""
    print(f'{bot.user} がDiscordに接続しました！')
    print(f'Bot ID: {bot.user.id}')

@bot.event
async def on_voice_state_update(member, before, after):
    """
    ユーザーのボイスチャンネルの状態が更新されたときに呼ばれるイベント。
    Bot自身の状態変更は無視し、Botが接続しているVCでのユーザーの出入りを監視します。
    """
    if member == bot.user:
        return # Bot自身の状態変更は無視

    # BotがどこかのVCに接続している場合のみ、ユーザーの出入りをチェック
    if bot.voice_clients:
        for vc in bot.voice_clients:
            if before.channel == vc.channel and after.channel != vc.channel:
                # ユーザーがBotと同じVCから退出した場合
                print(f'{member.display_name} が {before.channel.name} から退出しました。')
            elif before.channel != vc.channel and after.channel == vc.channel:
                # ユーザーがBotと同じVCに参加した場合
                print(f'{member.display_name} が {after.channel.name} に参加しました。')

@bot.command()
async def hello(ctx):
    """シンプルなテストコマンド"""
    await ctx.send(f'こんにちは、{ctx.author.display_name}さん！')

# 音声録音機能が無効なため、CustomVoiceRecorderクラスを削除 (またはコメントアウト)
# class CustomVoiceRecorder(Sink): # これがエラーの原因となるため削除
#     ...

@bot.command()
async def join(ctx):
    """
    ボットをユーザーと同じボイスチャンネルに接続するコマンド。
    注意: 現在、音声録音機能は無効です。
    """
    # コマンドを実行したユーザーがボイスチャンネルにいるか確認
    if ctx.author.voice is None:
        await ctx.send("ボイスチャンネルに接続してください。")
        return

    # 既にBotがボイスチャンネルにいる場合は、一度切断して再接続を試みる
    if ctx.voice_client:
        # 音声録音機能が無効なため、録音停止ロジックを削除
        # if ctx.voice_client.is_listening():
        #     ctx.voice_client.stop_listening()
        #     if hasattr(ctx.voice_client, 'recorder_instance') and ctx.voice_client.recorder_instance:
        #         ctx.voice_client.recorder_instance.cleanup()
        #         ctx.voice_client.recorder_instance = None
        #     await ctx.send("既存の録音を停止しました。")
        #     print("既存のボイス録音を停止しました。")
        
        await ctx.voice_client.disconnect() # ボットを切断
        await asyncio.sleep(0.5) # 切断が完了するのを少し待つ

    voice_channel = ctx.author.voice.channel
    
    # ユーザーがいるボイスチャンネルに接続
    vc = await voice_channel.connect()
    await ctx.send(f'ボイスチャンネル **{voice_channel.name}** に接続しました！')
    print(f'Botがボイスチャンネル {voice_channel.name} に接続しました。')

    # 音声録音機能が無効なため、レコーダーのインスタンス化とlisten()の呼び出しを削除
    # recorder = CustomVoiceRecorder(AUDIO_OUTPUT_DIR)
    # vc.listen(recorder)
    # vc.recorder_instance = recorder 
    
    await ctx.send("ボイスチャンネルに接続しましたが、音声録音機能は現在利用できません。")
    print("音声録音機能は現在無効です。")


@bot.command()
async def stop_record(ctx):
    """
    ボイスチャンネルでの音声録音（現在無効）を停止するコマンド。
    """
    await ctx.send("音声録音機能は現在利用できません。")
    print("音声録音機能は現在無効です。")

@bot.command()
async def leave(ctx):
    """
    ボットがボイスチャンネルから切断するコマンド。
    """
    if ctx.voice_client:
        # 音声録音機能が無効なため、録音停止ロジックを削除
        # if ctx.voice_client.is_listening():
        #     ctx.voice_client.stop_listening()
        #     if hasattr(ctx.voice_client, 'recorder_instance') and ctx.voice_client.recorder_instance:
        #         ctx.voice_client.recorder_instance.cleanup()
        await ctx.voice_client.disconnect() # ボットを切断
        await ctx.send("ボイスチャンネルから切断しました。")
        print(f'Botがボイスチャンネルから切断しました。')
    else:
        await ctx.send("ボイスチャンネルに接続していません。")

# Botの実行
if DISCORD_BOT_TOKEN:
    bot.run(DISCORD_BOT_TOKEN)
else:
    print("エラー: Discord Botトークンが設定されていません。'.env'ファイルを確認してください。")
