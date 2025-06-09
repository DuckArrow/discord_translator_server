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
import threading
import queue
from collections import deque
from typing import Optional, Dict, Any, List
import numpy as np

# faster-whisperのインポート
from faster_whisper import WhisperModel

# discord-ext-voice-recv の正しいインポート方法
from discord.ext.voice_recv import VoiceRecvClient
from discord.ext.voice_recv import AudioSink
from discord.ext.voice_recv import VoiceData

# VAD (Voice Activity Detection) 用
import webrtcvad

# ★★★ 新しい設定 ★★★
# リアルタイム性向上のための設定
REALTIME_CHUNK_DURATION_MS = 500  # 500ms（0.5秒）でチャンク処理
VAD_AGGRESSIVENESS = 2  # VADの感度 (0-3, 3が最も厳格)
MIN_SPEECH_DURATION_MS = 300  # 最小発話時間（300ms）
SILENCE_THRESHOLD_MS = 800  # 無音時間がこれを超えると発話終了とみなす
OVERLAP_DURATION_MS = 200  # チャンク間のオーバーラップ

# 音声品質設定（16kHzに変更してWhisperの処理を高速化）
WHISPER_SAMPLE_RATE = 16000  # Whisperの推奨サンプルレート
PCM_SAMPLE_RATE = 48000  # Discordの音声データ
PCM_BYTES_PER_SAMPLE = 2
PCM_CHANNELS = 2

# 計算用定数
REALTIME_CHUNK_SAMPLES = int(WHISPER_SAMPLE_RATE * REALTIME_CHUNK_DURATION_MS / 1000)
REALTIME_CHUNK_BYTES = REALTIME_CHUNK_SAMPLES * 2  # 16-bit mono

# ★★★ 従来の設定（コメントアウト） ★★★
# TRANSCRIPTION_CHUNK_DURATION_SECONDS = 2
# TRANSCRIPTION_CHUNK_BYTES = PCM_SAMPLE_RATE * PCM_BYTES_PER_SAMPLE * PCM_CHANNELS * TRANSCRIPTION_CHUNK_DURATION_SECONDS

# .env ファイルから環境変数をロード
load_dotenv()

DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")

# Botのインテントを設定
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True

# Botの初期化
bot = commands.Bot(command_prefix='!', intents=intents)

# 音声データ保存用のディレクトリを作成
AUDIO_OUTPUT_DIR = "recorded_audio"
os.makedirs(AUDIO_OUTPUT_DIR, exist_ok=True)

# ギルドごとのボイス接続を管理
connections: Dict[int, VoiceRecvClient] = {}

# Whisperモデルのグローバル変数
WHISPER_MODEL: Optional[WhisperModel] = None
WHISPER_MODEL_SIZE = "base"
WHISPER_DEVICE = "cpu"
WHISPER_COMPUTE_TYPE = "int8"


class AudioUtils:
    """音声データ処理のユーティリティクラス"""
    
    @staticmethod
    def resample_audio(audio_data: bytes, original_rate: int, target_rate: int) -> bytes:
        """音声データを16kHzにリサンプリング（簡易版）"""
        if original_rate == target_rate:
            return audio_data
        
        # NumPyを使用してリサンプリング
        audio_array = np.frombuffer(audio_data, dtype=np.int16)
        
        # ステレオからモノラルに変換
        if len(audio_array) % 2 == 0:
            audio_array = audio_array.reshape(-1, 2).mean(axis=1).astype(np.int16)
        
        # 簡易リサンプリング（より高品質なライブラリを使用することを推奨）
        ratio = target_rate / original_rate
        new_length = int(len(audio_array) * ratio)
        resampled = np.interp(
            np.linspace(0, len(audio_array) - 1, new_length),
            np.arange(len(audio_array)),
            audio_array
        ).astype(np.int16)
        
        return resampled.tobytes()
    
    @staticmethod
    def apply_vad(audio_data: bytes, sample_rate: int, vad_aggressiveness: int = 2) -> bool:
        """WebRTC VADを使用して音声活動を検出"""
        try:
            vad = webrtcvad.Vad(vad_aggressiveness)
            
            # VADは特定のサンプルレートとフレームサイズに対応
            if sample_rate not in [8000, 16000, 32000, 48000]:
                return True  # サポートされていないサンプルレートの場合は音声ありとみなす
            
            # フレームサイズ（10ms, 20ms, 30ms）
            frame_duration_ms = 30
            frame_size = int(sample_rate * frame_duration_ms / 1000) * 2  # 16-bit
            
            if len(audio_data) < frame_size:
                return False
                
            # フレームを切り出してVADを適用
            for i in range(0, len(audio_data) - frame_size + 1, frame_size):
                frame = audio_data[i:i + frame_size]
                if vad.is_speech(frame, sample_rate):
                    return True
            
            return False
        except Exception as e:
            print(f"VADエラー: {e}")
            return True  # エラーの場合は音声ありとみなす


class RealtimeTranscriptionEngine:
    """リアルタイム文字起こしエンジン"""
    
    def __init__(self, whisper_model: WhisperModel, output_dir: str):
        self.whisper_model = whisper_model
        self.output_dir = output_dir
        self.transcription_queue = queue.Queue()
        self.result_queue = queue.Queue()
        self.is_running = False
        self.worker_thread = None
        
    def start(self):
        """バックグラウンド処理を開始"""
        if not self.is_running:
            self.is_running = True
            self.worker_thread = threading.Thread(target=self._transcription_worker, daemon=True)
            self.worker_thread.start()
            print("リアルタイム文字起こしエンジンを開始しました")
    
    def stop(self):
        """バックグラウンド処理を停止"""
        self.is_running = False
        if self.worker_thread:
            self.worker_thread.join(timeout=5.0)
            print("リアルタイム文字起こしエンジンを停止しました")
    
    def submit_audio(self, audio_data: bytes, user_id: int, username: str, guild_id: int):
        """音声データを処理キューに追加"""
        if self.is_running:
            self.transcription_queue.put({
                'audio_data': audio_data,
                'user_id': user_id,
                'username': username,
                'guild_id': guild_id,
                'timestamp': time.time()
            })
    
    def get_result(self) -> Optional[Dict]:
        """処理結果を取得"""
        try:
            return self.result_queue.get_nowait()
        except queue.Empty:
            return None
    
    def _transcription_worker(self):
        """バックグラウンドで文字起こしを処理するワーカー"""
        while self.is_running:
            try:
                # タスクを取得（0.1秒でタイムアウト）
                task = self.transcription_queue.get(timeout=0.1)
                
                # 音声データをWAVファイルに変換
                audio_data = task['audio_data']
                if len(audio_data) < REALTIME_CHUNK_BYTES:
                    continue
                
                # 一時ファイルを作成
                with tempfile.NamedTemporaryFile(delete=False, suffix='.wav') as temp_file:
                    temp_path = temp_file.name
                
                try:
                    # WAVファイルとして保存
                    with wave.open(temp_path, 'wb') as wf:
                        wf.setnchannels(1)  # モノラル
                        wf.setsampwidth(2)  # 16-bit
                        wf.setframerate(WHISPER_SAMPLE_RATE)
                        wf.writeframes(audio_data)
                    
                    # Whisperで文字起こし
                    if self.whisper_model:
                        segments, info = self.whisper_model.transcribe(
                            temp_path,
                            language="ja",
                            beam_size=1,  # 高速化のためビームサイズを1に
                            vad_filter=True,
                            no_speech_threshold=0.6,
                            condition_on_previous_text=False  # 前のテキストに依存しない
                        )
                        
                        transcription = ""
                        for segment in segments:
                            transcription += segment.text
                        
                        if transcription.strip():
                            self.result_queue.put({
                                'user_id': task['user_id'],
                                'username': task['username'],
                                'guild_id': task['guild_id'],
                                'transcription': transcription.strip(),
                                'timestamp': task['timestamp']
                            })
                
                finally:
                    # 一時ファイルを削除
                    try:
                        os.remove(temp_path)
                    except:
                        pass
            
            except queue.Empty:
                continue
            except Exception as e:
                print(f"文字起こし処理エラー: {e}")


class StreamingAudioBuffer:
    """ストリーミング音声バッファ"""
    
    def __init__(self, user_id: int, username: str):
        self.user_id = user_id
        self.username = username
        self.audio_buffer = deque()
        self.last_speech_time = 0
        self.is_speaking = False
        self.accumulated_audio = bytearray()
        
    def add_audio(self, audio_data: bytes):
        """音声データを追加"""
        current_time = time.time()
        
        # 16kHzにリサンプリング
        resampled_audio = AudioUtils.resample_audio(
            audio_data, PCM_SAMPLE_RATE, WHISPER_SAMPLE_RATE
        )
        
        # VADで音声活動を検出
        has_speech = AudioUtils.apply_vad(resampled_audio, WHISPER_SAMPLE_RATE, VAD_AGGRESSIVENESS)
        
        if has_speech:
            self.last_speech_time = current_time
            self.is_speaking = True
            self.accumulated_audio.extend(resampled_audio)
        else:
            # 無音時間が閾値を超えた場合
            if self.is_speaking and (current_time - self.last_speech_time) > (SILENCE_THRESHOLD_MS / 1000):
                self.is_speaking = False
                return True  # 発話終了を示す
            elif self.is_speaking:
                # まだ発話中の場合は無音部分も含める
                self.accumulated_audio.extend(resampled_audio)
        
        return False
    
    def get_audio_chunk(self) -> Optional[bytes]:
        """音声チャンクを取得"""
        if len(self.accumulated_audio) >= REALTIME_CHUNK_BYTES:
            chunk = bytes(self.accumulated_audio[:REALTIME_CHUNK_BYTES])
            # オーバーラップのために一部データを残す
            overlap_bytes = int(REALTIME_CHUNK_BYTES * OVERLAP_DURATION_MS / REALTIME_CHUNK_DURATION_MS)
            self.accumulated_audio = self.accumulated_audio[REALTIME_CHUNK_BYTES - overlap_bytes:]
            return chunk
        return None
    
    def get_remaining_audio(self) -> Optional[bytes]:
        """残りの音声データを取得"""
        if len(self.accumulated_audio) > 0:
            chunk = bytes(self.accumulated_audio)
            self.accumulated_audio.clear()
            return chunk
        return None


class RealtimeVoiceProcessor:
    """リアルタイム音声プロセッサー"""
    
    def __init__(self, transcription_engine: RealtimeTranscriptionEngine):
        self.transcription_engine = transcription_engine
        self.audio_buffers: Dict[int, Dict[int, StreamingAudioBuffer]] = {}  # guild_id -> user_id -> buffer
        self.text_channels: Dict[int, discord.TextChannel] = {}  # guild_id -> channel
        self.result_polling_tasks: Dict[int, asyncio.Task] = {}  # guild_id -> task
        
    def start_processing(self, guild_id: int, text_channel: discord.TextChannel):
        """処理を開始"""
        self.text_channels[guild_id] = text_channel
        if guild_id not in self.audio_buffers:
            self.audio_buffers[guild_id] = {}
        
        # 結果ポーリングタスクを開始
        if guild_id not in self.result_polling_tasks or self.result_polling_tasks[guild_id].done():
            self.result_polling_tasks[guild_id] = asyncio.create_task(
                self._result_polling_loop(guild_id)
            )
        
        self.transcription_engine.start()
        print(f"リアルタイム音声処理を開始: Guild {guild_id}")
    
    def stop_processing(self, guild_id: int):
        """処理を停止"""
        if guild_id in self.result_polling_tasks:
            self.result_polling_tasks[guild_id].cancel()
            del self.result_polling_tasks[guild_id]
        
        if guild_id in self.audio_buffers:
            del self.audio_buffers[guild_id]
        
        if guild_id in self.text_channels:
            del self.text_channels[guild_id]
        
        print(f"リアルタイム音声処理を停止: Guild {guild_id}")
    
    def process_audio(self, guild_id: int, user_id: int, username: str, audio_data: bytes):
        """音声データを処理"""
        if guild_id not in self.audio_buffers:
            return
        
        if user_id not in self.audio_buffers[guild_id]:
            self.audio_buffers[guild_id][user_id] = StreamingAudioBuffer(user_id, username)
        
        buffer = self.audio_buffers[guild_id][user_id]
        
        # 音声データを追加
        speech_ended = buffer.add_audio(audio_data)
        
        # チャンクが準備できているか確認
        chunk = buffer.get_audio_chunk()
        if chunk:
            self.transcription_engine.submit_audio(chunk, user_id, username, guild_id)
        
        # 発話が終了した場合、残りのデータを処理
        if speech_ended:
            remaining = buffer.get_remaining_audio()
            if remaining and len(remaining) >= REALTIME_CHUNK_BYTES // 2:  # 半分以上のデータがある場合
                self.transcription_engine.submit_audio(remaining, user_id, username, guild_id)
    
    async def _result_polling_loop(self, guild_id: int):
        """結果ポーリングループ"""
        while True:
            try:
                result = self.transcription_engine.get_result()
                if result and result['guild_id'] == guild_id:
                    text_channel = self.text_channels.get(guild_id)
                    if text_channel:
                        await text_channel.send(f"**{result['username']}**: {result['transcription']}")
                        print(f"文字起こし送信: {result['username']} -> {result['transcription']}")
                
                await asyncio.sleep(0.05)  # 50msごとにチェック
            except Exception as e:
                print(f"結果ポーリングエラー: {e}")
                break


# グローバル変数
transcription_engine: Optional[RealtimeTranscriptionEngine] = None
voice_processor: Optional[RealtimeVoiceProcessor] = None


class OptimizedAudioSink(AudioSink):
    """最適化された音声シンク"""
    
    def __init__(self, processor: RealtimeVoiceProcessor, guild_id: int):
        super().__init__()
        self.processor = processor
        self.guild_id = guild_id
        
    def write(self, user: discord.Member, data: VoiceData):
        """音声データを受信"""
        if user.bot:
            return
        
        # リアルタイム処理に音声データを送信
        self.processor.process_audio(
            self.guild_id, 
            user.id, 
            user.display_name, 
            data.pcm
        )
    
    def wants_opus(self) -> bool:
        return False
    
    def cleanup(self):
        pass


# Botコマンド
@bot.command()
async def join(ctx):
    """ボットをボイスチャンネルに接続"""
    if ctx.author.voice is None:
        await ctx.send("❌ ボイスチャンネルに接続してください。")
        return

    voice_channel = ctx.author.voice.channel
    
    # 既存の接続があれば切断
    if ctx.guild.id in connections:
        await connections[ctx.guild.id].disconnect()
        voice_processor.stop_processing(ctx.guild.id)
        del connections[ctx.guild.id]
        await asyncio.sleep(0.5)

    # VoiceRecvClient を使用して接続
    vc = await voice_channel.connect(cls=VoiceRecvClient, reconnect=True)
    connections[ctx.guild.id] = vc

    # 音声シンクを設定
    vc.listen(OptimizedAudioSink(voice_processor, ctx.guild.id))
    
    # リアルタイム処理を開始
    voice_processor.start_processing(ctx.guild.id, ctx.channel)

    await ctx.send(f'🎵 ボイスチャンネル **{voice_channel.name}** に接続しました！')
    await ctx.send(
        f"🎙️ **リアルタイム音声文字起こしを開始しました！**\n"
        f"⚡ 処理間隔: {REALTIME_CHUNK_DURATION_MS}ms\n"
        f"🎯 VAD感度: {VAD_AGGRESSIVENESS}/3\n"
        f"🔊 音声検出閾値: {SILENCE_THRESHOLD_MS}ms\n"
        f"ℹ️ `!leave` で接続を切断できます。"
    )


@bot.command()
async def leave(ctx):
    """ボイスチャンネルから切断"""
    if ctx.guild.id not in connections:
        await ctx.send("❌ ボイスチャンネルに接続していません。")
        return
    
    voice_processor.stop_processing(ctx.guild.id)
    await connections[ctx.guild.id].disconnect()
    del connections[ctx.guild.id]
    await ctx.send("👋 ボイスチャンネルから切断しました。")


@bot.command()
async def status(ctx):
    """現在の状況を確認"""
    if ctx.guild.id not in connections:
        await ctx.send("❌ ボイスチャンネルに接続していません。")
        return
    
    vc = connections[ctx.guild.id]
    channel_members = len(vc.channel.members) - 1
    
    status_msg = f"📊 **リアルタイム文字起こし稼働中**\n"
    status_msg += f"🎤 チャンネル参加者: {channel_members}人\n"
    status_msg += f"⚡ 処理間隔: {REALTIME_CHUNK_DURATION_MS}ms\n"
    status_msg += f"🎯 VAD感度: {VAD_AGGRESSIVENESS}/3\n"
    status_msg += f"🔊 無音閾値: {SILENCE_THRESHOLD_MS}ms"
    
    await ctx.send(status_msg)


@bot.event
async def on_ready():
    """Bot起動時の初期化"""
    print(f'{bot.user} がDiscordに接続しました！')
    
    global WHISPER_MODEL, transcription_engine, voice_processor
    try:
        print(f"Whisperモデル ({WHISPER_MODEL_SIZE}) をロード中...")
        WHISPER_MODEL = WhisperModel(WHISPER_MODEL_SIZE, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE_TYPE)
        print("✅ Whisperモデルのロードが完了しました。")
        
        # エンジンとプロセッサーを初期化
        transcription_engine = RealtimeTranscriptionEngine(WHISPER_MODEL, AUDIO_OUTPUT_DIR)
        voice_processor = RealtimeVoiceProcessor(transcription_engine)
        
        print("✅ リアルタイム文字起こしシステムの初期化が完了しました。")
    except Exception as e:
        print(f"❌ Whisperモデルのロードに失敗しました: {e}")


# Botの実行
if DISCORD_BOT_TOKEN:
    bot.run(DISCORD_BOT_TOKEN)
else:
    print("エラー: Discord Botトークンが設定されていません。")
