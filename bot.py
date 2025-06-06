import os
import discord
from discord.ext import commands
from dotenv import load_dotenv
import asyncio
import time
import wave
import io
import json
from typing import Optional, Dict, Any
import tempfile # 一時ファイル作成・管理用

# faster-whisperのインポート
from faster_whisper import WhisperModel

# .env ファイルから環境変数をロード
load_dotenv()

# 各種トークン・APIキーを取得
DISCORD_BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
# OpenAI API キーはローカルWhisperでは不要になります
# OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") 
GOOGLE_CLOUD_API_KEY = os.getenv("GOOGLE_CLOUD_API_KEY") # 現在は未使用

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

# ユーザー音声プロファイル管理 (現在は未使用、将来の話者識別強化用)
user_voice_profiles: Dict[int, Dict[str, Any]] = {}
# ギルドごとのボイス接続を管理
connections: Dict[int, discord.VoiceClient] = {}

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
    async def transcribe_with_google(audio_file_path: str, unique_id: str) -> Optional[str]:
        """Google Cloud Speech-to-Text APIを使用（未実装）"""
        # Google Cloud STT APIの実装は別途必要です。
        # 実際の実装はGoogle Cloud SDKを使用することを推奨
        print("Google Cloud STT APIは現在実装されていません。")
        return None

class VoiceDataProcessor:
    """音声データ処理とSTT処理を管理するクラス"""
    
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

    async def process_recorded_audio(self, sink: discord.sinks.WaveSink, channel: discord.TextChannel):
        """録音完了後の音声データ処理"""
        print("🎵 録音完了 - 音声データ処理開始...")
        
        # 転写結果を一時的に保持するリスト
        all_transcriptions = []

        try:
            # 各ユーザーの音声データを処理
            if not sink.audio_data:
                await channel.send("⚠️ 録音データがありませんでした。誰も話していなかった可能性があります。")
                print("録音データがありませんでした。")
                return

            for user_id, audio_data in sink.audio_data.items():
                # bot.get_user はキャッシュに依存するため、get_guild.get_member も試す
                user = bot.get_user(user_id) or channel.guild.get_member(user_id)
                if not user:
                    print(f"ユーザーID {user_id} のユーザー情報が取得できませんでした")
                    continue
                
                print(f"👤 処理中: {user.display_name} (ID: {user_id})")
                
                # 話者識別
                speaker_info = self.identify_speaker(user_id, user.display_name)
                
                # 音声ファイルを一時保存
                # audio_data.file は io.BytesIO オブジェクトなので、getvalue()でバイトデータを取得
                temp_audio_path = await self.save_temp_audio_file(audio_data.file.getvalue(), user_id, user.display_name)
                
                if temp_audio_path:
                    # STT処理: faster-whisperはunique_idを必要としないため、引数から削除
                    transcription = await self.stt_handler.transcribe_with_local_whisper(temp_audio_path)
                    
                    if transcription and transcription.strip():
                        # 結果をリストに追加
                        all_transcriptions.append(f"**{user.display_name}**: {transcription}")
                        
                        # 転写結果をファイルに保存
                        await self.save_transcription(user_id, user.display_name, transcription, speaker_info)
                    else:
                        print(f"❌ {user.display_name} の音声転写に失敗しました (空またはNone)")
                        all_transcriptions.append(f"**{user.display_name}**: _(転写失敗または音声なし)_")
                        
                    # 一時ファイルを削除 (finallyブロックで確実に行う)
                    try:
                        os.remove(temp_audio_path)
                        print(f"🗑️ 一時ファイルを削除しました: {temp_audio_path}")
                    except Exception as e:
                        print(f"一時ファイル削除エラー: {temp_audio_path} - {e}")
                else:
                    print(f"❌ {user.display_name} の一時音声ファイル保存に失敗しました")
            
            # 全ての転写結果をDiscordチャンネルにまとめて送信
            if all_transcriptions:
                message_parts = []
                current_message = "--- 全ての転写結果 ---\n"
                for entry in all_transcriptions:
                    # Discordのメッセージ文字数制限 (約2000文字) を考慮
                    if len(current_message) + len(entry) + 4 > 1990: # 少し余裕を持たせる
                        message_parts.append(current_message)
                        current_message = ""
                    current_message += entry + "\n"
                if current_message.strip() != "--- 全ての転写結果 ---": # 残りのメッセージを追加
                    message_parts.append(current_message)
                
                for msg_part in message_parts:
                    await channel.send(msg_part)
                await channel.send("✅ 全ての音声処理が完了しました。")
            else:
                await channel.send("ℹ️ 処理対象の音声データが見つかりませんでした。")

        except Exception as e:
            print(f"音声データ処理エラー: {e}")
            await channel.send(f"❌ 音声処理中にエラーが発生しました: {str(e)}")

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

# グローバルな音声データプロセッサ
# STTハンドラーを初期化する際に、ロード済みのWHISPER_MODELを渡す
# on_readyでWHISPER_MODELがロードされるため、初期段階ではNoneの可能性がある
# 実際にはon_readyで再初期化される
voice_processor = VoiceDataProcessor(AUDIO_OUTPUT_DIR, SpeechToTextHandler(None)) # 初期段階ではNoneを渡す

async def once_done(sink: discord.sinks.WaveSink, channel: discord.TextChannel, *args):
    """録音完了時のコールバック関数"""
    # イベントループが閉じている場合は処理をスキップ
    if bot.loop.is_closed():
        print("警告: イベントループが閉じているため、録音完了後の処理をスキップします。")
        return
    
    print("🛑 録音が完了しました。音声処理を開始します...")
    # 録音されたデータを処理する
    # voice_processor はグローバルで定義されており、on_readyでSTTハンドラーが更新される
    await voice_processor.process_recorded_audio(sink, channel)
    
    # 録音終了時、VoiceClientの recording_state を False に設定
    # ギルドIDを使ってconnectionsからVoiceClientを取得
    vc = connections.get(channel.guild.id)
    if vc:
        vc.is_currently_recording = False # 録音状態フラグをリセット

    # 接続をconnectionsから削除（ギルドが切断されたときも考慮）
    if channel.guild.id in connections:
        # vc.disconnect() は leave コマンドで実行されるべきなので、ここでは行わない
        # ただし、Botが切断されない限りconnectionsに残しておくのが適切
        pass # connectionsからの削除は leave コマンドに任せる


@bot.event
async def on_ready():
    """BotがDiscordに接続したときに呼ばれるイベント"""
    print(f'{bot.user} がDiscordに接続しました！')
    print(f'Bot ID: {bot.user.id}')
    # Whisperモデルをロード
    global WHISPER_MODEL
    try:
        print(f"Whisperモデル ({WHISPER_MODEL_SIZE}, {WHISPER_DEVICE}, {WHISPER_COMPUTE_TYPE}) をロード中...")
        # PyTorchがCPU/GPUのバージョンと互換性があるか確認
        # deviceが"cuda"の場合、GPUの利用可能性をチェックすることが望ましい
        WHISPER_MODEL = WhisperModel(WHISPER_MODEL_SIZE, device=WHISPER_DEVICE, compute_type=WHISPER_COMPUTE_TYPE)
        print("Whisperモデルのロードが完了しました。")
        # モデルがロードされたらSTTハンドラーとVoiceDataProcessorを再初期化
        voice_processor.stt_handler = SpeechToTextHandler(WHISPER_MODEL)
    except Exception as e:
        print(f"Whisperモデルのロードに失敗しました: {e}")
        WHISPER_MODEL = None # ロード失敗時はNoneにする

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
async def join(ctx):
    """ボットをボイスチャンネルに接続し、音声録音・転写を開始"""
    if ctx.author.voice is None:
        await ctx.send("❌ ボイスチャンネルに接続してください。")
        return

    voice_channel = ctx.author.voice.channel
    
    # 既存の接続があれば切断
    if ctx.guild.id in connections:
        old_vc = connections[ctx.guild.id]
        # 録音中の場合は停止 (カスタムフラグを使用)
        if hasattr(old_vc, 'is_currently_recording') and old_vc.is_currently_recording:
            old_vc.stop_recording()
            old_vc.is_currently_recording = False # フラグをリセット
        await old_vc.disconnect()
        del connections[ctx.guild.id] # 古い接続を削除
        await asyncio.sleep(0.5)

    # ボイスチャンネルに接続
    vc = await voice_channel.connect()
    connections[ctx.guild.id] = vc # 新しい接続を記録
    vc.is_currently_recording = False # 初期状態をFalseに設定
    
    await ctx.send(f'🎵 ボイスチャンネル **{voice_channel.name}** に接続しました！')
    print(f'Botがボイスチャンネル {voice_channel.name} に接続しました。')

    # STTモデルがロードされているか確認
    # joinコマンド実行時にvoice_processorのstt_handlerが適切に設定されていることを確認
    if not voice_processor.stt_handler or voice_processor.stt_handler.whisper_model is None:
        await ctx.send("⚠️ Whisperモデルがロードされていません。STT機能は利用できません。Botのログを確認してください。")
        print("Whisperモデルがロードされていないため、STT機能なしで録音を開始します。")
        pass # STTがNoneでも続行し、process_recorded_audioでスキップする

    # WaveSinkを使用して録音開始
    sink = discord.sinks.WaveSink()
    # 修正: bot.loop.call_soon_threadsafe を使用してメインスレッドでコールバックをスケジュール
    #       asyncio.create_task の呼び出しは call_soon_threadsafe の内部で行われるようにする
    vc.start_recording(
        sink,
        lambda s: bot.loop.call_soon_threadsafe(once_done, s, ctx.channel),
    )
    vc.is_currently_recording = True # 録音開始フラグをTrueに設定
    
    await ctx.send(
        f"🎙️ **音声録音・転写を開始しました！**\n"
        f"📁 ファイル保存先: `{AUDIO_OUTPUT_DIR}`\n"
        f"🤖 STT: {'✅ ローカルWhisper' if voice_processor.stt_handler and voice_processor.stt_handler.whisper_model else '❌ なし'}\n"
        f"ℹ️ `!stop` で録音を停止できます。"
    )
    print(f"音声録音開始: {AUDIO_OUTPUT_DIR}")

@bot.command()
async def stop(ctx):
    """音声録音を停止"""
    if ctx.guild.id not in connections:
        await ctx.send("❌ ボイスチャンネルに接続していません。")
        return
    
    vc = connections[ctx.guild.id]
    
    # カスタムフラグで録音中か確認
    if hasattr(vc, 'is_currently_recording') and vc.is_currently_recording:
        vc.stop_recording() # 録音を停止するとonce_doneが呼ばれる
        # once_doneで is_currently_recording がFalseに設定される
        await ctx.send("🛑 録音を停止しました。音声処理を開始します...")
        print("音声録音を停止しました。")
    else:
        await ctx.send("❌ 現在録音していません。")

@bot.command()
async def leave(ctx):
    """ボイスチャンネルから切断"""
    if ctx.guild.id not in connections:
        await ctx.send("❌ ボイスチャンネルに接続していません。")
        return
    
    vc = connections[ctx.guild.id]
    
    # 録音中なら停止 (カスタムフラグを使用)
    if hasattr(vc, 'is_currently_recording') and vc.is_currently_recording:
        vc.stop_recording()
        vc.is_currently_recording = False # フラグをリセット
        await ctx.send("🛑 録音を停止してボイスチャンネルから切断します...")
    
    await vc.disconnect()
    if ctx.guild.id in connections: # 念のためconnectionsからも削除
        del connections[ctx.guild.id]
    await ctx.send("👋 ボイスチャンネルから切断しました。")
    print('Botがボイスチャンネルから切断しました。')

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
