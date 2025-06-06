import os
import discord
from discord.ext import commands
from dotenv import load_dotenv
import asyncio
import time
import wave # WAVファイル書き込み用

# discord-ext-voice-recv から VoiceRecvClient と AudioSink をインポート
from discord.ext import voice_recv

load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")

intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True

# ボットの初期化時に VoiceRecvClient を使うように指定
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
    if bot.voice_clients:
        for vc in bot.voice_clients:
            # voice_recv.VoiceRecvClient インスタンスかどうか確認
            if isinstance(vc, voice_recv.VoiceRecvClient):
                if before.channel == vc.channel and after.channel != vc.channel:
                    print(f'{member.display_name} が {before.channel.name} から退出しました。')
                elif before.channel != vc.channel and after.channel == vc.channel:
                    print(f'{member.display_name} が {after.channel.name} に参加しました。')


@bot.command()
async def hello(ctx):
    await ctx.send(f'こんにちは、{ctx.author.display_name}さん！')

# 各ユーザーの音声データを個別に処理するためのカスタムシンククラス
# discord.ext.voice_recv.AudioSink を継承する
class CustomVoiceRecorder(voice_recv.AudioSink):
    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        self.users_audio_streams = {} # user_id -> wave.Wave_write object
        self.start_time = int(time.time())

    # voice_recv.AudioSink の write メソッドは、PCM データと user オブジェクトを受け取る
    def write(self, data, user):
        user_id = user.id
        if user_id not in self.users_audio_streams:
            file_name = f"{user_id}_{user.display_name}_{self.start_time}.wav"
            file_path = os.path.join(self.output_dir, file_name)

            wf = wave.open(file_path, 'wb')
            # Discordの音声データはPCM、48kHz、ステレオ（2チャンネル）、16bitです
            wf.setnchannels(2) # ステレオ
            wf.setsampwidth(2) # 16-bit = 2 bytes
            wf.setframerate(48000) # 48kHz
            self.users_audio_streams[user_id] = wf
            print(f"録音開始: {user.display_name} ({user_id}) の音声 -> {file_path}")

        self.users_audio_streams[user_id].writeframes(data)

    def cleanup(self):
        print("すべてのユーザーの音声レコーダーをクリーンアップ中...")
        for user_id, wf in self.users_audio_streams.items():
            wf.close()
            print(f"ユーザー {user_id} のWAVファイルを閉じました。")
        self.users_audio_streams.clear()


# ボイスチャンネルへの接続と個別音声の録音開始
@bot.command()
async def join(ctx):
    if ctx.author.voice is None:
        await ctx.send("ボイスチャンネルに接続してください。")
        return

    if ctx.voice_client:
        # 既存のボイスクライアントが voice_recv.VoiceRecvClient の場合のみ処理
        if isinstance(ctx.voice_client, voice_recv.VoiceRecvClient):
            if ctx.voice_client.is_listening(): # 録音中の場合
                ctx.voice_client.stop_listening() # voice_recv の stop_listening メソッド
                if hasattr(ctx.voice_client, 'recorder_instance') and ctx.voice_client.recorder_instance:
                    ctx.voice_client.recorder_instance.cleanup()
                    ctx.voice_client.recorder_instance = None
                await ctx.send("既存の録音を停止しました。")
                print("既存のボイス録音を停止しました。")
        await ctx.voice_client.disconnect()
        await asyncio.sleep(0.5)

    voice_channel = ctx.author.voice.channel

    # connect メソッドの cls 引数に voice_recv.VoiceRecvClient を渡す
    vc = await voice_channel.connect(cls=voice_recv.VoiceRecvClient)
    await ctx.send(f'ボイスチャンネル **{voice_channel.name}** に接続しました！')
    print(f'Botがボイスチャンネル {voice_channel.name} に接続しました。')

    # カスタムレコーダー（シンク）をインスタンス化
    recorder = CustomVoiceRecorder(AUDIO_OUTPUT_DIR)

    # voice_recv.VoiceRecvClient の listen() メソッドを使用
    vc.listen(recorder)

    # recorderインスタンスへの参照を保持 (stop_recordやleaveでcleanupを呼ぶため)
    vc.recorder_instance = recorder 

    await ctx.send(f"ボイスチャンネルの各ユーザーの音声録音を開始しました。ファイルは`{AUDIO_OUTPUT_DIR}`に保存されます。")
    print(f"各ユーザーの音声録音開始: ターゲットディレクトリ `{AUDIO_OUTPUT_DIR}`")


@bot.command()
async def stop_record(ctx):
    if ctx.voice_client:
        if isinstance(ctx.voice_client, voice_recv.VoiceRecvClient):
            if ctx.voice_client.is_listening(): # 録音中の場合
                ctx.voice_client.stop_listening() # voice_recv の stop_listening メソッド
                if hasattr(ctx.voice_client, 'recorder_instance') and ctx.voice_client.recorder_instance:
                    ctx.voice_client.recorder_instance.cleanup()
                    ctx.voice_client.recorder_instance = None
                await ctx.send("録音を停止しました。録音ファイルはサーバーに保存されました。")
                print("ボイスチャンネルの録音を停止しました。")
            else:
                await ctx.send("現在録音していません。")
        else:
            await ctx.send("BotのVoiceClientが音声受信に対応していません。`!join`で再接続してみてください。")
    else:
        await ctx.send("ボイスチャンネルに接続していません。")

@bot.command()
async def leave(ctx):
    if ctx.voice_client:
        if isinstance(ctx.voice_client, voice_recv.VoiceRecvClient):
            if ctx.voice_client.is_listening():
                ctx.voice_client.stop_listening() # 録音を停止
                if hasattr(ctx.voice_client, 'recorder_instance') and ctx.voice_client.recorder_instance:
                    ctx.voice_client.recorder_instance.cleanup()
        await ctx.voice_client.disconnect()
        await ctx.send("ボイスチャンネルから切断しました。")
        print(f'Botがボイスチャンネルから切断しました。')
    else:
        await ctx.send("ボイスチャンネルに接続していません。")

if DISCORD_BOT_TOKEN:
    bot.run(DISCORD_BOT_TOKEN)
else:
    print("エラー: Discord Botトークンが設定されていません。'.env'ファイルを確認してください。")
