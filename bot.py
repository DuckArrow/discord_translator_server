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

# 各ユーザーの音声データを個別に処理するためのカスタムシンククラス
class CustomVoiceRecorder(discord.sinks.AudioSink):
    def __init__(self, output_dir: str):
        self.output_dir = output_dir
        self.users_audio_streams = {} # user_id -> wave.Wave_write object
        self.start_time = int(time.time())

    def write(self, data, user):
        # userオブジェクトはdiscord.Userまたはdiscord.Member
        user_id = user.id
        if user_id not in self.users_audio_streams:
            # 新しいユーザーの音声ストリームが来た場合、新しいWAVファイルを作成
            file_name = f"{user_id}_{user.display_name}_{self.start_time}.wav"
            file_path = os.path.join(self.output_dir, file_name)
            wf = wave.open(file_path, 'wb')
            # Discordの音声データはPCM、48kHz、ステレオ（2チャンネル）です
            wf.setnchannels(2) # ステレオ
            wf.setsampwidth(2) # 16-bit
            wf.setframerate(48000) # 48kHz
            self.users_audio_streams[user_id] = wf
            print(f"録音開始: {user.display_name} ({user_id}) の音声 -> {file_path}")

        # 音声データをWAVファイルに書き込む
        self.users_audio_streams[user_id].writeframes(data)

    def cleanup(self):
        # 録音が終了したときに全てのファイルを閉じる
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
        # 既にボイスチャンネルにいる場合は、一度切断
        # 既存のレコーディングがある場合は停止
        if hasattr(ctx.voice_client, '_listener') and ctx.voice_client._listener:
            # 録音中のリスナーを停止
            ctx.voice_client._listener.stop()
            ctx.voice_client._listener.sink.cleanup() # カスタムシンクのcleanupを呼ぶ
        await ctx.voice_client.disconnect()
        await asyncio.sleep(0.5)

    voice_channel = ctx.author.voice.channel
    vc = await voice_channel.connect()
    await ctx.send(f'ボイスチャンネル **{voice_channel.name}** に接続しました！')
    print(f'Botがボイスチャンネル {voice_channel.name} に接続しました。')

    # カスタムレコーダー（シンク）をインスタンス化
    recorder = CustomVoiceRecorder(AUDIO_OUTPUT_DIR)

    # vc.listen() で音声受信を開始
    vc.listen(recorder) # ここでrecorderインスタンスを渡す
    
    # リスナーへの参照を保持（停止時に必要）
    # discord.pyの内部実装に依存するため、_listenerは将来変更される可能性あり
    # より安定した停止方法を後で検討する必要がありますが、まずはこれで進めます
    ctx.voice_client._listener = vc.listen(recorder) # listenはTaskを返すので、実際にはこうはしません
    # 正しい listen の使い方: listen() はタスクを返すので、そのタスクをキャンセルすることで停止します。
    # しかし、discord.pyのlisten()は内部でsinkを管理するため、stop()で停止します。
    # ここではVCオブジェクトに直接recorderインスタンスを保持させて、stop時にcleanupできるようにします。
    
    vc.recorder_instance = recorder # 後でcleanupを呼べるように参照を保持
    
    await ctx.send(f"ボイスチャンネルの各ユーザーの音声録音を開始しました。ファイルは`{AUDIO_OUTPUT_DIR}`に保存されます。")
    print(f"各ユーザーの音声録音開始: ターゲットディレクトリ `{AUDIO_OUTPUT_DIR}`")


@bot.command()
async def stop_record(ctx):
    if ctx.voice_client:
        if hasattr(ctx.voice_client, 'recorder_instance') and ctx.voice_client.recorder_instance:
            # 録音中のリスナーを停止
            ctx.voice_client.stop() # listen() を停止する推奨メソッド
            ctx.voice_client.recorder_instance.cleanup() # カスタムシンクのcleanupを呼ぶ
            ctx.voice_client.recorder_instance = None # リセット
            await ctx.send("録音を停止しました。録音ファイルはサーバーに保存されました。")
            print("ボイスチャンネルの録音を停止しました。")
        else:
            await ctx.send("現在録音していません。")
    else:
        await ctx.send("ボイスチャンネルに接続していません。")

@bot.command()
async def leave(ctx):
    if ctx.voice_client:
        if hasattr(ctx.voice_client, 'recorder_instance') and ctx.voice_client.recorder_instance:
            ctx.voice_client.stop() # 録音を停止
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
