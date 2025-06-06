import os
import discord
from discord.ext import commands
from discord.sinks import Sink # 修正: Sinkをインポート
from dotenv import load_dotenv
import asyncio
import time
import wave # WAVファイル書き込み用

# py-cordのバージョンとsinks機能の確認
print(f"Discord.py version: {discord.__version__}")
print(f"Has sinks module: {hasattr(discord, 'sinks')}")

# discord.sinksが存在するか確認し、AudioSinkのインポートを試みる
try:
    # AudioSinkが正常にインポートできるか試す
    # (CustomVoiceRecorderがAudioSinkを継承する場合に必要)
    from discord.sinks import AudioSink
    print("Successfully imported AudioSink")
except ImportError as e:
    print(f"Failed to import AudioSink: {e}")
    print("Available attributes in discord.sinks:")
    if hasattr(discord, 'sinks'):
        print(dir(discord.sinks))
    else:
        print("discord.sinks module not found")
except AttributeError as e: # `discord.sinks` はあるが `AudioSink` がない場合に対応
    print(f"Failed to access AudioSink: {e}")
    print("Available attributes in discord.sinks:")
    if hasattr(discord, 'sinks'):
        print(dir(discord.sinks))
    else:
        print("discord.sinks module not found")


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

# 音声データ保存用のディレクトリを作成
AUDIO_OUTPUT_DIR = "recorded_audio"
os.makedirs(AUDIO_OUTPUT_DIR, exist_ok=True)

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

# 各ユーザーの音声データを個別に処理するためのカスタムシンククラス
# 修正: discord.sinks.Sink を継承します
# 注意: Sinkを継承すると、writeメソッドにuserオブジェクトが渡されなくなるため、
#      話者ごとの分離はできません。ボイスチャンネル全体の音声がまとめて録音されます。
class CustomVoiceRecorder(Sink):
    """
    ボイスチャンネル全体の音声データをWAVファイルに保存するカスタムシンク。
    注意: このクラスはPycordのdiscord.sinks.Sinkを継承しているため、
          各話者の音声を個別に分離して録音することはできません。
          ボイスチャンネル内の全ユーザーの音声がミキシングされて録音されます。
    """
    def __init__(self, output_dir: str):
        super().__init__() # discord.sinks.Sink のコンストラクタを呼び出す
        self.output_dir = output_dir
        # 単一ファイルに保存するため、辞書ではなく直接ファイルオブジェクトを保持
        self.output_file = None 
        self.start_time = int(time.time()) # 録音開始時のタイムスタンプ (ファイル名用)

    def wants_opus(self) -> bool:
        """
        PycordのAudioSink抽象メソッド。
        Sinkには直接このメソッドは存在しませんが、互換性のため残しています。
        Trueを返すとOPUSデータ、FalseだとPCMデータを受け取ります。
        ここではPCMデータを要求します。
        """
        return False # PCMデータを要求

    # 修正: Sinkのwriteメソッドのシグネチャに合わせてuser引数を削除
    def write(self, data: bytes):
        """
        ボイスチャンネル全体の音声データが到着するたびに呼ばれるメソッド。
        data: デコードされたPCM音声データ (bytes)
        注意: Sinkを継承しているため、userオブジェクトはここには渡されません。
        """
        if self.output_file is None:
            # ファイルがまだ開かれていない場合、新しくWAVファイルを作成
            file_name = f"mixed_audio_{self.start_time}.wav" # 全員分の音声なのでファイル名を変更
            file_path = os.path.join(self.output_dir, file_name)
            
            self.output_file = wave.open(file_path, 'wb')
            # Discordの音声データはPCM、48kHz、ステレオ（2チャンネル）、16bitです
            self.output_file.setnchannels(2)    # ステレオ
            self.output_file.setsampwidth(2)    # 16-bit (2バイト/サンプル)
            self.output_file.setframerate(48000) # 48kHz
            print(f"録音開始: ボイスチャンネル全体の音声 -> {file_path}")

        # 音声データをWAVファイルに書き込む
        self.output_file.writeframes(data)

    def cleanup(self):
        """
        録音が終了したときに呼ばれるクリーンアップメソッド。
        開いているWAVファイルを閉じます。
        """
        if self.output_file:
            self.output_file.close()
            print("録音ファイル閉じました。")
            self.output_file = None


@bot.command()
async def join(ctx):
    """
    ボットをユーザーと同じボイスチャンネルに接続し、音声録音を開始するコマンド。
    """
    # コマンドを実行したユーザーがボイスチャンネルにいるか確認
    if ctx.author.voice is None:
        await ctx.send("ボイスチャンネルに接続してください。")
        return

    # 既にBotがボイスチャンネルにいる場合は、一度切断して再接続を試みる
    if ctx.voice_client:
        # 既存のボイスクライアントが録音中の場合、停止しクリーンアップ
        if ctx.voice_client.is_listening(): # Pycordのis_listening()メソッド
            ctx.voice_client.stop_listening() # Pycordのstop_listening()メソッド
            if hasattr(ctx.voice_client, 'recorder_instance') and ctx.voice_client.recorder_instance:
                ctx.voice_client.recorder_instance.cleanup()
                ctx.voice_client.recorder_instance = None
            await ctx.send("既存の録音を停止しました。")
            print("既存のボイス録音を停止しました。")
        
        await ctx.voice_client.disconnect() # ボットを切断
        await asyncio.sleep(0.5) # 切断が完了するのを少し待つ

    voice_channel = ctx.author.voice.channel
    
    # ユーザーがいるボイスチャンネルに接続
    # PycordではVoiceClientがlistenメソッドを持つため、cls=は不要です
    vc = await voice_channel.connect()
    await ctx.send(f'ボイスチャンネル **{voice_channel.name}** に接続しました！')
    print(f'Botがボイスチャンネル {voice_channel.name} に接続しました。')

    # カスタムレコーダー（シンク）をインスタンス化
    recorder = CustomVoiceRecorder(AUDIO_OUTPUT_DIR)

    # PycordのVoiceClientのlisten() メソッドで音声受信を開始
    vc.listen(recorder)
    
    # recorderインスタンスへの参照をVCオブジェクトに保持 (停止や切断時にcleanupを呼ぶため)
    vc.recorder_instance = recorder 
    
    await ctx.send(f"ボイスチャンネル全体の音声録音を開始しました。ファイルは`{AUDIO_OUTPUT_DIR}`に保存されます。")
    print(f"ボイスチャンネル全体の音声録音開始: ターゲットディレクトリ `{AUDIO_OUTPUT_DIR}`")


@bot.command()
async def stop_record(ctx):
    """
    ボイスチャンネルでの音声録音を停止するコマンド。
    """
    if ctx.voice_client:
        if ctx.voice_client.is_listening(): # 録音中の場合
            ctx.voice_client.stop_listening() # Pycordのstop_listening()メソッド
            if hasattr(ctx.voice_client, 'recorder_instance') and ctx.voice_client.recorder_instance:
                ctx.voice_client.recorder_instance.cleanup()
                ctx.voice_client.recorder_instance = None
            await ctx.send("録音を停止しました。録音ファイルはサーバーに保存されました。")
            print("ボイスチャンネルの録音を停止しました。")
        else:
            await ctx.send("現在録音していません。")
    else:
        await ctx.send("ボイスチャンネルに接続していません。")

@bot.command()
async def leave(ctx):
    """
    ボットがボイスチャンネルから切断するコマンド。
    """
    if ctx.voice_client:
        # 切断前に録音中の場合は停止し、クリーンアップ
        if ctx.voice_client.is_listening():
            ctx.voice_client.stop_listening() # 録音を停止
            if hasattr(ctx.voice_client, 'recorder_instance') and ctx.voice_client.recorder_instance:
                ctx.voice_client.recorder_instance.cleanup()
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
