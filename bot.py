# ... (既存のインポートと初期設定) ...
# from discord.ext import commands <- これに加え
from discord_ext_voice_recv import VoiceRecvClient # 新しくインポート

# ... (既存のクラス定義やグローバル変数) ...

# VoiceRecvClientを扱うためのconnections辞書更新
connections: Dict[int, VoiceRecvClient] = {}

# リアルタイム録音バッファをユーザーごとに管理
# 例: {guild_id: {user_id: [audio_chunks], ...}}
realtime_audio_buffers: Dict[int, Dict[int, bytearray]] = {}
# ユーザーごとのVAD状態を管理
# 例: {guild_id: {user_id: True/False, ...}}
user_speaking_status: Dict[int, Dict[int, bool]] = {}
# 一時ファイル管理 (元のコードの tempfile はそのまま利用可能)
# 転写処理のタスクを管理
transcription_tasks: Dict[int, Dict[int, asyncio.Task]] = {}


class RealtimeVoiceDataProcessor:
    """リアルタイム音声データ処理とSTT処理を管理するクラス"""

    def __init__(self, output_dir: str, stt_handler: SpeechToTextHandler):
        self.output_dir = output_dir
        self.stt_handler = stt_handler
        self.start_time = int(time.time())

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
                task = asyncio.create_task(
                    self.process_single_user_audio(
                        pcm_data,
                        user.id,
                        user.display_name,
                        bot.get_channel(connections[guild_id].channel.id) # または ctx.channel をどこかから渡す
                    )
                )
                if guild_id not in transcription_tasks:
                    transcription_tasks[guild_id] = {}
                transcription_tasks[guild_id][user.id] = task
            else:
                print(f"⚠️ {user.display_name} の音声データが空でした。")
        else:
            print(f"⚠️ {user.display_name} の音声バッファが見つかりませんでした。")

    async def process_single_user_audio(self, pcm_data: bytes, user_id: int, username: str, text_channel: discord.TextChannel):
        """個別のユーザーの音声データを処理（保存、転写、結果送信）"""
        print(f"--- 音声処理開始: {username} ---")
        speaker_info = self.identify_speaker(user_id, username)
        temp_audio_path = await self.save_temp_audio_file(pcm_data, user_id, username)

        transcription = None
        if temp_audio_path:
            transcription = await self.stt_handler.transcribe_with_local_whisper(temp_audio_path)
            try:
                os.remove(temp_audio_path)
                print(f"🗑️ 一時ファイルを削除しました: {temp_audio_path}")
            except Exception as e:
                print(f"一時ファイル削除エラー: {temp_audio_path} - {e}")
        else:
            print(f"❌ {username} の一時音声ファイル保存に失敗しました")

        if transcription and transcription.strip():
            await text_channel.send(f"**{username}**: {transcription}")
            await self.save_transcription(user_id, username, transcription, speaker_info)
        else:
            await text_channel.send(f"**{username}**: _(転写失敗または音声なし)_")
            print(f"❌ {username} の音声転写に失敗しました (空またはNone)")
        print(f"--- 音声処理完了: {username} ---")


    async def save_temp_audio_file(self, pcm_data: bytes, user_id: int, username: str) -> Optional[str]:
        """PCMデータをWAV形式で一時ファイルに保存 (既存関数を流用)"""
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix='.wav') as temp_file:
                temp_path = temp_file.name
            with wave.open(temp_path, 'wb') as wf:
                wf.setnchannels(2)
                wf.setsampwidth(2)
                wf.setframerate(48000)
                wf.writeframes(pcm_data)
            print(f"📁 一時ファイル保存: {username} -> {temp_path}")
            return temp_path
        except Exception as e:
            print(f"一時ファイル保存エラー ({username}): {e}")
            return None

    async def save_transcription(self, user_id: int, username: str, transcription: str, speaker_info: Dict):
        """転写結果をJSONL形式でファイルに保存 (既存関数を流用)"""
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

# ユーザーが話しているかをリアルタイムで検知し、音声データをバッファリングするリスナー
@bot.event
async def on_voice_receive(user: discord.Member, chunk: bytes):
    """
    discord-ext-voice-recv からリアルタイムで音声チャンクを受信
    """
    if user.bot: # ボット自身の音声は無視
        return

    guild_id = user.guild.id
    user_id = user.id

    if guild_id in connections and connections[guild_id].is_currently_recording:
        if guild_id not in realtime_audio_buffers:
            realtime_audio_buffers[guild_id] = {}
        if user_id not in realtime_audio_buffers[guild_id]:
            realtime_audio_buffers[guild_id][user_id] = bytearray()
        
        # 音声チャンクをバッファに追加
        realtime_audio_buffers[guild_id][user_id].extend(chunk)

@bot.event
async def on_voice_member_speaking_start(member: discord.Member):
    """メンバーが話し始めたときに呼ばれるイベント"""
    if member.bot:
        return
    guild_id = member.guild.id
    if guild_id in connections and connections[guild_id].is_currently_recording:
        # VAD状態を更新し、必要な初期化を行う
        if guild_id not in user_speaking_status:
            user_speaking_status[guild_id] = {}
        await realtime_voice_processor.handle_speaking_start(guild_id, member)

@bot.event
async def on_voice_member_speaking_stop(member: discord.Member):
    """メンバーが話し終えたときに呼ばれるイベント"""
    if member.bot:
        return
    guild_id = member.guild.id
    if guild_id in connections and connections[guild_id].is_currently_recording:
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
            for user_id, buffer in realtime_audio_buffers.get(ctx.guild.id, {}).items():
                if buffer: # バッファにデータがあれば処理
                    user = bot.get_user(user_id) or ctx.guild.get_member(user_id)
                    if user:
                        await realtime_voice_processor.process_single_user_audio(
                            bytes(buffer), user.id, user.display_name, ctx.channel
                        )
            realtime_audio_buffers.pop(ctx.guild.id, None) # バッファをクリア
            user_speaking_status.pop(ctx.guild.id, None) # 状態をクリア
            for user_id, task in transcription_tasks.get(ctx.guild.id, {}).items():
                if not task.done():
                    print(f"未完了の転写タスクをキャンセル: {user_id}")
                    task.cancel() # タスクをキャンセル
            transcription_tasks.pop(ctx.guild.id, None)

        await old_vc.disconnect()
        del connections[ctx.guild.id]
        await asyncio.sleep(0.5)

    # VoiceRecvClient を使用して接続
    # listen=True で音声データを受信可能にする
    vc = await voice_channel.connect(cls=VoiceRecvClient, reconnect=True, listen=True)
    connections[ctx.guild.id] = vc
    vc.is_currently_recording = True # 録音開始フラグをTrueに設定

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
            for user_id, pcm_data_buffer in realtime_audio_buffers[ctx.guild.id].items():
                if pcm_data_buffer:
                    user = bot.get_user(user_id) or ctx.guild.get_member(user_id)
                    if user:
                        await realtime_voice_processor.process_single_user_audio(
                            bytes(pcm_data_buffer), user_id, user.display_name, ctx.channel
                        )
            realtime_audio_buffers[ctx.guild.id].clear() # バッファをクリア
            user_speaking_status[ctx.guild.id].clear() # 状態をクリア
            for user_id, task in transcription_tasks.get(ctx.guild.id, {}).items():
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
            for user_id, pcm_data_buffer in realtime_audio_buffers[ctx.guild.id].items():
                if pcm_data_buffer:
                    user = bot.get_user(user_id) or ctx.guild.get_member(user_id)
                    if user:
                        await realtime_voice_processor.process_single_user_audio(
                            bytes(pcm_data_buffer), user_id, user.display_name, ctx.channel
                        )
            realtime_audio_buffers.pop(ctx.guild.id, None) # バッファをクリア
            user_speaking_status.pop(ctx.guild.id, None) # 状態をクリア

        # 実行中の転写タスクがあれば待機またはキャンセル
        if ctx.guild.id in transcription_tasks:
            for user_id, task in transcription_tasks[ctx.guild.id].items():
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

# ... (on_ready, hello, register_voice, status, test_stt コマンドは基本的にそのまま) ...

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

# ボットの実行
if DISCORD_BOT_TOKEN:
    bot.run(DISCORD_BOT_TOKEN)
else:
    print("エラー: Discord Botトークンが設定されていません。'.env'ファイルを確認してください。")
