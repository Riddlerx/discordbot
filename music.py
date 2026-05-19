import discord
from discord.ext import commands
import yt_dlp
import asyncio
import os
import shlex
import random
import tempfile
import glob
import time
import gc
import logging
import re
import aiohttp
import html as html_lib
from concurrent.futures import ThreadPoolExecutor
from collections import deque

TEMP_DIR = os.path.join(tempfile.gettempdir(), 'discord_music')
os.makedirs(TEMP_DIR, exist_ok=True)
logger = logging.getLogger("discordbot.music")
AUTO_DISCONNECT_WHEN_EMPTY = os.getenv("AUTO_DISCONNECT_WHEN_EMPTY", "true").strip().lower() in ("1", "true", "yes", "on")
AUTO_DISCONNECT_EMPTY_DELAY = int(os.getenv("AUTO_DISCONNECT_EMPTY_DELAY", "60"))

# ── Spotify Metadata Extraction ──────────────────────────────────────────────

async def _extract_spotify_metadata(url: str) -> list[str] | str | None:
    """Extract song title and artist from a Spotify URL using simple HTML scraping.
    Returns a list of queries if it's a playlist/album, or a single query string for a track.
    """
    try:
        # Handle spotify.link (mobile redirects)
        if "spotify.link" in url:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, allow_redirects=True, timeout=10) as resp:
                    url = str(resp.url)
                    logger.info("Redirected spotify.link to: %s", url)

        # Convert to embed URL for better scraping
        embed_url = url
        if "/embed/" not in url:
            embed_url = url.replace("open.spotify.com/track/", "open.spotify.com/embed/track/")
            embed_url = embed_url.replace("open.spotify.com/playlist/", "open.spotify.com/embed/playlist/")
            embed_url = embed_url.replace("open.spotify.com/album/", "open.spotify.com/embed/album/")
        
        # Remove query params for a cleaner URL
        if "?" in embed_url:
            embed_url = embed_url.split("?")[0]

        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(embed_url, timeout=10) as response:
                if response.status != 200:
                    logger.warning("Spotify metadata request failed with status %d for %s", response.status, embed_url)
                    return None
                html = await response.text()
                
                is_collection = "/playlist/" in embed_url or "/album/" in embed_url
                tracks = []
                
                # Pattern 1: JSON-LD like structure (Most reliable)
                # "title":"Track Name","artists":[{"name":"Artist Name"}]
                # We use a permissive regex to handle whitespace/formatting variations
                t_matches = re.findall(r'\"title\":\"([^\"]+)\".*?\"artists\":\[\{.*?\"name\":\"([^\"]+)\"', html)
                for t_name, a_name in t_matches:
                    t, a = html_lib.unescape(t_name).strip(), html_lib.unescape(a_name).strip()
                    if t.lower() not in ("spotify", "playlist", "album"):
                        tracks.append(f"{t} {a}")

                # Pattern 2: title/subtitle (Common in playlists)
                if not tracks:
                    ts_matches = re.findall(r'\"title\":\"([^\"]+)\",\"subtitle\":\"([^\"]+)\"', html)
                    for t_name, s_name in ts_matches:
                        t, s = html_lib.unescape(t_name).strip(), html_lib.unescape(s_name).strip()
                        if t.lower() not in ("spotify", "playlist", "album"):
                            tracks.append(f"{t} {s}")

                # Pattern 3: name/artists array (Backup)
                if not tracks:
                    na_matches = re.findall(r'\"name\":\"([^\"]+)\",\"artists\":\[\{.*?\"name\":\"([^\"]+)\"', html)
                    for t_name, a_name in na_matches:
                        t, a = html_lib.unescape(t_name).strip(), html_lib.unescape(a_name).strip()
                        if t.lower() not in ("spotify", "playlist", "album"):
                            tracks.append(f"{t} {a}")

                if tracks:
                    if is_collection:
                        logger.info("Extracted %d tracks from Spotify collection: %s", len(tracks), url)
                        return tracks
                    return tracks[0]
                
                # Final Fallback: Open Graph Metadata
                og_title = re.search(r'property="og:title"\s+content="([^"]+)"', html)
                if og_title:
                    title = html_lib.unescape(og_title.group(1).replace(" | Spotify", "").strip())
                    if title.lower() not in ("spotify", "playlist", "album"):
                        og_desc = re.search(r'property="og:description"\s+content="([^"]+)"', html)
                        if og_desc:
                            desc = html_lib.unescape(og_desc.group(1))
                            artist = desc.split(" · ")[0] if " · " in desc else ""
                            return f"{title} {artist}".strip()
                        return title

                # If all failed, log a snippet for debugging
                snippet = html[:500].replace("\n", " ")
                logger.warning("Spotify extraction failed for %s. Snippet: %s", embed_url, snippet)
                return None
    except Exception as e:
        logger.warning("Failed to extract Spotify metadata from %s: %s", url, e)
        return None

# ── yt-dlp options ─────────────────────────────────────────────────────────────

YDL_OPTIONS_FAST = {
    'format': 'bestaudio[ext=webm][vcodec=none]/bestaudio/best',
    'noplaylist': True,
    'default_search': 'ytsearch1',
    'quiet': True,
    'no_warnings': True,
    'no_color': True,
    'js_runtimes': {'node': {}},
    'force_ipv4': True,
    'retries': 5,
    'fragment_retries': 5,
    'concurrent_fragment_downloads': 5,
    # GCP Speed Optimizations
    'nocheckcertificate': True,
    'youtube_include_dash_manifest': False,
    'youtube_include_hls_manifest': False,
    'check_formats': 'cached',
    # Minimal extraction for speed
    'skip_download': False,
    'writethumbnail': False,
    'writesubtitles': False,
    'writeautomaticsub': False,
    'getcomments': False,
    'cachedir': os.path.join(tempfile.gettempdir(), 'yt_dlp_cache'),
    'user_agent': os.getenv("USER_AGENT", 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36'),
    'cookiefile': '/home/ubuntu/discordbot/cookies.txt',
    'proxy': os.getenv("YTDLP_PROXY"),
    # Use a more effective player client configuration
    'extractor_args': {"youtube": {"player_client": "web"}}, 
    'lazy_playlist': True,
    'playlist_items': '1',
    'noplaylist': True,
    'noprogress': True,
    'no_part': True,
    'buffersize': 16384,
    'outtmpl': os.path.join(TEMP_DIR, '%(id)s.%(ext)s'),
}

YDL_OPTIONS_FALLBACK = {
    **YDL_OPTIONS_FAST,
    'format': 'bestaudio/best',
}

def _get_yt_dlp_auth_config() -> dict:
    """Return yt-dlp auth-related options from environment variables."""
    cookies_path = os.getenv("YTDLP_COOKIES") or os.getenv("YOUTUBE_COOKIES_PATH")
    cookies_from_browser = os.getenv("YTDLP_COOKIES_FROM_BROWSER")
    auth_options: dict = {}

    if cookies_from_browser:
        auth_options["cookiesfrombrowser"] = (cookies_from_browser,)
    elif cookies_path:
        if os.path.exists(cookies_path):
            auth_options["cookiefile"] = cookies_path

    return auth_options


def _build_ydl_options(base_options: dict) -> dict:
    """Clone base yt-dlp options and apply auth and environment configuration."""
    auth_cfg = _get_yt_dlp_auth_config()
    options = {
        **base_options,
        **auth_cfg,
    }

    force_ipv4 = os.getenv("YTDLP_FORCE_IPV4")
    if force_ipv4 is not None:
        options["force_ipv4"] = force_ipv4.lower() in ("1", "true", "yes", "on")

    js_runtime = os.getenv("YTDLP_JS_RUNTIME")
    if js_runtime:
        options["js_runtimes"] = {js_runtime: {}}

    remote_components = os.getenv("YTDLP_REMOTE_COMPONENTS")
    if remote_components:
        options["remote_components"] = remote_components

    if auth_cfg.get("cookiefile"):
        logger.info("Using yt-dlp cookies from %s", auth_cfg["cookiefile"])
    elif base_options.get("cookiefile"):
        logger.info("Using default yt-dlp cookies from %s", base_options["cookiefile"])
    
    return options


_ydl_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="yt-dlp")
_extract_semaphore = asyncio.Semaphore(1)
_info_cache: dict[str, tuple[float, dict]] = {}
_info_cache_lock = asyncio.Lock()
_inflight_queries: dict[str, asyncio.Future] = {}
_inflight_queries_lock = asyncio.Lock()
_INFO_CACHE_TTL = 3600
_STARTUP_WARMUP_DELAY = 5
_STARTUP_WARMUP_YOUTUBE = os.getenv("MUSIC_WARMUP_YOUTUBE", "").strip().lower() in ("1", "true", "yes", "on")


def _normalize_query(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().lower()


def _clone_info(info: dict) -> dict:
    return dict(info)


def _track_label(info: dict | None) -> str:
    if not info:
        return "unknown"
    title = info.get("title") or "unknown"
    video_id = info.get("id") or "unknown"
    return f"{title} [{video_id}]"


async def _read_cached_info(keys: list[str]) -> dict | None:
    now = time.monotonic()
    async with _info_cache_lock:
        for key in keys:
            cached = _info_cache.get(key)
            if cached and now - cached[0] < _INFO_CACHE_TTL:
                return _clone_info(cached[1])
    return None


async def _store_cached_info(info: dict, *keys: str | None):
    now = time.monotonic()
    cached_info = _clone_info(info)
    cache_keys = {_normalize_query(key) for key in keys}
    cache_keys.update(
        {
            _normalize_query(info.get("id")),
            _normalize_query(info.get("webpage_url")),
            _normalize_query(info.get("original_url")),
            _normalize_query(info.get("title")),
        }
    )
    async with _info_cache_lock:
        expired = [key for key, (ts, _) in _info_cache.items() if now - ts >= _INFO_CACHE_TTL]
        for key in expired:
            _info_cache.pop(key, None)
        for key in cache_keys:
            if key:
                _info_cache[key] = (now, cached_info)


def get_audio_path(video_id: str) -> str | None:
    """Find the downloaded audio file for a video ID."""
    patterns = [
        os.path.join(TEMP_DIR, f'{video_id}.opus'),
        os.path.join(TEMP_DIR, f'{video_id}.m4a'),
        os.path.join(TEMP_DIR, f'{video_id}.webm'),
        os.path.join(TEMP_DIR, f'{video_id}.mp4'),
        os.path.join(TEMP_DIR, f'{video_id}.*'),
    ]
    for pattern in patterns:
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
    return None


def cleanup_file(filepath: str):
    """Remove a temp audio file."""
    try:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
    except Exception:
        pass


def cleanup_all():
    """Remove all temp audio files."""
    for f in glob.glob(os.path.join(TEMP_DIR, '*')):
        try:
            os.remove(f)
        except Exception:
            pass


# ── Core: single-call search + download ───────────────────────────────────────

async def search_and_download(query: str, *, refresh: bool = False, download: bool = True) -> tuple[dict, str]:
    """Search YouTube, extract info, and optionally download audio.

    Returns (info_dict, audio_path_or_url).
    """
    # 0. Handle Spotify URLs by converting them to YouTube search queries
    # Note: Collections (playlists/albums) should be handled by the caller (e.g. play command)
    # to avoid blocking on multiple downloads.
    original_query = query
    is_spotify = "open.spotify.com" in query
    if is_spotify:
        metadata = await _extract_spotify_metadata(query)
        if isinstance(metadata, list):
            # If a list is returned, just take the first one for this single-item call
            query = metadata[0] if metadata else query
        elif metadata:
            query = metadata
        else:
            raise Exception("Could not extract song info from this Spotify link. Please try searching for the song name instead.")
        
        # Refinement: Append 'audio' to prioritize studio versions over music videos
        if not query.lower().endswith("audio") and not query.lower().endswith("lyrics"):
            query = f"{query} audio"

    normalized = _normalize_query(query)

    # 1. Check cache — if we already have info + file on disk, return immediately
    if not refresh:
        cache_lookup = [normalized] if normalized else []
        if is_spotify:
            cache_lookup.append(_normalize_query(original_query))
        
        cached = await _read_cached_info(cache_lookup)
        if cached and cached.get('id'):
            existing_path = get_audio_path(cached['id'])
            if existing_path:
                return cached, existing_path

    # 2. Dedup in-flight requests
    inflight_key = f"refresh:{normalized}:{download}" if refresh else f"{normalized or query}:{download}"
    future: asyncio.Future | None = None
    is_owner = False
    async with _inflight_queries_lock:
        future = _inflight_queries.get(inflight_key)
        if future is None:
            future = asyncio.get_running_loop().create_future()
            _inflight_queries[inflight_key] = future
            is_owner = True

    if not is_owner:
        result = await asyncio.shield(future)
        return _clone_info(result[0]), result[1]

    # 3. yt-dlp call
    try:
        loop = asyncio.get_running_loop()

        def _do_extract():
            opts = _build_ydl_options(YDL_OPTIONS_FAST)
            opts['skip_download'] = not download
            
            if query.startswith(('http://', 'https://')):
                opts['default_search'] = 'auto'
            
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(query, download=download)
            except yt_dlp.utils.DownloadError as e:
                err_msg = str(e)
                if "[DRM]" in err_msg or "DRM protected" in err_msg:
                    raise Exception("This content is DRM protected and cannot be played directly. Try searching for the song name instead.") from e
                raise

            if info and 'entries' in info:
                if not info['entries']:
                    raise Exception("No results found.")
                info = info['entries'][0]

            if not info or not info.get('id'):
                raise Exception("Could not extract video info.")

            # If we downloaded it, return the path. Otherwise, return the stream URL.
            if download:
                path = get_audio_path(info['id'])
                if not path:
                    raise Exception(f"Download finished but file not found for {info.get('id')}")
                return info, path
            
            return info, info['url']

        async with _extract_semaphore:
            info, result_path = await loop.run_in_executor(_ydl_executor, _do_extract)

        if download:
            await _store_cached_info(info, query, original_query if is_spotify else None)
        
        future.set_result((_clone_info(info), result_path))
        return info, result_path

    except Exception as exc:
        logger.warning("yt-dlp extraction failed query=%r refresh=%s download=%s: %s", query, refresh, download, exc)
        future.set_exception(exc)
        future.exception()
        raise
    finally:
        async with _inflight_queries_lock:
            if _inflight_queries.get(inflight_key) is future:
                _inflight_queries.pop(inflight_key, None)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _parse_cookies_for_ffmpeg(cookiefile: str) -> str:
    """Parse Netscape cookies file into a semicolon-separated string for FFmpeg."""
    if not cookiefile or not os.path.exists(cookiefile):
        return ""
    
    cookies = []
    try:
        with open(cookiefile, 'r') as f:
            for line in f:
                if not line.strip() or line.startswith('#'):
                    continue
                parts = line.split('\t')
                if len(parts) >= 7:
                    # name is at index 5, value at index 6
                    cookies.append(f"{parts[5]}={parts[6].strip()}")
        return "; ".join(cookies)
    except Exception as e:
        logger.warning("Failed to parse cookies for FFmpeg: %s", e)
        return ""


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
        cleanup_all()
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
                await asyncio.sleep(3600)  # Every hour
                now = time.time()
                for f in glob.glob(os.path.join(TEMP_DIR, '*')):
                    try:
                        # If file is older than 2 hours, remove it
                        if os.path.isfile(f) and now - os.path.getmtime(f) > 7200:
                            os.remove(f)
                    except Exception:
                        pass
        except asyncio.CancelledError:
            pass

    async def _warmup_extractors(self):
        try:
            await asyncio.sleep(_STARTUP_WARMUP_DELAY)
            loop = asyncio.get_running_loop()
            start = time.perf_counter()
            # Warm up by triggering lazy extractor loading
            await loop.run_in_executor(
                _ydl_executor,
                lambda: yt_dlp.YoutubeDL(_build_ydl_options(YDL_OPTIONS_FAST))._ies,
            )
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.info("Music extractors warmed in %.0fms", elapsed_ms)

            if _STARTUP_WARMUP_YOUTUBE:
                start = time.perf_counter()
                await loop.run_in_executor(
                    _ydl_executor,
                    lambda: yt_dlp.YoutubeDL(_build_ydl_options(YDL_OPTIONS_FAST)).extract_info(
                        "ytsearch1:youtube", download=False
                    ),
                )
                elapsed_ms = (time.perf_counter() - start) * 1000
                logger.info("Music YouTube warmup finished in %.0fms", elapsed_ms)
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
            auth_cfg = _get_yt_dlp_auth_config()
            cookiefile = auth_cfg.get("cookiefile") or YDL_OPTIONS_FAST.get("cookiefile")
            user_agent = os.getenv("USER_AGENT") or YDL_OPTIONS_FAST.get("user_agent")
            
            headers = []
            if user_agent:
                headers.append(f"User-Agent: {user_agent}")
            # Required for c=WEB YouTube URLs — without this FFmpeg gets 403
            headers.append("Referer: https://www.youtube.com/")
            
            cookie_str = _parse_cookies_for_ffmpeg(cookiefile)
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
            _track_label(info),
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
                _track_label(st.current_info),
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
                        _clone_info(st.current_info),
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
                        _track_label(st.current_info),
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
                        _track_label(next_info),
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
                _track_label(st.current_info),
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
            st.queue.clear()
            st.current_title = None
            st.current_info = None
            st.current_file = None
            st.is_loading = False
            st.playback_started_at = None
            if st.prefetch_task and not st.prefetch_task.done():
                st.prefetch_task.cancel()
                st.prefetch_task = None
            await vc.disconnect()
            cleanup_all()
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
            # Small delay to let the current playback stabilize
            await asyncio.sleep(5)
            
            if not st.queue:
                return
            next_track = st.queue[0]
            try:
                query = next_track.get('original_url') or next_track.get('webpage_url') or next_track.get('title')
                if query:
                    info, path = await search_and_download(query)
                    if st.queue and st.queue[0] is next_track:
                        st.queue[0].update(info)
                        st.queue[0]['_audio_path'] = path
                        logger.info("Prefetched next track guild=%s track=%s", guild_id, _track_label(info))
            except Exception as e:
                logger.warning("Prefetch failed guild=%s track=%s: %s", guild_id, _track_label(next_track), e)
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
                next_info = _clone_info(st.current_info)
                # Preserve audio path for looped song
                if st.current_file:
                    next_info['_audio_path'] = st.current_file
            else:
                if st.loop_mode == "queue" and st.current_info:
                    queued = _clone_info(st.current_info)
                    if st.current_file:
                        queued['_audio_path'] = st.current_file
                    st.queue.append(queued)
                if st.queue:
                    next_info = st.queue.popleft()
                else:
                    st.current_title = None
                    st.current_info = None
                    st.current_file = None
                    st.is_loading = False
                    st.playback_started_at = None

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
                playlist_tracks = await _extract_spotify_metadata(query)

            if isinstance(playlist_tracks, list) and playlist_tracks:
                logger.info("Spotify playlist detected with %d tracks, loading first one.", len(playlist_tracks))
                # Resolve first track immediately
                info, audio_path = await search_and_download(playlist_tracks[0], download=True)
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
                info, audio_path = await search_and_download(query, download=True)
                elapsed = time.perf_counter() - s_dl
                logger.info("Search+download guild=%s took %.2fs", ctx.guild.id, elapsed)

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
        if st.prefetch_task and not st.prefetch_task.done():
            st.prefetch_task.cancel()
            st.prefetch_task = None
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
        st.queue.clear()
        st.current_title = None
        st.current_info = None
        st.current_file = None
        st.is_loading = False
        st.playback_started_at = None
        if st.prefetch_task and not st.prefetch_task.done():
            st.prefetch_task.cancel()
            st.prefetch_task = None
        if ctx.voice_client:
            await ctx.voice_client.disconnect()
        cleanup_all()
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
