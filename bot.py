import os
import discord
from discord.ext import commands
from dotenv import load_dotenv
import asyncio
import time
import wave
import io
import json
import tempfile # 一時ファイル作成・管理用
import threading # 文字起こし処理を別スレッドで実行
import queue # スレッド間のデータ通信用
from collections import deque # 効率的なバッファリング用
from typing import Optional, Dict, Any, List
import numpy as np # 音声リサンプリング用
import shutil # ファイルコピー用

# faster-whisperのインポート
from faster_whisper import WhisperModel

# discord-ext-voice-recv の正しいインポート方法
from discord.ext.voice_recv import VoiceRecvClient
from discord.ext.voice_recv import AudioSink # AudioSinkをインポートします
from discord.ext.voice_recv import VoiceData # VoiceDataの型ヒントのためにインポート

# VAD (Voice Activity Detection) 用
import webrtcvad

# ★★★ 設定 ★★★
REALTIME_CHUNK_DURATION_MS = 1200  # 1200ms（1.2秒）
VAD_AGGRESSIVENESS = 0  # WebRTC VADの感度を調整 (0-3, 0が最も寛容)
MIN_SPEECH_DURATION_MS = 300  # 最小発話時間（300ms）
SILENCE_THRESHOLD_MS = 1000 # 無音時間がこれを超えると発話終了とみなす
OVERLAP_DURATION_MS = 200  # チャンク間のオーバーラップ

# 音声品質設定（16kHzに変更してWhisperの処理を高速化）
WHISPER_SAMPLE_RATE = 16000  # Whisperの推奨サンプルレート
PCM_SAMPLE_RATE = 48000  # Discordの音声データ
PCM_BYTES_PER_SAMPLE = 2
PCM_CHANNELS = 2

# 計算用定数
REALTIME_CHUNK_SAMPLES = int(WHISPER_SAMPLE_RATE * REALTIME_CHUNK_DURATION_MS / 1000)
REALTIME_CHUNK_BYTES = REALTIME_CHUNK_SAMPLES * 2  # 16-bit mono (2 bytes per sample)

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

# デバッグ用音声保存設定
SAVE_DEBUG_AUDIO = False
DEBUG_AUDIO_SAVE_DIR = os.path.join(AUDIO_OUTPUT_DIR, "debug_recordings")
os.makedirs(DEBUG_AUDIO_SAVE_DIR, exist_ok=True) # Ensure directory exists

# ギルドごとのボイス接続を管理
connections: Dict[int, VoiceRecvClient] = {}

# Whisperモデルのグローバル変数
WHISPER_MODEL: Optional[WhisperModel] = None
WHISPER_MODEL_SIZE = "small" 
WHISPER_DEVICE = "cpu"
WHISPER_COMPUTE_TYPE = "int8"

# 抑制するフレーズのリストを定義
# 環境音などで誤認識されやすいフィラーワードや、出力したくないフレーズをここにリストアップします。
HALLUCINATION_TEXTS = [
    "ご視聴ありがとうございました", "ご視聴ありがとうございました。",
    "ありがとうございました", "ありがとうございました。",
    "どうもありがとうございました", "どうもありがとうございました。",
    "どうも、ありがとうございました", "どうも、ありがとうございました。",
    "おやすみなさい", "おやすみなさい。",
    "Thanks for watching!",
    "終わり", "おわり",
    "お疲れ様でした", "お疲れ様でした。",
]


class AudioUtils:
    """音声データ処理のユーティリティクラス"""
    
    @staticmethod
    def resample_audio(audio_data: bytes, original_rate: int, target_rate: int) -> bytes:
        """音声データを指定のサンプルレートにリサンプリングし、モノラルに変換（簡易版）"""
        # Discordからのデータはステレオなので、モノラルに変換
        audio_array = np.frombuffer(audio_data, dtype=np.int16)
        
        # ステレオの場合、左右のチャンネルの平均を取ってモノラルに変換
        if audio_array.shape[0] % PCM_CHANNELS == 0:
            audio_array = audio_array.reshape(-1, PCM_CHANNELS).mean(axis=1).astype(np.int16)
        
        # リサンプリングが必要な場合
        if original_rate != target_rate:
            # 簡易リサンプリング（より高品質なライブラリを使用することを推奨）
            ratio = target_rate / original_rate
            new_length = int(len(audio_array) * ratio)
            resampled = np.interp(
                np.linspace(0, len(audio_array) - 1, new_length),
                np.arange(len(audio_array)),
                audio_array
            ).astype(np.int16)
            return resampled.tobytes()
        else:
            return audio_array.tobytes()
    
    @staticmethod
    def apply_vad(audio_data: bytes, sample_rate: int, vad_aggressiveness: int = 2) -> bool:
        """WebRTC VADを使用して音声活動を検出"""
        try:
            vad = webrtcvad.Vad(vad_aggressiveness)
            
            # VADは特定のサンプルレートとフレームサイズに対応
            if sample_rate not in [8000, 16000, 32000, 48000]:
                print(f"DEBUG VAD: Unsupported sample rate {sample_rate}. Assuming speech.")
                return True  # サポートされていないサンプルレートの場合は音声ありとみなす
            
            # フレームサイズ（10ms, 20ms, 30ms）
            # WebRTC VADは、これらのいずれかのフレームサイズで処理する必要があります。
            # ここでは30msのフレームを使用します。
            frame_duration_ms = 30
            frame_size = int(sample_rate * frame_duration_ms / 1000) * 2  # 16-bit mono
            
            if len(audio_data) < frame_size:
                # フレームサイズよりもデータが短い場合は、音声なしと判断
                return False
                
            # フレームを切り出してVADを適用
            is_any_speech = False
            for i in range(0, len(audio_data) - frame_size + 1, frame_size):
                frame = audio_data[i:i + frame_size]
                if vad.is_speech(frame, sample_rate):
                    is_any_speech = True
                    break # 音声が検出されたらすぐにループを抜ける
            
            return is_any_speech
        except Exception as e:
            print(f"DEBUG VAD: VADエラー: {e}. 音声ありとみなします。")
            return True  # エラーの場合は音声ありとみなす


class RealtimeTranscriptionEngine:
    """リアルタイム文字起こしエンジン"""
    
    def __init__(self, whisper_model: WhisperModel, output_dir: str):
        self.whisper_model = whisper_model
        self.output_dir = output_dir
        self.transcription_queue = queue.Queue() # 音声データをワーカーに送るキュー
        self.result_queue = queue.Queue() # ワーカーから結果を受け取るキュー
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
        if self.worker_thread and self.worker_thread.is_alive():
            self.worker_thread.join(timeout=5.0) # ワーカーが終了するのを待機
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
            print(f"DEBUG TranscriptionEngine: Submitted {len(audio_data)} bytes for {username} to queue.")
        
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
                
                audio_data = task['audio_data']
                if len(audio_data) < REALTIME_CHUNK_BYTES // 2: # チャンクの半分未満はスキップ
                    print(f"DEBUG Worker: Skipping too small audio chunk for {task['username']} ({len(audio_data)} bytes).")
                    continue
                
                # 一時ファイルを作成
                with tempfile.NamedTemporaryFile(delete=False, suffix='.wav') as temp_file:
                    temp_path = temp_file.name
                
                try:
                    # WAVファイルとして保存 (モノラル, 16kHz)
                    with wave.open(temp_path, 'wb') as wf:
                        wf.setnchannels(1)  # モノラル
                        wf.setsampwidth(2)  # 16-bit
                        wf.setframerate(WHISPER_SAMPLE_RATE)
                        wf.writeframes(audio_data)
                    
                    # デバッグ用音声保存が有効な場合、コピーを保存
                    global SAVE_DEBUG_AUDIO, DEBUG_AUDIO_SAVE_DIR
                    if SAVE_DEBUG_AUDIO:
                        debug_save_filename = f"user_{task['user_id']}_{int(time.time())}.wav"
                        debug_save_path = os.path.join(DEBUG_AUDIO_SAVE_DIR, debug_save_filename)
                        shutil.copy(temp_path, debug_save_path)
                        print(f"DEBUG Worker: Saved debug audio to {debug_save_path}")

                    print(f"DEBUG Worker: Processing task for {task['username']}. Temp file: {temp_path}, Audio length: {len(audio_data)} bytes.")
                    
                    # Whisperで文字起こし
                    transcription = ""
                    if self.whisper_model:
                        segments, info = self.whisper_model.transcribe(
                            temp_path,
                            language="ja",
                            beam_size=5,  # 精度を重視するためビームサイズ5
                            vad_filter=True, # WhisperのVADフィルターを有効に維持
                            no_speech_threshold=0.5, # 精度とリアルタイム性のバランス
                            condition_on_previous_text=True,  # 精度を重視するため文脈依存を有効
                            patience=0.5 # ★★★ patienceを0.5に再設定 ★★★
                        )
                        
                        for segment in segments:
                            transcription += segment.text
                    
                    # 文字起こし結果をHALLUCINATION_TEXTSでフィルタリング
                    stripped_transcription = transcription.strip()
                    if stripped_transcription and stripped_transcription not in HALLUCINATION_TEXTS:
                        self.result_queue.put({
                            'user_id': task['user_id'],
                            'username': task['username'],
                            'guild_id': task['guild_id'],
                            'transcription': stripped_transcription,
                            'timestamp': task['timestamp']
                        })
                        print(f"DEBUG Worker: Transcribed for {task['username']}: '{stripped_transcription}'. Result added to queue.")
                    else:
                        if stripped_transcription:
                            print(f"DEBUG Worker: Suppressed hallucination: '{stripped_transcription}' for {task['username']}.")
                        else:
                            print(f"DEBUG Worker: No transcription for {task['username']} (empty or stripped).")
                
                finally:
                    # 一時ファイルを削除
                    try:
                        os.remove(temp_path)
                        # print(f"DEBUG Worker: Cleaned up temp file: {temp_path}")
                    except Exception as cleanup_e:
                        print(f"DEBUG Worker: 一時ファイル削除エラー ({temp_path}): {cleanup_e}")
            
            except queue.Empty:
                # キューが空の場合は待機
                continue
            except Exception as e:
                print(f"ERROR: 文字起こし処理ワーカーでエラーが発生しました: {e}")


class StreamingAudioBuffer:
    """ストリーミング音声バッファ - 各ユーザーの音声を一時的に蓄積し、VADで発話区間を管理"""
    
    def __init__(self, user_id: int, username: str):
        self.user_id = user_id
        self.username = username
        self.audio_buffer = deque() # 受信した生のPCMデータ (Discordからの48kHzステレオ)
        self.last_speech_time = 0.0 # 最後に音声が検出された時刻
        self.is_speaking = False # 現在発話中かどうかのフラグ
        self.accumulated_audio = bytearray() # VAD・リサンプリング後の16kHzモノラル音声データ

        # チャンク管理用変数
        self.last_processed_buffer_length = 0 # 最後に処理した accumulated_audio の長さ
        
    def add_audio(self, audio_data: bytes) -> bool:
        """音声データを追加し、VADに基づいて発話の状態を更新。発話終了を検出した場合はTrueを返す。"""
        current_time = time.time()
        
        # 48kHzステレオPCMを16kHzモノラルにリサンプリング
        resampled_audio = AudioUtils.resample_audio(
            audio_data, PCM_SAMPLE_RATE, WHISPER_SAMPLE_RATE
        )
        
        # VAD判定にかかわらず、常に音声データをバッファに蓄積する
        self.accumulated_audio.extend(resampled_audio) 

        # VADで音声活動を検出
        has_speech = AudioUtils.apply_vad(resampled_audio, WHISPER_SAMPLE_RATE, VAD_AGGRESSIVENESS)
        
        if has_speech:
            self.last_speech_time = current_time
            if not self.is_speaking:
                print(f"DEBUG Buffer: {self.username} -> Speech START detected.")
            self.is_speaking = True
        else:
            # 無音時間が閾値を超えた場合、発話終了とみなす
            if self.is_speaking and (current_time - self.last_speech_time) > (SILENCE_THRESHOLD_MS / 1000):
                # Only signal speech end if there's actual accumulated audio to process
                if len(self.accumulated_audio) > 0: # Ensures we don't signal end on empty buffer
                    print(f"DEBUG Buffer: {self.username} -> Speech END detected. Accumulated audio length: {len(self.accumulated_audio)} bytes.")
                    self.is_speaking = False
                    return True  # 発話終了を示す
                else:
                    self.is_speaking = False # No speech and buffer empty, so stop speaking
            # VADが一時的に無音と判断しても、SILENCE_THRESHOLD_MSに達していなければis_speakingはTrueのまま
            # また、音声データは既にaccumulated_audioに追加済み
        
        return False
    
    def get_audio_chunk(self) -> Optional[bytes]:
        """蓄積された音声データから、固定サイズのチャンクを取得して返す"""
        # accumulated_audio から、オーバーラップを考慮して処理すべき新しい部分を切り出す
        new_data_length = len(self.accumulated_audio) - self.last_processed_buffer_length

        # REALTIME_CHUNK_BYTES 以上の新しいデータがある場合にチャンクを生成
        if new_data_length >= REALTIME_CHUNK_BYTES:
            # 処理するチャンクの開始位置を計算 (オーバーラップを考慮)
            chunk_start_index = self.last_processed_buffer_length - int(REALTIME_CHUNK_BYTES * OVERLAP_DURATION_MS / REALTIME_CHUNK_DURATION_MS)
            if chunk_start_index < 0: # 負の値にならないように
                chunk_start_index = 0

            # 処理するチャンクの終了位置
            chunk_end_index = chunk_start_index + REALTIME_CHUNK_BYTES
            
            chunk = bytes(self.accumulated_audio[chunk_start_index:chunk_end_index])
            
            self.last_processed_buffer_length = chunk_end_index # 処理した位置を更新
            print(f"DEBUG Buffer: {self.username} -> Extracted chunk of {len(chunk)} bytes. New accumulated length: {len(self.accumulated_audio)} bytes. Last processed: {self.last_processed_buffer_length}.")
            return chunk
        
        return None
    
    def get_remaining_audio(self) -> Optional[bytes]:
        """バッファに残っている全ての音声データを取得し、バッファをクリアする"""
        if len(self.accumulated_audio) > 0:
            remaining_chunk = bytes(self.accumulated_audio)
            self.accumulated_audio.clear()
            self.last_processed_buffer_length = 0
            print(f"DEBUG Buffer: {self.username} -> Getting remaining audio of {len(remaining_chunk)} bytes.")
            return remaining_chunk
        return None


class RealtimeVoiceProcessor:
    """リアルタイム音声プロセッサー - 音声データの受信と文字起こしエンジンへの連携を管理"""
    
    def __init__(self, transcription_engine: RealtimeTranscriptionEngine):
        self.transcription_engine = transcription_engine
        self.audio_buffers: Dict[int, Dict[int, StreamingAudioBuffer]] = {}  # guild_id -> user_id -> buffer
        self.text_channels: Dict[int, discord.TextChannel] = {}  # guild_id -> channel
        self.result_polling_tasks: Dict[int, asyncio.Task] = {}  # guild_id -> task
        self.periodic_chunk_processing_tasks: Dict[int, asyncio.Task] = {} # Guild ID -> Task for periodic chunking
        
    def start_processing(self, guild_id: int, text_channel: discord.TextChannel):
        """指定されたギルドの音声処理を開始"""
        self.text_channels[guild_id] = text_channel
        if guild_id not in self.audio_buffers:
            self.audio_buffers[guild_id] = {}
        
        # 文字起こしエンジンを開始
        self.transcription_engine.start()

        # 結果ポーリングタスクを開始
        if guild_id not in self.result_polling_tasks or self.result_polling_tasks[guild_id].done():
            self.result_polling_tasks[guild_id] = asyncio.create_task(
                self._result_polling_loop(guild_id)
            )

        # 定期的なチャンク処理タスクを開始 (VADに依存せず一定間隔でチャンクを生成)
        if guild_id not in self.periodic_chunk_processing_tasks or self.periodic_chunk_processing_tasks[guild_id].done():
            self.periodic_chunk_processing_tasks[guild_id] = asyncio.create_task(
                self._periodic_chunk_processing_loop(guild_id)
            )

        print(f"リアルタイム音声処理を開始: Guild {guild_id}")
        
    def stop_processing(self, guild_id: int):
        """指定されたギルドの音声処理を停止"""
        if guild_id in self.result_polling_tasks:
            self.result_polling_tasks[guild_id].cancel()
            del self.result_polling_tasks[guild_id]

        if guild_id in self.periodic_chunk_processing_tasks:
            self.periodic_chunk_processing_tasks[guild_id].cancel()
            del self.periodic_chunk_processing_tasks[guild_id]
        
        # 残りの音声データをすべて処理してからバッファをクリア
        if guild_id in self.audio_buffers:
            for user_id, buffer in list(self.audio_buffers[guild_id].items()):
                remaining_audio = buffer.get_remaining_audio()
                if remaining_audio:
                    self.transcription_engine.submit_audio(remaining_audio, user_id, buffer.username, guild_id)
            del self.audio_buffers[guild_id]
            
        if guild_id in self.text_channels:
            del self.text_channels[guild_id]

        # 文字起こしエンジンは全体で一つなので、ボット停止時のみ止める
        # self.transcription_engine.stop() # これはBot終了時に行う

        print(f"リアルタイム音声処理を停止: Guild {guild_id}")
        
    def process_audio(self, guild_id: int, user_id: int, username: str, audio_data: bytes):
        """OptimizedAudioSink から受信した生の音声データを処理"""
        if guild_id not in self.audio_buffers:
            return # 処理が開始されていないギルドのデータは無視
        
        if user_id not in self.audio_buffers[guild_id]:
            self.audio_buffers[guild_id][user_id] = StreamingAudioBuffer(user_id, username)
        
        buffer = self.audio_buffers[guild_id][user_id]
        
        # 音声データをバッファに追加し、VADで発話終了を検出
        speech_ended = buffer.add_audio(audio_data)
        
        # VADが発話終了を検出した場合、残りのデータを文字起こしエンジンに送信
        if speech_ended:
            remaining = buffer.get_remaining_audio()
            if remaining and len(remaining) > 0:
                print(f"DEBUG Processor: Speech ended detected for {username}. Submitting remaining {len(remaining)} bytes.")
                self.transcription_engine.submit_audio(remaining, user_id, username, guild_id)
            else:
                print(f"DEBUG Processor: Speech ended for {username}, but no remaining audio to process.")
        
    async def _periodic_chunk_processing_loop(self, guild_id: int):
        """定期的に音声バッファをチェックし、チャンクを生成して文字起こしエンジンに送信するループ"""
        while True:
            await asyncio.sleep(REALTIME_CHUNK_DURATION_MS / 1000.0) # 設定された間隔でチェック

            if guild_id not in self.audio_buffers:
                break # ギルドの処理が停止されたらループを抜ける

            # Iterate over a copy of items to allow modification during iteration
            for user_id, buffer in list(self.audio_buffers[guild_id].items()):
                # `get_audio_chunk` が内部で適切なサイズのチャンクを管理
                chunk = buffer.get_audio_chunk()
                if chunk:
                    print(f"DEBUG Processor: Periodic chunk generated for {buffer.username} ({len(chunk)} bytes). Submitting.")
                    self.transcription_engine.submit_audio(chunk, user_id, buffer.username, guild_id)
                # 発話終了時の処理はprocess_audioに任せるため、このelifブロックは不要
                    
    async def _result_polling_loop(self, guild_id: int):
        """文字起こし結果キューを定期的にポーリングし、Discordに送信するループ"""
        while True:
            try:
                result = self.transcription_engine.get_result()
                if result and result['guild_id'] == guild_id:
                    text_channel = self.text_channels.get(guild_id)
                    if text_channel:
                        await text_channel.send(f"**{result['username']}**: {result['transcription']}")
                        print(f"文字起こし送信: {result['username']} -> {result['transcription']}")
                
                await asyncio.sleep(0.05)  # 50msごとにチェック (ポーリング間隔)
            except asyncio.CancelledError:
                print(f"結果ポーリングループがキャンセルされました: Guild {guild_id}")
                break
            except Exception as e:
                print(f"ERROR: 結果ポーリングエラー: {e}")
                # エラーが発生してもループを続行するが、致命的な場合はbreakすることも検討
                await asyncio.sleep(1) # エラー時の連続ポーリングを防ぐ


# グローバル変数 (Bot起動時に初期化される)
transcription_engine: Optional[RealtimeTranscriptionEngine] = None
voice_processor: Optional[RealtimeVoiceProcessor] = None


class OptimizedAudioSink(AudioSink):
    """discord-ext-voice-recv からデコード済みPCM音声データを受信するカスタムシンク"""
    
    def __init__(self, processor: RealtimeVoiceProcessor, guild_id: int):
        super().__init__()
        self.processor = processor
        self.guild_id = guild_id
        
    def write(self, user: discord.Member, data: VoiceData):
        """音声データを受信し、リアルタイムプロセッサーに渡す"""
        if user.bot:
            return
        
        # ユーザーごとの音声データをプロセッサーに送信
        # data.pcm は既にデコードされたPCMデータ
        if self.processor: # プロセッサーが初期化されていることを確認
            self.processor.process_audio(
                self.guild_id, 
                user.id, 
                user.display_name, 
                data.pcm
            )
            # StreamingAudioBuffer オブジェクトの accumulated_audio の長さを参照
            current_user_buffer = self.processor.audio_buffers.get(self.guild_id, {}).get(user.id)
            buffer_len = len(current_user_buffer.accumulated_audio) if current_user_buffer else 0
            print(f"DEBUG OptimizedAudioSink: User {user.display_name} received {len(data.pcm)} bytes. Total in buffer: {buffer_len} bytes.")
        else:
            print("WARNING: RealtimeVoiceProcessor is not initialized.")
    
    def wants_opus(self) -> bool:
        """シンクがOpus形式の音声データを希望するかどうかを返します。Falseの場合、PCMを受け取ります。"""
        return False
        
    def cleanup(self):
        """シンクが破棄される際に呼び出されるクリーンアップメソッドです。"""
        print(f"DEBUG OptimizedAudioSink: cleanup called for Guild {self.guild_id}.")
        pass


# Botコマンド
@bot.command()
async def join(ctx):
    """ボットをボイスチャンネルに接続し、リアルタイム音声録音・転写を開始"""
    if ctx.author.voice is None:
        await ctx.send("❌ ボイスチャンネルに接続してください。")
        return

    voice_channel = ctx.author.voice.channel
    
    # 既存の接続があれば切断し、関連する処理を停止
    if ctx.guild.id in connections:
        old_vc = connections[ctx.guild.id]
        if old_vc.is_connected():
            print(f"既存のボイス接続を切断します: Guild {ctx.guild.id}")
            # 古い接続からのlistenを停止
            old_vc.stop_listening()
            await old_vc.disconnect()
            
            # 古いギルドのリアルタイム処理も停止
            if voice_processor:
                voice_processor.stop_processing(ctx.guild.id)
            
            del connections[ctx.guild.id]
            await asyncio.sleep(0.5) # 切断処理が完全に終わるのを待つ

    # VoiceRecvClient を使用して接続
    try:
        vc = await voice_channel.connect(cls=VoiceRecvClient, reconnect=True) 
        connections[ctx.guild.id] = vc

        if voice_processor is None:
            await ctx.send("❌ STTシステムが初期化されていません。Botログを確認してください。")
            print("ERROR: voice_processor is None. Cannot start processing.")
            return

        # 音声シンクを設定し、リアルタイム処理を開始
        vc.listen(OptimizedAudioSink(voice_processor, ctx.guild.id))
        voice_processor.start_processing(ctx.guild.id, ctx.channel)
        
        print(f"🔊 VoiceRecvClient listening with OptimizedAudioSink for Guild {ctx.guild.id}.")

        await ctx.send(f'🎵 ボイスチャンネル **{voice_channel.name}** に接続しました！')
        await ctx.send(
            f"🎙️ **リアルタイム音声文字起こしを開始しました！**\n"
            f"⚡ 処理間隔: {REALTIME_CHUNK_DURATION_MS}ms\n"
            f"🎯 VAD感度: {VAD_AGGRESSIVENESS}/3\n"
            f"🔊 音声検出閾値: {SILENCE_THRESHOLD_MS}ms\n"
            f"ℹ️ `!leave` で接続を切断できます。"
        )
        print("リアルタイム音声監視開始。")

    except discord.errors.ClientException as e:
        await ctx.send(f"❌ ボイスチャンネルへの接続に失敗しました: {e}")
        print(f"ERROR: Failed to connect to voice channel: {e}")
    except Exception as e:
        await ctx.send(f"❌ 予期せぬエラーが発生しました: {e}")
        print(f"ERROR: An unexpected error occurred in !join: {e}")


@bot.command()
async def leave(ctx):
    """ボイスチャンネルから切断し、リアルタイム音声録音を停止"""
    if ctx.guild.id not in connections:
        await ctx.send("❌ ボイスチャンネルに接続していません。")
        return
    
    vc = connections[ctx.guild.id]
    
    await ctx.send("🛑 リアルタイム音声監視を停止してボイスチャンネルから切断します...")

    # リアルタイム処理を停止（これによりバッファの残りが処理される）
    if voice_processor:
        voice_processor.stop_processing(ctx.guild.id)

    # VoiceRecvClient の listen を停止し、切断
    vc.stop_listening() # 明示的にリスナーを停止
    await vc.disconnect()
    del connections[ctx.guild.id]
    
    await ctx.send("👋 ボイスチャンネルから切断しました。")
    print('Botがボイスチャンネルから切断しました。')


@bot.command()
async def status(ctx):
    """現在の状況を確認"""
    if ctx.guild.id not in connections:
        await ctx.send("❌ ボイスチャンネルに接続していません。")
        return
    
    vc = connections[ctx.guild.id]
    channel_members = len(vc.channel.members) - 1 # Bot自身を除く
    
    status_msg = f"📊 **リアルタイム文字起こし稼働中**\n"
    status_msg += f"🎤 チャンネル参加者: {channel_members}人\n"
    status_msg += f"⚡ 処理間隔: {REALTIME_CHUNK_DURATION_MS}ms\n"
    status_msg += f"🎯 VAD感度: {VAD_AGGRESSIVENESS}/3\n"
    status_msg += f"🔊 音声検出閾値: {SILENCE_THRESHOLD_MS}ms\n"
    
    # 現在バッファリングされている音声データの量を表示（デバッグ用）
    if ctx.guild.id in voice_processor.audio_buffers:
        for user_id, buffer in voice_processor.audio_buffers[ctx.guild.id].items():
            user = bot.get_user(user_id) or ctx.guild.get_member(user_id)
            if user:
                status_msg += f"Buffer ({user.display_name}): {len(buffer.accumulated_audio)} bytes\n"
    
    await ctx.send(status_msg)

@bot.command()
async def toggle_debug_audio(ctx):
    """デバッグ用音声保存の有効/無効を切り替えます。"""
    global SAVE_DEBUG_AUDIO
    SAVE_DEBUG_AUDIO = not SAVE_DEBUG_AUDIO
    status_text = "有効" if SAVE_DEBUG_AUDIO else "無効"
    await ctx.send(f"🔊 デバッグ用音声保存を**{status_text}**にしました。\n保存先: `{DEBUG_AUDIO_SAVE_DIR}`")


@bot.event
async def on_ready():
    """Bot起動時の初期化"""
    print(f'{bot.user} がDiscordに接続しました！')
    
    global WHISPER_MODEL, transcription_engine, voice_processor
    try:
        print(f"Whisperモデル ({WHISPER_MODEL_SIZE}, {WHISPER_DEVICE}, {WHISPER_COMPUTE_TYPE}) をロード中...")
        WHISPER_MODEL = WhisperModel(WHISPER_MODEL_SIZE, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE_TYPE)
        print("✅ Whisperモデルのロードが完了しました。")
        
        # エンジンとプロセッサーを初期化
        transcription_engine = RealtimeTranscriptionEngine(WHISPER_MODEL, AUDIO_OUTPUT_DIR)
        voice_processor = RealtimeVoiceProcessor(transcription_engine)
        
        print("✅ リアルタイム文字起こしシステムの初期化が完了しました。")
    except Exception as e:
        print(f"❌ Whisperモデルのロードに失敗しました: {e}")
        WHISPER_MODEL = None

    print(f'利用可能なSTT: {"✅ ローカルWhisper" if WHISPER_MODEL else "❌ なし"}')

@bot.event
async def on_voice_state_update(member, before, after):
    """ユーザーのボイスチャンネル状態更新イベント"""
    # このイベントはボットの接続状態や、ユーザーがミュート・デフになったり、チャンネルを移動したりしたときに発生します。
    # ここでは主にデバッグ目的で使用。
    if member == bot.user:
        return # Bot自身の状態変更は無視

    guild_id = member.guild.id
    # Botが接続しているギルドのユーザーの状態変更のみを処理
    if guild_id in connections and connections[guild_id].is_connected():
        # チャンネルの移動や退出
        if before.channel and before.channel != after.channel:
            print(f'DEBUG on_voice_state_update: {member.display_name} が {before.channel.name} から退出しました。')
            # ユーザーがボイスチャンネルを退出した場合、そのユーザーの残りの音声データを処理
            if voice_processor and guild_id in voice_processor.audio_buffers and member.id in voice_processor.audio_buffers[guild_id]:
                text_channel = voice_processor.text_channels.get(guild_id)
                if text_channel:
                    buffer = voice_processor.audio_buffers[guild_id].pop(member.id)
                    remaining_audio = buffer.get_remaining_audio()
                    if remaining_audio:
                        print(f"DEBUG on_voice_state_update: {member.display_name} の退出を検知。残りの音声データ {len(remaining_audio)} バイトを処理します。")
                        voice_processor.transcription_engine.submit_audio(remaining_audio, member.id, member.display_name, guild_id)
                    else:
                        print(f"DEBUG on_voice_state_update: {member.display_name} の退出を検知しましたが、未処理の音声データはありません。")
                else:
                    print(f"WARNING: テキストチャンネルが見つからないため、{member.display_name} の残りの音声を処理できませんでした。")


# Botの実行
if DISCORD_BOT_TOKEN:
    try:
        bot.run(DISCORD_BOT_TOKEN)
    except Exception as e:
        print(f"Botの実行中にエラーが発生しました: {e}")
    finally:
        # ボットがシャットダウンする際に文字起こしエンジンも停止
        if transcription_engine:
            transcription_engine.stop()
else:
    print("エラー: Discord Botトークンが設定されていません。'.env'ファイルを確認してください。")

