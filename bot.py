import os
import discord
from discord.ext import commands
from dotenv import load_dotenv
import asyncio
import time
import wave
import io
import json
import tempfile 

from typing import Optional, Dict, Any

# faster-whisperのインポート
from faster_whisper import WhisperModel

# discord-ext-voice-recv の正しいインポート方法
from discord.ext.voice_recv import VoiceRecvClient


# ★★★ 追加: discord.py の詳細ロギングを有効にする ★★★
import logging

# discord.py のロガーを設定
handler = logging.StreamHandler()
handler.setLevel(logging.INFO) # INFOレベル以上のログを出力
formatter = logging.Formatter('%(asctime)s:%(levelname)s:%(name)s: %(message)s')
handler.setFormatter(formatter)
discord.utils.setup_logging(handler=handler, root=False)

# voice_recv のロガーも設定 (必要であればDEBUGに上げてより詳細に)
logging.getLogger('discord.ext.voice_recv').setLevel(logging.DEBUG)
logging.getLogger('discord.voice_state').setLevel(logging.DEBUG)
logging.getLogger('discord.gateway').setLevel(logging.DEBUG)

# .env ファイルから環境変数をロード
load_dotenv()

# 各種トークン・APIキーを取得
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
# OpenAI API キーはローカルWhisperでは不要になります
# GOOGLE_CLOUD_API_KEY = os.getenv("GOOGLE_CLOUD_API_KEY") # 現在は未使用

# Botのインテントを設定
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True # メンバー情報（display_nameなど）取得のために必要

# Botの初期化
bot = commands.Bot(command_prefix='!', intents=intents)

# 音声データ保存用のディレクトリを作成
AUDIO_OUTPUT_DIR = "recorded_audio"
os.makedirs(AUDIO_OUTPUT_DIR, exist_ok=True)

# ユーザー音声プロファイル管理 (現在は未使用、将来の話者識別強化用)
user_voice_profiles: Dict[int, Dict[str, Any]] = {}
# ギルドごとのボイス接続を管理
connections: Dict[int, VoiceRecvClient] = {} # VoiceRecvClient 型を指定

# リアルタイム録音バッファをユーザーごとに管理
# 例: {guild_id: {user_id: bytearray_of_audio_data, ...}}
realtime_audio_buffers: Dict[int, Dict[int, bytearray]] = {}
# ユーザーごとのVAD状態を管理
# 例: {guild_id: {user_id: True/False, ...}}
user_speaking_status: Dict[int, Dict[int, bool]] = {}
# 転写処理の非同期タスクを管理
transcription_tasks: Dict[int, Dict[int, asyncio.Task]] = {}


# faster-whisper モデルのグローバル変数
# Bot起動時に一度だけロード
# モデルサイズを選択 (tiny, base, small, medium, large)
# device="cpu" を指定することでCPUを使用。GPUがある場合は device="cuda"
# compute_type="int8" はより少ないメモリと高速な推論を提供しますが、精度に影響する場合があります。
# より高い精度が必要な場合は "float16" や "float32" を試してください。
WHISPER_MODEL: Optional[WhisperModel] = None
WHISPER_MODEL_SIZE = "base" # 推奨: "base" または "small"
WHISPER_DEVICE = "cpu" # GPUがある場合は "cuda" を試す
WHISPER_COMPUTE_TYPE = "int8" # CPUの場合は "int8" が効率的


class SpeechToTextHandler:
    """Speech to Text (STT) 呼び出しを管理するクラス"""
    
    def __init__(self, whisper_model: WhisperModel):
        self.whisper_model = whisper_model

    async def transcribe_with_local_whisper(self, audio_file_path: str) -> Optional[str]:
        """faster-whisper を使用してローカルで音声をテキストに変換"""
        if self.whisper_model is None:
            print("Whisper モデルがロードされていません。")
            return None
        
        try:
            # faster-whisperで音声を転写
            # language='ja' で日本語を指定、vad_filter=True で無音部分をフィルタリング
            segments, info = self.whisper_model.transcribe(
                audio_file_path, 
                language="ja", 
                beam_size=5, # 推論のビームサイズ
                vad_filter=True # 無音部分のフィルタリングを有効にする
            )
            
            transcription_text = []
            for segment in segments:
                transcription_text.append(segment.text)
            
            return "".join(transcription_text)

        except FileNotFoundError:
            print(f"一時音声ファイルが見つかりません: {audio_file_path}")
            return None
        except Exception as e:
            print(f"ローカルWhisper音声転写エラー: {e}")
            return None

    @staticmethod
    async def transcribe_with_google(audio_file_path: str) -> Optional[str]:
        """Google Cloud Speech-to-Text APIを使用（未実装）"""
        # Google Cloud STT APIの実装は別途必要です。
        # 実際の実装はGoogle Cloud SDKを使用することを推奨
        print("Google Cloud STT APIは現在実装されていません。")
        return None

class RealtimeVoiceDataProcessor:
    """リアルタイム音声データ処理とSTT処理を管理するクラス"""
    
    def __init__(self, output_dir: str, stt_handler: SpeechToTextHandler):
        self.output_dir = output_dir
        self.stt_handler = stt_handler
        self.start_time = int(time.time())

    def identify_speaker(self, user_id: int, user_name: str) -> Dict[str, Any]:
        """話者識別処理（このコードではDiscordユーザーIDを使用）"""
        # DiscordのユーザーIDを直接話者として使用するため、
        # 高度な話者識別アルゴリズムはここに実装する
        speaker_info = {
            'user_id': user_id,
            'user_name': user_name,
            'confidence': 1.0,  # Discord APIから直接得られるユーザーIDなので信頼度100%
            'voice_characteristics': {
                'platform': 'Discord',
                'identification_method': 'discord_user_id'
            }
        }
        return speaker_info

    async def handle_speaking_start(self, guild_id: int, user: discord.Member):
        """ユーザーが話し始めたときの処理"""
        print(f"🎤 {user.display_name} (ID: {user.id}) が話し始めました。")
        if guild_id not in realtime_audio_buffers:
            realtime_audio_buffers[guild_id] = {}
        if user.id not in realtime_audio_buffers[guild_id]:
            realtime_audio_buffers[guild_id][user.id] = bytearray()
        user_speaking_status[guild_id][user.id] = True

    async def handle_speaking_stop(self, guild_id: int, user: discord.Member):
        """ユーザーが話し終えたときの処理"""
        print(f"🔇 {user.display_name} (ID: {user.id}) が話し終えました。")
        user_speaking_status[guild_id][user.id] = False

        if guild_id in realtime_audio_buffers and user.id in realtime_audio_buffers[guild_id]:
            pcm_data = bytes(realtime_audio_buffers[guild_id].pop(user.id))
            if pcm_data:
                # 非同期タスクとして音声処理を実行
                # voice_channel_idは connections[guild_id].channel.id から取得可能
                # テキストチャンネルはギルドのデフォルトチャンネル、またはBotがアクセス可能な任意のチャンネル
                # 例: ctx.channel をどこかから渡すか、bot.get_channel(channel_id) で取得
                # ここでは簡易的に、ボイスチャンネルのあるギルドの任意のテキストチャンネルを取得する
                # 本番環境では、ユーザーが設定できる専用のテキストチャンネルを使用すべき
                text_channel_to_send = None
                guild_obj = bot.get_guild(guild_id)
                if guild_obj:
                    for channel in guild_obj.text_channels:
                        if channel.permissions_for(guild_obj.me).send_messages:
                            text_channel_to_send = channel
                            break
                
                if text_channel_to_send:
                    print(f"DEBUG: Processing audio for {user.display_name} (length: {len(pcm_data)} bytes)") # デバッグ用ログ
                    task = asyncio.create_task(
                        self.process_single_user_audio(
                            pcm_data,
                            user.id,
                            user.display_name,
                            text_channel_to_send # 取得したTextChannelオブジェクトを使用
                        )
                    )
                    if guild_id not in transcription_tasks:
                        transcription_tasks[guild_id] = {}
                    transcription_tasks[guild_id][user.id] = task
                else:
                    print(f"⚠️ ギルド {guild_id} でテキストチャンネルが見つかりませんでした。メッセージを送信できません。")
            else:
                print(f"⚠️ {user.display_name} の音声データが空でした。")
        else:
            print(f"⚠️ {user.display_name} の音声バッファが見つかりませんでした。")

    async def process_single_user_audio(self, pcm_data: bytes, user_id: int, username: str, text_channel: discord.TextChannel):
        """個別のユーザーの音声データを処理（保存、転写、結果送信）"""
        print(f"--- 音声処理開始: {username} ---")
        speaker_info = self.identify_speaker(user_id, username)
        
        temp_audio_path = None
        try:
            temp_audio_path = await self.save_temp_audio_file(pcm_data, user_id, username)
        except Exception as e:
            print(f"一時音声ファイル保存エラー ({username}): {e}")

        transcription = None
        if temp_audio_path:
            transcription = await self.stt_handler.transcribe_with_local_whisper(temp_audio_path)
            try:
                os.remove(temp_audio_path)
                print(f"🗑️ 一時ファイルを削除しました: {temp_audio_path}")
            except Exception as e:
                print(f"一時ファイル削除エラー: {temp_audio_path} - {e}")
        else:
            print(f"❌ {username} の一時音声ファイル保存に失敗しました (temp_audio_path is None)")

        if transcription and transcription.strip():
            await text_channel.send(f"**{username}**: {transcription}")
            await self.save_transcription(user_id, username, transcription, speaker_info)
            print(f"DEBUG: Sent transcription to Discord for {username}") # デバッグ用ログ
        else:
            await text_channel.send(f"**{username}**: _(転写失敗または音声なし)_")
            print(f"❌ {username} の音声転写に失敗しました (空またはNone)")
        print(f"--- 音声処理完了: {username} ---")

    async def save_temp_audio_file(self, pcm_data: bytes, user_id: int, username: str) -> Optional[str]:
        """PCMデータをWAV形式で一時ファイルに保存"""
        try:
            # 一時ファイルを作成
            # NamedTemporaryFileは自動でオープンされるため、クローズしてからパスを使用
            with tempfile.NamedTemporaryFile(delete=False, suffix='.wav') as temp_file:
                temp_path = temp_file.name
                
            # waveモジュールを使用してWAVファイルとして書き込み
            with wave.open(temp_path, 'wb') as wf:
                wf.setnchannels(2)    # ステレオ
                wf.setsampwidth(2)    # 16-bit (2バイト/サンプル)
                wf.setframerate(48000) # 48kHz
                wf.writeframes(pcm_data) # PCMデータを書き込む
                
            print(f"📁 一時ファイル保存: {username} -> {temp_path}")
            return temp_path
            
        except Exception as e:
            print(f"一時ファイル保存エラー ({username}): {e}")
            return None

    async def save_transcription(self, user_id: int, username: str, transcription: str, speaker_info: Dict):
        """転写結果をJSONL形式でファイルに保存"""
        transcript_file = os.path.join(self.output_dir, f"transcriptions_{self.start_time}.jsonl")
        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        
        transcript_entry = {
            'timestamp': timestamp,
            'user_id': user_id,
            'username': username,
            'transcription': transcription,
            'speaker_info': speaker_info
        }
        
        try:
            with open(transcript_file, 'a', encoding='utf-8') as f:
                f.write(f"{json.dumps(transcript_entry, ensure_ascii=False)}\n")
            print(f"💾 転写結果保存: {transcript_file}")
        except Exception as e:
            print(f"転写結果保存エラー: {e}")

# グローバルな音声データプロセッサを更新
realtime_voice_processor = RealtimeVoiceDataProcessor(AUDIO_OUTPUT_DIR, SpeechToTextHandler(None))

# discord-ext-voice-recvのイベントリスナーを追加
@bot.event
async def on_voice_receive(user: discord.Member, audio_data):
    """
    discord-ext-voice-recv からリアルタイムで音声データを受信
    audio_data は VoiceRecvClient.AudioPacket オブジェクトであると想定
    """
    if user.bot: # ボット自身の音声は無視
        return

    guild_id = user.guild.id
    user_id = user.id

    # botが現在録音中であるか、かつそのユーザーが話している状態であるかを確認
    if guild_id in connections and connections[guild_id].is_currently_recording:
        if guild_id not in realtime_audio_buffers:
            realtime_audio_buffers[guild_id] = {}
        if user_id not in realtime_audio_buffers[guild_id]:
            realtime_audio_buffers[guild_id][user.id] = bytearray()
        
        # 音声データをバッファに追加 (audio_data.packet.decrypted_data が生のPCMバイトデータ)
        realtime_audio_buffers[guild_id][user.id].extend(audio_data.packet.decrypted_data)
        # print(f"DEBUG: Received {len(audio_data.packet.decrypted_data)} bytes from {user.display_name}. Buffer size: {len(realtime_audio_buffers[guild_id][user.id])}") # 高頻度で表示されるためコメントアウト

@bot.event
async def on_voice_member_speaking_start(member: discord.Member):
    """メンバーが話し始めたときに呼ばれるイベント (DiscordのVADに基づく)"""
    if member.bot:
        return
    guild_id = member.guild.id
    if guild_id in connections and connections[guild_id].is_currently_recording:
        # VAD状態を更新し、必要な初期化を行う
        if guild_id not in user_speaking_status:
            user_speaking_status[guild_id] = {}
        print(f"DEBUG: on_voice_member_speaking_start for {member.display_name}") # デバッグ用ログ
        await realtime_voice_processor.handle_speaking_start(guild_id, member)

@bot.event
async def on_voice_member_speaking_stop(member: discord.Member):
    """メンバーが話し終えたときに呼ばれるイベント (DiscordのVADに基づく)"""
    if member.bot:
        return
    guild_id = member.guild.id
    if guild_id in connections and connections[guild_id].is_currently_recording:
        print(f"DEBUG: on_voice_member_speaking_stop for {member.display_name}") # デバッグ用ログ
        await realtime_voice_processor.handle_speaking_stop(guild_id, member)


@bot.command()
async def join(ctx):
    """ボットをボイスチャンネルに接続し、リアルタイム音声録音・転写を開始"""
    if ctx.author.voice is None:
        await ctx.send("❌ ボイスチャンネルに接続してください。")
        return

    voice_channel = ctx.author.voice.channel
    
    # 既存の接続があれば切断
    if ctx.guild.id in connections:
        old_vc = connections[ctx.guild.id]
        if hasattr(old_vc, 'is_currently_recording') and old_vc.is_currently_recording:
            # 録音中のユーザーがいれば、その時点までの音声を処理して停止
            if ctx.guild.id in realtime_audio_buffers: # 念のため存在チェック
                for user_id, buffer in list(realtime_audio_buffers[ctx.guild.id].items()): # 変更中に辞書が変更されないようlist()でコピー
                    if buffer: # バッファにデータがあれば処理
                        user = bot.get_user(user_id) or ctx.guild.get_member(user_id)
                        if user:
                            await realtime_voice_processor.process_single_user_audio(
                                bytes(buffer), user.id, user.display_name, ctx.channel
                            )
            realtime_audio_buffers.pop(ctx.guild.id, None) # バッファをクリア
            user_speaking_status.pop(ctx.guild.id, None) # 状態をクリア
            for user_id, task in list(transcription_tasks.get(ctx.guild.id, {}).items()): # 変更中に辞書が変更されないようlist()でコピー
                if not task.done():
                    print(f"未完了の転写タスクをキャンセル: {user_id}")
                    task.cancel() # タスクをキャンセル
            transcription_tasks.pop(ctx.guild.id, None)

        await old_vc.disconnect()
        del connections[ctx.guild.id]
        await asyncio.sleep(0.5) # 切断処理が完全に終わるのを待つ

    # VoiceRecvClient を使用して接続
    # cls=VoiceRecvClient を指定。
    vc = await voice_channel.connect(cls=VoiceRecvClient, reconnect=True) 
    connections[ctx.guild.id] = vc
    vc.is_currently_recording = True # 録音開始フラグをTrueに設定

    # ★★★ 修正箇所: start_receiving() の呼び出しを削除 ★★★
    # try:
    #     await vc.start_receiving() # VoiceRecvClientのstart_receiving()を呼び出す
    #     print("🔊 VoiceRecvClient started explicit receiving.")
    # except Exception as e:
    #     print(f"❌ Error starting explicit receiving: {e}")
    # ★★★ 修正ここまで ★★★

    await ctx.send(f'🎵 ボイスチャンネル **{voice_channel.name}** に接続しました！')
    print(f'Botがボイスチャンネル {voice_channel.name} に接続しました。')

    if not realtime_voice_processor.stt_handler or realtime_voice_processor.stt_handler.whisper_model is None:
        await ctx.send("⚠️ Whisperモデルがロードされていません。STT機能は利用できません。Botのログを確認してください。")
        print("Whisperモデルがロードされていないため、VAD機能なしで録音を開始します。")
    
    await ctx.send(
        f"🎙️ **リアルタイム音声監視・転写を開始しました！**\n"
        f"📁 ファイル保存先: `{AUDIO_OUTPUT_DIR}`\n"
        f"🤖 STT: {'✅ ローカルWhisper' if realtime_voice_processor.stt_handler and realtime_voice_processor.stt_handler.whisper_model else '❌ なし'}\n"
        f"ℹ️ `!leave` で接続を切断できます。"
    )
    print("リアルタイム音声監視開始。")

@bot.command()
async def stop(ctx):
    """
    リアルタイム録音は `!leave` で停止されるため、このコマンドは不要になるか、
    一時停止などの別の機能に変更する必要があります。
    ここでは、現在のセッションの録音状態を終了させるものとして扱います。
    """
    if ctx.guild.id not in connections:
        await ctx.send("❌ ボイスチャンネルに接続していません。")
        return
    
    vc = connections[ctx.guild.id]
    if hasattr(vc, 'is_currently_recording') and vc.is_currently_recording:
        vc.is_currently_recording = False # 録音停止フラグ
        await ctx.send("⏸️ リアルタイム音声監視を一時停止しました。")
        print("リアルタイム音声監視を一時停止しました。")
        
        # 現在バッファリング中の音声があればここで処理
        if ctx.guild.id in realtime_audio_buffers:
            for user_id, pcm_data_buffer in list(realtime_audio_buffers[ctx.guild.id].items()): # コピーしてイテレート
                if pcm_data_buffer:
                    user = bot.get_user(user_id) or ctx.guild.get_member(user_id)
                    if user:
                        await realtime_voice_processor.process_single_user_audio(
                            bytes(pcm_data_buffer), user_id, user.display_name, ctx.channel
                        )
            realtime_audio_buffers[ctx.guild.id].clear() # バッファをクリア
            user_speaking_status[ctx.guild.id].clear() # 状態をクリア
            for user_id, task in list(transcription_tasks.get(ctx.guild.id, {}).items()): # コピーしてイテレート
                if not task.done():
                    print(f"未完了の転写タスクをキャンセル: {user_id}")
                    task.cancel()
            transcription_tasks[ctx.guild.id].clear()
            await ctx.send("✅ 残りの音声処理を完了しました。")
    else:
        await ctx.send("❌ 現在、リアルタイム音声監視は行われていません。")

@bot.command()
async def leave(ctx):
    """ボイスチャンネルから切断し、リアルタイム音声録音を停止"""
    if ctx.guild.id not in connections:
        await ctx.send("❌ ボイスチャンネルに接続していません。")
        return
    
    vc = connections[ctx.guild.id]
    
    # 録音中のユーザーがいれば、その時点までの音声を処理して停止
    if hasattr(vc, 'is_currently_recording') and vc.is_currently_recording:
        vc.is_currently_recording = False
        await ctx.send("🛑 リアルタイム音声監視を停止してボイスチャンネルから切断します...")
        
        if ctx.guild.id in realtime_audio_buffers:
            for user_id, pcm_data_buffer in list(realtime_audio_buffers[ctx.guild.id].items()): # コピーしてイテレート
                if pcm_data_buffer:
                    user = bot.get_user(user_id) or ctx.guild.get_member(user_id)
                    if user:
                        await realtime_voice_processor.process_single_user_audio(
                            bytes(pcm_data_buffer), user.id, user.display_name, ctx.channel
                        )
            realtime_audio_buffers.pop(ctx.guild.id, None) # バッファをクリア
            user_speaking_status.pop(ctx.guild.id, None) # 状態をクリア

        # 実行中の転写タスクがあれば待機またはキャンセル
        if ctx.guild.id in transcription_tasks:
            # list() でコピーすることで、タスク完了時に辞書が変更されてもエラーにならない
            for user_id, task in list(transcription_tasks[ctx.guild.id].items()):
                if not task.done():
                    print(f"未完了の転写タスクを待機: {user_id}")
                    try:
                        await asyncio.wait_for(task, timeout=10.0) # 10秒待機
                    except asyncio.TimeoutError:
                        print(f"転写タスク {user_id} がタイムアウトしました。キャンセルします。")
                        task.cancel()
            transcription_tasks.pop(ctx.guild.id, None)

    await vc.disconnect()
    if ctx.guild.id in connections:
        del connections[ctx.guild.id]
    await ctx.send("👋 ボイスチャンネルから切断しました。")
    print('Botがボイスチャンネルから切断しました。')


@bot.event
async def on_ready():
    """BotがDiscordに接続したときに呼ばれるイベント"""
    print(f'{bot.user} がDiscordに接続しました！')
    print(f'Bot ID: {bot.user.id}')
    global WHISPER_MODEL
    try:
        print(f"Whisperモデル ({WHISPER_MODEL_SIZE}, {WHISPER_DEVICE}, {WHISPER_COMPUTE_TYPE}) をロード中...")
        WHISPER_MODEL = WhisperModel(WHISPER_MODEL_SIZE, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE_TYPE)
        print("Whisperモデルのロードが完了しました。")
        # モデルがロードされたらSTTハンドラーとVoiceDataProcessorを再初期化
        realtime_voice_processor.stt_handler = SpeechToTextHandler(WHISPER_MODEL)
    except Exception as e:
        print(f"Whisperモデルのロードに失敗しました: {e}")
        WHISPER_MODEL = None
    print(f'利用可能なSTT: {"✅ ローカルWhisper" if WHISPER_MODEL else "❌ なし"}')


@bot.event
async def on_voice_state_update(member, before, after):
    """ユーザーのボイスチャンネル状態更新イベント"""
    if member == bot.user:
        return

    # Botが接続しているボイスチャンネルにユーザーが参加/退出した場合を検知
    # (connections辞書でBotが接続中のギルドを追跡)
    if member.guild.id in connections:
        vc = connections[member.guild.id]
        if vc.channel == before.channel and vc.channel != after.channel:
            print(f'{member.display_name} が {before.channel.name} から退出しました。')
        elif vc.channel != before.channel and vc.channel == after.channel:
            print(f'{member.display_name} が {after.channel.name} に参加しました。')

@bot.command()
async def hello(ctx):
    """シンプルなテストコマンド"""
    await ctx.send(f'こんにちは、{ctx.author.display_name}さん！')

@bot.command()
async def register_voice(ctx):
    """ユーザーの音声プロファイルを登録（将来の話者識別用）"""
    # この機能は高度な話者識別アルゴリズムの実装が必要です。
    # 現在のコードではDiscordユーザーIDを話者として利用しています。
    await ctx.send("🎤 このコマンドは、高度な話者識別機能が実装された際に利用できます。")

@bot.command()
async def status(ctx):
    """現在の録音状況を確認"""
    if ctx.guild.id not in connections:
        await ctx.send("❌ ボイスチャンネルに接続していません。")
        return
    
    vc = connections[ctx.guild.id]
    # カスタムフラグで録音中か確認
    if hasattr(vc, 'is_currently_recording') and vc.is_currently_recording:
        channel_members = len(vc.channel.members) - 1  # Bot自身を除く
        await ctx.send(f"📊 録音中です。チャンネル内のメンバー数: {channel_members}人。")
    else:
        await ctx.send("⏸️ 現在録音していません。")

@bot.command()
async def test_stt(ctx):
    """STT機能の接続テスト"""
    if WHISPER_MODEL:
        await ctx.send(f"✅ ローカルWhisperモデル ({WHISPER_MODEL_SIZE}, {WHISPER_DEVICE}) 設定済み - STT機能が利用可能です。")
    else:
        await ctx.send("❌ STT機能が利用できません。Whisperモデルのロードに失敗している可能性があります。")

# Botの実行
if DISCORD_BOT_TOKEN:
    bot.run(DISCORD_BOT_TOKEN)
else:
    print("エラー: Discord Botトークンが設定されていません。'.env'ファイルを確認してください。")

