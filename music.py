import asyncio
import gc
import logging
import os
import random
import shlex
import time
from collections import deque

import discord
from discord.ext import commands

from music_backend import (
    DEFAULT_USER_AGENT,
    cleanup_all,
    cleanup_stale_audio_files,
    clone_info,
    extract_spotify_metadata,
    get_yt_dlp_auth_config,
    parse_cookies_for_ffmpeg,
    search_and_download,
    track_label,
    warmup_extractors,
)

logger = logging.getLogger("discordbot.music")
AUTO_DISCONNECT_WHEN_EMPTY = os.getenv("AUTO_DISCONNECT_WHEN_EMPTY", "true").strip().lower() in ("1", "true", "yes", "on")
AUTO_DISCONNECT_EMPTY_DELAY = int(os.getenv("AUTO_DISCONNECT_EMPTY_DELAY", "60"))
_STARTUP_WARMUP_YOUTUBE = os.getenv("MUSIC_WARMUP_YOUTUBE", "").strip().lower() in ("1", "true", "yes", "on")
_STARTUP_WARMUP_DELAY = int(os.getenv("MUSIC_WARMUP_DELAY", "2"))
_PREFETCH_DELAY_SECONDS = float(os.getenv("MUSIC_PREFETCH_DELAY", "2"))

# ── Views ───────────────────────────────────────────────────────────────────

class MusicControlView(discord.ui.View):
    def __init__(self, bot, guild_id):
        super().__init__(timeout=None)
        self.bot = bot
        self.guild_id = guild_id

    def get_music_cog(self):
        return self.bot.get_cog("Music")

    @discord.ui.button(emoji="⏯️", style=discord.ButtonStyle.primary)
    async def play_pause_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if not vc:
            return await interaction.response.send_message("❌ Not connected to voice.", ephemeral=True)
        
        if vc.is_playing():
            vc.pause()
            await interaction.response.send_message("⏸️ Paused.", ephemeral=True)
        elif vc.is_paused():
            vc.resume()
            await interaction.response.send_message("▶️ Resumed.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Nothing is playing.", ephemeral=True)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary)
    async def skip_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            vc.stop()
            await interaction.response.send_message("⏭️ Skipped.", ephemeral=True)
        else:
            await interaction.response.send_message("❌ Nothing to skip.", ephemeral=True)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary)
    async def shuffle_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        st = self.get_music_cog().state(self.guild_id)
        if len(st.queue) < 2:
            return await interaction.response.send_message("❌ Not enough songs to shuffle.", ephemeral=True)
        
        temp_list = list(st.queue)
        random.shuffle(temp_list)
        st.queue = deque(temp_list)
        await interaction.response.send_message("🔀 Shuffled.", ephemeral=True)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary)
    async def loop_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        st = self.get_music_cog().state(self.guild_id)
        valid_modes = ["off", "song", "queue"]
        idx = (valid_modes.index(st.loop_mode) + 1) % len(valid_modes)
        st.loop_mode = valid_modes[idx]
        
        emoji = {"off": "➡️", "song": "🔂", "queue": "🔁"}
        await interaction.response.send_message(f"{emoji[st.loop_mode]} Loop: **{st.loop_mode}**", ephemeral=True)


# ── Per-guild state ────────────────────────────────────────────────────────────

class GuildState:
    def __init__(self):
        self.queue: deque[dict] = deque()
        self.current_title: str | None = None
        self.current_file: str | None = None
        self.volume: float = 0.5
        self.is_loading: bool = False
        self.loop_mode: str = "off"
        self.current_info: dict | None = None
        self.advance_lock = asyncio.Lock()
        self.prefetch_task: asyncio.Task | None = None
        self.last_voice_channel_id: int | None = None
        self.last_text_channel_id: int | None = None
        self.playback_started_at: float | None = None
        self.expected_disconnect_until: float = 0.0
        self.recovery_lock = asyncio.Lock()
        self.connection_lock = asyncio.Lock()
        self.empty_disconnect_task: asyncio.Task | None = None


# ── Cog ───────────────────────────────────────────────────────────────────────

class Music(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._states: dict[int, GuildState] = {}
        self._warmup_task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._voice_watchdog_task: asyncio.Task | None = None

    def state(self, guild_id: int) -> GuildState:
        if guild_id not in self._states:
            self._states[guild_id] = GuildState()
        return self._states[guild_id]

    async def cog_load(self):
        await asyncio.to_thread(cleanup_all)
        self._warmup_task = asyncio.create_task(self._warmup_extractors())
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup())
        self._voice_watchdog_task = asyncio.create_task(self._voice_watchdog())

    def cog_unload(self):
        if self._warmup_task and not self._warmup_task.done():
            self._warmup_task.cancel()
        if self._cleanup_task and not self._cleanup_task.done():
            self._cleanup_task.cancel()
        if self._voice_watchdog_task and not self._voice_watchdog_task.done():
            self._voice_watchdog_task.cancel()
        for st in self._states.values():
            if st.prefetch_task and not st.prefetch_task.done():
                st.prefetch_task.cancel()
            if st.empty_disconnect_task and not st.empty_disconnect_task.done():
                st.empty_disconnect_task.cancel()

    async def _periodic_cleanup(self):
        """Periodically remove old audio files to save disk space."""
        try:
            while True:
                await asyncio.sleep(3600)
                await asyncio.to_thread(cleanup_stale_audio_files, 7200)
        except asyncio.CancelledError:
            pass

    async def _warmup_extractors(self):
        try:
            await warmup_extractors(
                warmup_youtube=_STARTUP_WARMUP_YOUTUBE,
                delay_seconds=_STARTUP_WARMUP_DELAY,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Music warmup failed: %s", exc)

    def _mark_expected_disconnect(self, st: GuildState, *, seconds: float = 15.0):
        st.expected_disconnect_until = time.monotonic() + seconds

    def _remember_context(self, ctx: commands.Context):
        st = self.state(ctx.guild.id)
        st.last_text_channel_id = ctx.channel.id
        author_voice = getattr(ctx.author, "voice", None)
        if author_voice and author_voice.channel:
            st.last_voice_channel_id = author_voice.channel.id

    def _get_text_channel(self, guild: discord.Guild, st: GuildState):
        if st.last_text_channel_id is None:
            return None
        return guild.get_channel(st.last_text_channel_id)

    def _non_bot_voice_user_ids(self, voice_channel) -> list[int]:
        bot_user_id = self.bot.user.id if self.bot.user else None
        return [user_id for user_id in voice_channel.voice_states.keys() if user_id != bot_user_id]

    def _cancel_empty_disconnect(self, st: GuildState):
        if st.empty_disconnect_task and not st.empty_disconnect_task.done():
            st.empty_disconnect_task.cancel()
        st.empty_disconnect_task = None

    def _cancel_prefetch(self, st: GuildState):
        if st.prefetch_task and not st.prefetch_task.done():
            st.prefetch_task.cancel()
        st.prefetch_task = None

    def _reset_playback_state(self, st: GuildState, *, clear_queue: bool = False):
        if clear_queue:
            st.queue.clear()
        st.current_title = None
        st.current_info = None
        st.current_file = None
        st.is_loading = False
        st.playback_started_at = None
        self._cancel_prefetch(st)

    @staticmethod
    def _track_query(info: dict) -> str | None:
        return info.get("original_url") or info.get("webpage_url") or info.get("title")

    async def _resolve_track_audio(
        self,
        info: dict,
        *,
        allow_stream_fallback: bool,
    ) -> tuple[dict, str]:
        audio_path = info.get("_audio_path")
        if audio_path and (audio_path.startswith("http") or os.path.exists(audio_path)):
            return info, audio_path

        query = self._track_query(info)
        if not query:
            raise RuntimeError("Track is missing a playable URL or title.")

        try:
            resolved_info, audio_path = await search_and_download(query, download=True)
        except Exception:
            if not allow_stream_fallback:
                raise
            logger.warning("Download failed for track, falling back to stream query=%r", query, exc_info=True)
            resolved_info, audio_path = await search_and_download(query, download=False)

        resolved_info["original_url"] = info.get("original_url", query)
        resolved_info["_audio_path"] = audio_path
        return resolved_info, audio_path

    def _schedule_empty_disconnect(self, guild: discord.Guild):
        st = self.state(guild.id)
        if st.empty_disconnect_task and not st.empty_disconnect_task.done():
            return
        st.empty_disconnect_task = self.bot.loop.create_task(self._empty_disconnect_watch(guild.id))

    async def _connect_to_voice_channel(self, guild: discord.Guild, voice_channel: discord.VoiceChannel) -> bool:
        st = self.state(guild.id)
        async with st.connection_lock:
            vc = guild.voice_client
            try:
                if vc:
                    if vc.is_connected():
                        if vc.channel == voice_channel:
                            return True
                        logger.info(
                            "Moving voice client guild=%s from=%s to=%s",
                            guild.id,
                            vc.channel,
                            voice_channel,
                        )
                        await vc.move_to(voice_channel)
                        return True

                    logger.warning("Found ghost or reconnecting voice client in guild=%s; cleaning it up", guild.id)
                    self._mark_expected_disconnect(st)
                    await vc.disconnect(force=True)
                    
                    # Wait for cleanup to reflect in guild.voice_client
                    for _ in range(20):
                        if guild.voice_client is None:
                            break
                        await asyncio.sleep(0.1)
                    
                    # Small extra buffer for gateway state to settle and avoid race with new CONNECT
                    await asyncio.sleep(0.5)

                logger.info("Connecting voice client guild=%s channel=%s", guild.id, voice_channel)
                # Setting reconnect=True is fine if we ensured a clean slate
                await voice_channel.connect(timeout=60.0, reconnect=True)
                st.last_voice_channel_id = voice_channel.id
                return True
            except Exception as exc:
                logger.exception("Voice connection failed guild=%s channel=%s: %s", guild.id, voice_channel, exc)
                return False

    def _create_audio_source(self, guild_id: int, audio_path: str, volume: float, *, seek_seconds: int = 0):
        # Optimized for local files and streaming (more stable on AWS)
        st = self.state(guild_id)
        is_url = audio_path.startswith("http")
        
        # Base FFmpeg options as a list to avoid shell quoting issues
        before_options = ["-nostdin", "-thread_queue_size", "8192"]
        if is_url:
            before_options.extend([
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5"
            ])
            
            # Use headers for cookies and user-agent
            auth_cfg = get_yt_dlp_auth_config()
            cookiefile = auth_cfg.get("cookiefile")
            user_agent = os.getenv("USER_AGENT") or DEFAULT_USER_AGENT
            
            headers = []
            if user_agent:
                headers.append(f"User-Agent: {user_agent}")
            # Required for c=WEB YouTube URLs — without this FFmpeg gets 403
            headers.append("Referer: https://www.youtube.com/")
            
            cookie_str = parse_cookies_for_ffmpeg(cookiefile)
            if cookie_str:
                headers.append(f"Cookie: {cookie_str}")
            
            if headers:
                # FFmpeg expects headers separated by \r\n and ending with \r\n
                header_str = "\r\n".join(headers) + "\r\n"
                before_options.extend(["-headers", header_str])

        if seek_seconds > 0:
            before_options.extend(["-ss", str(seek_seconds)])

        ffmpeg_options = f"-vn -loglevel warning -af volume={volume}"

        logger.debug("Creating audio source path=%s before_options=%r", audio_path, before_options)

        # Convert list to a properly quoted string for FFmpeg
        before_options_str = (
            " ".join(shlex.quote(arg) for arg in before_options)
            if isinstance(before_options, list)
            else before_options
        )

        try:
            return discord.FFmpegOpusAudio(
                audio_path,
                before_options=before_options_str,
                options=ffmpeg_options,
                bitrate=128
            )
        except Exception as exc:
            logger.warning("FFmpegOpusAudio failed for path=%s; falling back to PCMAudio: %s", audio_path, exc)
            return discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(
                    audio_path,
                    before_options=before_options_str,
                    options=ffmpeg_options,
                ),
                volume=volume,
            )

    async def _start_playback(
        self,
        guild: discord.Guild,
        info: dict,
        *,
        audio_path: str,
        announce_channel=None,
        announce_text: str | None = None,
        seek_seconds: int = 0,
    ):
        st = self.state(guild.id)
        vc = guild.voice_client
        if not vc or not vc.is_connected():
            raise RuntimeError("Voice client is not connected.")

        source = self._create_audio_source(guild.id, audio_path, st.volume, seek_seconds=seek_seconds)
        title = info.get("title", "Unknown")

        st.current_info = info
        st.current_file = audio_path
        st.current_title = title
        st.is_loading = False
        st.playback_started_at = time.monotonic() - max(seek_seconds, 0)

        # Include view for interactive controls
        view = MusicControlView(self.bot, guild.id)
        
        if announce_channel and announce_text:
            await announce_channel.send(announce_text, view=view)

        logger.info(
            "Starting playback guild=%s track=%s path=%s seek=%ss volume=%.2f",
            guild.id,
            track_label(info),
            audio_path,
            seek_seconds,
            st.volume,
        )
        vc.play(source, after=self._make_after_callback(guild.id))
        self._schedule_prefetch(guild.id)

    async def _recover_voice_connection(self, guild_id: int, *, reason: str):
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return

        st = self.state(guild_id)
        async with st.recovery_lock:
            vc = guild.voice_client
            if vc and vc.is_connected() and (vc.is_playing() or vc.is_paused()):
                return

            if not st.current_info and not st.queue:
                return

            if st.last_voice_channel_id is None:
                logger.warning("Cannot recover voice guild=%s: no saved voice channel", guild_id)
                return

            voice_channel = guild.get_channel(st.last_voice_channel_id)
            if not isinstance(voice_channel, discord.VoiceChannel):
                logger.warning(
                    "Cannot recover voice guild=%s: channel_id=%s not found",
                    guild_id,
                    st.last_voice_channel_id,
                )
                return

            logger.warning(
                "Attempting voice recovery guild=%s reason=%s current_track=%s queue_len=%s",
                guild_id,
                reason,
                track_label(st.current_info),
                len(st.queue),
            )
            if not await self._connect_to_voice_channel(guild, voice_channel):
                return

            text_channel = self._get_text_channel(guild, st)
            if st.current_info and st.current_file and os.path.exists(st.current_file):
                seek_seconds = 0
                if st.playback_started_at is not None:
                    seek_seconds = max(0, int(time.monotonic() - st.playback_started_at - 2))
                try:
                    await self._start_playback(
                        guild,
                        clone_info(st.current_info),
                        audio_path=st.current_file,
                        announce_channel=text_channel,
                        announce_text="⚠️ Voice connection dropped. Reconnected and resumed the current track.",
                        seek_seconds=seek_seconds,
                    )
                    return
                except Exception as exc:
                    logger.exception(
                        "Failed to resume current track guild=%s track=%s: %s",
                        guild_id,
                        track_label(st.current_info),
                        exc,
                    )

            if st.queue:
                next_info = st.queue.popleft()
                try:
                    await self._play_track_for_guild(
                        guild,
                        next_info,
                        text_channel=text_channel,
                        ensure_voice=False,
                        status_message="⚠️ Voice connection dropped. Reconnected and continuing with the queue.",
                    )
                except Exception as exc:
                    logger.exception(
                        "Failed to continue queue after reconnect guild=%s next_track=%s: %s",
                        guild_id,
                        track_label(next_info),
                        exc,
                    )

    async def _voice_watchdog(self):
        try:
            while True:
                await asyncio.sleep(30)
                for guild_id, st in list(self._states.items()):
                    if time.monotonic() < st.expected_disconnect_until:
                        continue
                    if not st.current_info:
                        continue
                    guild = self.bot.get_guild(guild_id)
                    if guild is None:
                        continue
                    vc = guild.voice_client
                    if vc is None or not vc.is_connected():
                        await self._recover_voice_connection(
                            guild_id,
                            reason="watchdog detected disconnected voice client",
                        )
        except asyncio.CancelledError:
            pass

    async def _empty_disconnect_watch(self, guild_id: int):
        st = self.state(guild_id)
        try:
            guild = self.bot.get_guild(guild_id)
            if guild is None:
                return
            vc = guild.voice_client
            if not vc or not vc.is_connected():
                return

            non_bot_ids = self._non_bot_voice_user_ids(vc.channel)
            if non_bot_ids:
                logger.info(
                    "Skipping empty-channel timer guild=%s channel=%s non_bot_ids=%s",
                    guild_id,
                    vc.channel,
                    non_bot_ids,
                )
                return

            logger.warning(
                "Voice channel became empty guild=%s channel=%s waiting=%ss current_track=%s queue_len=%s",
                guild_id,
                vc.channel,
                AUTO_DISCONNECT_EMPTY_DELAY,
                track_label(st.current_info),
                len(st.queue),
            )

            await asyncio.sleep(AUTO_DISCONNECT_EMPTY_DELAY)

            guild = self.bot.get_guild(guild_id)
            if guild is None:
                return
            vc = guild.voice_client
            if not vc or not vc.is_connected():
                return

            non_bot_ids = self._non_bot_voice_user_ids(vc.channel)
            if non_bot_ids:
                logger.info(
                    "Keeping voice connection guild=%s channel=%s non_bot_ids_after_wait=%s",
                    guild_id,
                    vc.channel,
                    non_bot_ids,
                )
                return

            self._mark_expected_disconnect(st)
            logger.warning(
                "Disconnecting because voice channel stayed empty guild=%s channel=%s delay=%ss",
                guild_id,
                vc.channel,
                AUTO_DISCONNECT_EMPTY_DELAY,
            )
            self._reset_playback_state(st, clear_queue=True)
            await vc.disconnect()
            await asyncio.to_thread(cleanup_all)
            gc.collect()
        except asyncio.CancelledError:
            logger.info("Cancelled empty-channel timer guild=%s", guild_id)
        finally:
            if st.empty_disconnect_task is asyncio.current_task():
                st.empty_disconnect_task = None

    async def cog_check(self, ctx: commands.Context) -> bool:
        """Restrict music commands to one text channel per guild."""
        if ctx.guild is None:
            await ctx.send("\u274c Music commands can only be used in a server.")
            return False

        # Default to 'music-bot' if not specified in environment
        music_channel_name = os.getenv("MUSIC_TEXT_CHANNEL", "music-bot")

        if isinstance(ctx.channel, discord.TextChannel) and ctx.channel.name == music_channel_name:
            return True

        await ctx.send(f"\u274c Music commands can only be used in the #{music_channel_name} channel.")
        return False

    # ── internal playback ────────────────────────────────────────────────────

    async def _ensure_voice(self, ctx: commands.Context) -> bool:
        """Connect/move to the author's voice channel. Returns False on failure."""
        self._remember_context(ctx)
        try:
            if not ctx.author.voice:
                await ctx.send("\u274c Join a voice channel first.")
                return False

            connected = await self._connect_to_voice_channel(ctx.guild, ctx.author.voice.channel)
            if not connected:
                await ctx.send("\u274c Voice connection failed.")
            return connected
        except discord.ClientException as exc:
            await ctx.send(f"\u274c Could not join voice: {exc}")
            logger.exception("Could not join voice guild=%s: %s", ctx.guild.id, exc)
            return False
        except Exception as exc:
            await ctx.send(f"\u274c Voice connection failed: {exc}")
            logger.exception("Voice connection failed guild=%s: %s", ctx.guild.id, exc)
            return False
        return True

    async def _play_track(self, ctx: commands.Context, info: dict, *, ensure_voice: bool = True):
        self._remember_context(ctx)
        await self._play_track_for_guild(
            ctx.guild,
            info,
            text_channel=ctx.channel,
            ensure_voice=ensure_voice,
        )

    async def _play_track_for_guild(
        self,
        guild: discord.Guild,
        info: dict,
        *,
        text_channel=None,
        ensure_voice: bool = True,
        status_message: str | None = None,
    ):
        """Start playing a track. Uses cached file or fast stream URL."""
        st = self.state(guild.id)

        if ensure_voice:
            voice_channel = guild.get_channel(st.last_voice_channel_id) if st.last_voice_channel_id else None
            if not isinstance(voice_channel, discord.VoiceChannel):
                if text_channel:
                    await text_channel.send("\u274c Join a voice channel first.")
                return
            if not await self._connect_to_voice_channel(guild, voice_channel):
                if text_channel:
                    await text_channel.send("\u274c Voice connection failed.")
                return

        st.is_loading = True
        st.current_info = info
        title = info.get('title', 'Unknown')

        # Use local file if available, otherwise download or get stream URL
        audio_path = info.get('_audio_path')
        if not audio_path or (not audio_path.startswith("http") and not os.path.exists(audio_path)):
            try:
                query = info.get('original_url') or info.get('webpage_url') or info.get('title')
                # Try download first for reliability
                try:
                    info, audio_path = await search_and_download(query, download=True)
                except Exception as e:
                    logger.warning("Download failed for track, falling back to stream: %s", e)
                    # Fallback to streaming if download fails
                    info, audio_path = await search_and_download(query, download=False)
                
                st.current_info = info
                title = info.get('title', title)
            except Exception as e:
                if text_channel:
                    await text_channel.send(f"\u274c Could not load track: {e}")
                st.is_loading = False
                self._advance(guild.id)
                return

        st.current_file = audio_path

        try:
            if guild.voice_client:
                loop_suffix = f" (Loop: {st.loop_mode})" if st.loop_mode != "off" else ""
                announce_text = status_message or (
                    f"\u25b6\ufe0f Now playing: **{title}**{loop_suffix}" if text_channel else None
                )
                await self._start_playback(
                    guild,
                    info,
                    audio_path=audio_path,
                    announce_channel=text_channel,
                    announce_text=announce_text,
                )
            else:
                st.current_title = None
                st.is_loading = False
        except Exception as exc:
            if text_channel:
                await text_channel.send(f"\u274c Playback failed: {exc}")
            st.current_title = None
            st.is_loading = False
            self._advance(guild.id)

    def _schedule_prefetch(self, guild_ref):
        guild_id = guild_ref.guild.id if hasattr(guild_ref, "guild") else guild_ref
        st = self.state(guild_id)
        if st.prefetch_task and not st.prefetch_task.done():
            return
        st.prefetch_task = self.bot.loop.create_task(self._prefetch_next(guild_id))

    async def _prefetch_next(self, guild_id: int):
        """Pre-download audio for the next track while current plays."""
        st = self.state(guild_id)
        current_task = asyncio.current_task()
        try:
            # Short configurable delay so prefetch starts soon without competing with track startup.
            await asyncio.sleep(_PREFETCH_DELAY_SECONDS)
            
            if not st.queue:
                return
            next_track = st.queue[0]
            try:
                query = self._track_query(next_track)
                if query:
                    info, path = await self._resolve_track_audio(next_track, allow_stream_fallback=False)
                    if st.queue and st.queue[0] is next_track:
                        st.queue[0].update(info)
                        st.queue[0]["_audio_path"] = path
                        logger.info("Prefetched next track guild=%s track=%s", guild_id, track_label(info))
            except Exception as e:
                logger.warning("Prefetch failed guild=%s track=%s: %s", guild_id, track_label(next_track), e)
        finally:
            if st.prefetch_task is current_task:
                st.prefetch_task = None

    def _make_after_callback(self, guild_id: int):
        def _after(error):
            if error:
                logger.warning("Voice playback error guild=%s: %s", guild_id, error)
            else:
                logger.info("Playback finished guild=%s", guild_id)
            gc.collect() # Reclaim memory from the finished stream
            asyncio.run_coroutine_threadsafe(self._handle_after(guild_id, error), self.bot.loop)
        return _after

    async def _handle_after(self, guild_id: int, error):
        guild = self.bot.get_guild(guild_id)
        vc = guild.voice_client if guild else None
        if error and (vc is None or not vc.is_connected()):
            await self._recover_voice_connection(guild_id, reason=f"playback error: {error}")
            return
        self._advance(guild_id)

    def _advance(self, guild_id: int):
        """Called when a track ends \u2014 pops the next item from the queue."""
        asyncio.run_coroutine_threadsafe(self._advance_async(guild_id), self.bot.loop)

    async def _advance_async(self, guild_id: int):
        st = self.state(guild_id)
        async with st.advance_lock:
            next_info = None
            if st.loop_mode == "song" and st.current_info:
                next_info = clone_info(st.current_info)
                # Preserve audio path for looped song
                if st.current_file:
                    next_info['_audio_path'] = st.current_file
            else:
                if st.loop_mode == "queue" and st.current_info:
                    queued = clone_info(st.current_info)
                    if st.current_file:
                        queued['_audio_path'] = st.current_file
                    st.queue.append(queued)
                if st.queue:
                    next_info = st.queue.popleft()
                else:
                    self._reset_playback_state(st)

        if next_info:
            guild = self.bot.get_guild(guild_id)
            if guild is not None:
                await self._play_track_for_guild(
                    guild,
                    next_info,
                    text_channel=self._get_text_channel(guild, st),
                )

    # ── commands ─────────────────────────────────────────────────────────────

    @commands.command()
    async def join(self, ctx):
        """Join your voice channel."""
        await self._ensure_voice(ctx)

    @commands.command(aliases=['p'])
    async def play(self, ctx, *, query: str):
        """Play a song or add it to the queue."""
        if ctx.voice_client is None and not ctx.author.voice:
            await ctx.send("\u274c Join a voice channel first.")
            return

        st = self.state(ctx.guild.id)
        self._remember_context(ctx)
        logger.info("Play command guild=%s user=%s query=%r", ctx.guild.id, ctx.author.id, query)

        searching_msg = await ctx.send(f"\ud83d\udd0d Searching for `{query}`...")
        try:
            s_start = time.perf_counter()
            voice_ok = await self._ensure_voice(ctx)
            logger.info("Voice prepare guild=%s took %.2fs", ctx.guild.id, time.perf_counter() - s_start)

            # Check if it's a Spotify playlist/album for lazy loading
            playlist_tracks = None
            if "open.spotify.com" in query and ("/playlist/" in query or "/album/" in query):
                playlist_tracks = await extract_spotify_metadata(query)

            if isinstance(playlist_tracks, list) and playlist_tracks:
                logger.info("Spotify playlist detected with %d tracks, loading first one.", len(playlist_tracks))
                # Resolve first track immediately using a stream URL so playback starts faster.
                info, audio_path = await search_and_download(playlist_tracks[0], download=False)
                info['original_url'] = playlist_tracks[0]
                info['_audio_path'] = audio_path

                # Add remaining tracks as placeholders (will be resolved when played)
                for track_query in playlist_tracks[1:50]:
                    st.queue.append({
                        'title': track_query,
                        'original_url': track_query,
                    })

                added_msg = f"\U0001f4cb Added **{len(playlist_tracks[:50])}** tracks from Spotify."
            else:
                # Single track or normal search
                s_dl = time.perf_counter()
                info, audio_path = await search_and_download(query, download=False)
                elapsed = time.perf_counter() - s_dl
                logger.info("Search+stream guild=%s took %.2fs", ctx.guild.id, elapsed)

                info['original_url'] = query
                info['_audio_path'] = audio_path
                added_msg = f"\U0001f4cb Added to queue: **{info.get('title')}**"

        except Exception as e:
            if 'searching_msg' in locals():
                await searching_msg.delete()
            logger.exception("Error loading track guild=%s query=%r: %s", ctx.guild.id, query, e)
            return await ctx.send(f"\u274c Could not load track: {e}")
        if 'searching_msg' in locals():
            await searching_msg.delete()
            
        if not voice_ok:
            return

        vc = ctx.voice_client
        if vc.is_playing() or vc.is_paused() or st.is_loading:
            st.queue.append(info)
            if playlist_tracks:
                await ctx.send(added_msg)
            else:
                pos = len(st.queue)
                await ctx.send(f"\U0001f4cb Added to queue (#{pos}): **{info.get('title')}**")
            self._schedule_prefetch(ctx)
        else:
            if playlist_tracks:
                await ctx.send(added_msg)
            await self._play_track(ctx, info, ensure_voice=False)

    @commands.command()
    async def skip(self, ctx, count: int = 1):
        """Skip the current song or multiple songs."""
        st = self.state(ctx.guild.id)
        if not ctx.voice_client:
            return await ctx.send("\u274c Not connected to voice.")

        if count < 1:
            return await ctx.send("\u274c Skip count must be at least 1.")

        if ctx.voice_client.is_playing() or ctx.voice_client.is_paused() or st.is_loading:
            # If skipping more than 1, remove the extras from the queue first
            if count > 1:
                skipped_from_queue = 0
                for _ in range(count - 1):
                    if st.queue:
                        st.queue.popleft()
                        skipped_from_queue += 1
                
                # If we were loading, stop loading the current one as well
                if st.is_loading:
                    st.is_loading = False
                
                await ctx.send(f"\u23ed\ufe0f Skipped **{skipped_from_queue + 1}** tracks.")
            else:
                await ctx.send("\u23ed\ufe0f Skipped.")

            # Stop current playback (this triggers _advance via the 'after' callback)
            original_loop = st.loop_mode
            if st.loop_mode == "song":
                st.loop_mode = "off"
            
            if ctx.voice_client.is_playing() or ctx.voice_client.is_paused():
                ctx.voice_client.stop()
            else:
                # If it was just loading, we manually advance
                self._advance(ctx.guild.id)

            if original_loop == "song":
                await asyncio.sleep(0.5)
                st.loop_mode = "song"
        elif st.queue:
            # Handle case where nothing is playing but there's a queue
            skipped = 0
            for _ in range(count):
                if st.queue:
                    st.queue.popleft()
                    skipped += 1
            await ctx.send(f"\u23ed\ufe0f Skipped **{skipped}** tracks from queue.")
            self._advance(ctx.guild.id)
        else:
            await ctx.send("\u274c Nothing is playing and the queue is empty.")

    @commands.command()
    async def loop(self, ctx, mode: str = None):
        """Change loop mode: off, song, queue."""
        st = self.state(ctx.guild.id)
        valid_modes = ["off", "song", "queue"]
        if mode is None:
            idx = (valid_modes.index(st.loop_mode) + 1) % len(valid_modes)
            st.loop_mode = valid_modes[idx]
        elif mode.lower() in valid_modes:
            st.loop_mode = mode.lower()
        else:
            return await ctx.send(f"\u274c Invalid mode. Use: `!loop <off|song|queue>`")
        emoji = {"off": "\u27a1\ufe0f", "song": "\U0001f502", "queue": "\U0001f501"}
        await ctx.send(f"{emoji[st.loop_mode]} Loop mode set to: **{st.loop_mode}**")

    @commands.command()
    async def shuffle(self, ctx):
        """Shuffle the current queue."""
        st = self.state(ctx.guild.id)
        if len(st.queue) < 2:
            return await ctx.send("\u274c Not enough songs in queue to shuffle.")
        temp_list = list(st.queue)
        random.shuffle(temp_list)
        st.queue = deque(temp_list)
        await ctx.send("\U0001f500 Queue shuffled.")

    @commands.command(aliases=['cl'])
    async def clear(self, ctx):
        """Clear the entire queue."""
        st = self.state(ctx.guild.id)
        st.queue.clear()
        self._cancel_prefetch(st)
        await ctx.send("\U0001f5d1\ufe0f Queue cleared.")

    @commands.command(aliases=['rm'])
    async def remove(self, ctx, index: int):
        """Remove a song from the queue by its index."""
        st = self.state(ctx.guild.id)
        if index < 1 or index > len(st.queue):
            return await ctx.send(f"\u274c Invalid index. Use `!q` to see song numbers.")
        temp_list = list(st.queue)
        removed = temp_list.pop(index - 1)
        st.queue = deque(temp_list)
        await ctx.send(f"\U0001f5d1\ufe0f Removed: **{removed.get('title')}**")

    @commands.command()
    async def pause(self, ctx):
        """Pause playback."""
        if ctx.voice_client and ctx.voice_client.is_playing():
            ctx.voice_client.pause()
            await ctx.send("\u23f8\ufe0f Paused.")
        else:
            await ctx.send("\u274c Nothing is playing.")

    @commands.command()
    async def resume(self, ctx):
        """Resume paused playback."""
        if ctx.voice_client and ctx.voice_client.is_paused():
            ctx.voice_client.resume()
            await ctx.send("\u25b6\ufe0f Resumed.")
        else:
            await ctx.send("\u274c Not paused.")

    @commands.command()
    async def stop(self, ctx):
        """Stop playback, clear the queue, and leave."""
        st = self.state(ctx.guild.id)
        self._mark_expected_disconnect(st)
        self._reset_playback_state(st, clear_queue=True)
        if ctx.voice_client:
            await ctx.voice_client.disconnect()
        await asyncio.to_thread(cleanup_all)
        gc.collect()
        await ctx.send("\u23f9\ufe0f Stopped and left the channel.")

    @commands.command(aliases=['q'])
    async def queue(self, ctx):
        """Show the current queue."""
        st = self.state(ctx.guild.id)
        if not st.current_title and not st.queue and not st.is_loading:
            return await ctx.send("\U0001f4cb Queue is empty.")

        lines = []
        if st.is_loading:
            lines.append("\u23f3 **Loading next song...**")
        elif st.current_title:
            lines.append(f"\u25b6\ufe0f **Now playing:** {st.current_title}")

        for i, info in enumerate(list(st.queue)[:10], 1):
            title = info.get('title', 'Unknown')
            lines.append(f"`{i}.` {title}")

        if len(st.queue) > 10:
            lines.append(f"\u2026 and {len(st.queue) - 10} more")

        await ctx.send("\n".join(lines))

    @commands.command(aliases=['vol'])
    async def volume(self, ctx, level: int):
        """Set volume from 1 to 100."""
        if not 1 <= level <= 100:
            return await ctx.send("\u274c Volume must be between 1 and 100.")
        st = self.state(ctx.guild.id)
        st.volume = level / 100
        
        if ctx.voice_client and ctx.voice_client.is_playing():
             await ctx.send(f"\u2705 Volume set to **{level}%** (will apply to the next song).")
        else:
             await ctx.send(f"\u2705 Volume set to **{level}%**")

    @commands.command(aliases=['np'])
    async def nowplaying(self, ctx):
        """Show the currently playing song."""
        st = self.state(ctx.guild.id)
        if st.is_loading:
            await ctx.send("\u23f3 Loading next song...")
        elif st.current_title:
            await ctx.send(f"\u25b6\ufe0f Now playing: **{st.current_title}**")
        else:
            await ctx.send("\u274c Nothing is playing.")

    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member == self.bot.user:
            st = self.state(member.guild.id)
            before_channel = getattr(before.channel, "name", None)
            after_channel = getattr(after.channel, "name", None)
            logger.info(
                "Bot voice state changed guild=%s before=%s after=%s",
                member.guild.id,
                before_channel,
                after_channel,
            )
            if after.channel is not None:
                st.last_voice_channel_id = after.channel.id
            elif (
                before.channel is not None
                and time.monotonic() >= st.expected_disconnect_until
                and (st.current_info or st.queue)
            ):
                self.bot.loop.create_task(
                    self._recover_voice_connection(
                        member.guild.id,
                        reason="voice_state_update detected unexpected disconnect",
                    )
                )
            return
        if not AUTO_DISCONNECT_WHEN_EMPTY:
            return
        vc = member.guild.voice_client
        if not vc or not vc.is_connected():
            return
        current_channel_id = vc.channel.id
        before_id = getattr(before.channel, "id", None)
        after_id = getattr(after.channel, "id", None)
        if before_id != current_channel_id and after_id != current_channel_id:
            return

        st = self.state(member.guild.id)
        non_bot_ids = self._non_bot_voice_user_ids(vc.channel)
        if non_bot_ids:
            logger.info(
                "Voice channel still occupied guild=%s channel=%s non_bot_ids=%s trigger_member=%s",
                member.guild.id,
                vc.channel,
                non_bot_ids,
                member.id,
            )
            self._cancel_empty_disconnect(st)
            return

        self._schedule_empty_disconnect(member.guild)


async def setup(bot):
    await bot.add_cog(Music(bot))
