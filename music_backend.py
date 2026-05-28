import asyncio
import glob
import html as html_lib
import logging
import os
import re
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor

import aiohttp
import yt_dlp

TEMP_DIR = os.path.join(tempfile.gettempdir(), "discord_music")
DEFAULT_COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cookies.txt")
DEFAULT_USER_AGENT = os.getenv(
    "USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
)
os.makedirs(TEMP_DIR, exist_ok=True)
logger = logging.getLogger("discordbot.music")

YDL_OPTIONS_FAST = {
    # Prefer opus in a webm container — no re-encoding needed by FFmpeg, lowest CPU
    "format": "bestaudio[ext=webm][acodec=opus]/bestaudio[ext=m4a]/bestaudio/best",
    "noplaylist": True,
    "default_search": "ytsearch1",
    "quiet": True,
    "no_warnings": True,
    "no_color": True,
    "js_runtimes": {"node": {}},
    "remote_components": ["ejs:github"],
    # More retries for high-latency / flaky connections
    "retries": 10,
    "fragment_retries": 15,
    # Download fragments in parallel — critical on high-latency links to hide RTT
    "concurrent_fragment_downloads": 5,
    "nocheckcertificate": True,
    "youtube_include_dash_manifest": True,
    "youtube_include_hls_manifest": True,
    "skip_download": False,
    "writethumbnail": False,
    "writesubtitles": False,
    "writeautomaticsub": False,
    "getcomments": False,
    "cachedir": os.path.join(tempfile.gettempdir(), "yt_dlp_cache"),
    "user_agent": DEFAULT_USER_AGENT,
    "proxy": None,
    "extractor_args": {
        "youtube": {
            # ios is the most reliable client for audio-only extraction
            "player_client": ["ios", "android", "web"],
            "player_skip": ["mweb"],
        }
    },
    "lazy_playlist": True,
    "playlist_items": "1",
    "noprogress": True,
    "no_part": True,
    # 256KB buffer — critical for high-latency (cross-Pacific) connections
    # The default 16KB causes FFmpeg buffer starvation and audible stuttering
    "buffersize": 262144,
    "sleep_interval": 0,
    "max_sleep_interval": 0,
    "outtmpl": os.path.join(TEMP_DIR, "%(id)s.%(ext)s"),
}

# 3 workers so multiple songs can be queued/prefetched without blocking each other
_ydl_executor = ThreadPoolExecutor(max_workers=3, thread_name_prefix="yt-dlp")
# Allow 2 concurrent extractions (e.g. current + prefetch running in parallel)
_extract_semaphore = asyncio.Semaphore(2)
_info_cache: dict[str, tuple[float, dict]] = {}
_info_cache_lock = asyncio.Lock()
_inflight_queries: dict[str, asyncio.Future] = {}
_inflight_queries_lock = asyncio.Lock()
_INFO_CACHE_TTL = 3600


def normalize_query(value: str | None) -> str | None:
    if not value:
        return None
    return value.strip().lower()


def clone_info(info: dict) -> dict:
    return dict(info)


def track_label(info: dict | None) -> str:
    if not info:
        return "unknown"
    title = info.get("title") or "unknown"
    video_id = info.get("id") or "unknown"
    return f"{title} [{video_id}]"


async def extract_spotify_metadata(url: str) -> list[str] | str | None:
    """Extract song title and artist from a Spotify URL."""
    try:
        if "spotify.link" in url:
            async with aiohttp.ClientSession() as session:
                async with session.get(url, allow_redirects=True, timeout=10) as resp:
                    url = str(resp.url)
                    logger.info("Redirected spotify.link to: %s", url)

        embed_url = url
        if "/embed/" not in url:
            embed_url = url.replace("open.spotify.com/track/", "open.spotify.com/embed/track/")
            embed_url = embed_url.replace("open.spotify.com/playlist/", "open.spotify.com/embed/playlist/")
            embed_url = embed_url.replace("open.spotify.com/album/", "open.spotify.com/embed/album/")
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

                t_matches = re.findall(r'\"title\":\"([^\"]+)\".*?\"artists\":\[\{.*?\"name\":\"([^\"]+)\"', html)
                for t_name, a_name in t_matches:
                    t, a = html_lib.unescape(t_name).strip(), html_lib.unescape(a_name).strip()
                    if t.lower() not in ("spotify", "playlist", "album"):
                        tracks.append(f"{t} {a}")

                if not tracks:
                    ts_matches = re.findall(r'\"title\":\"([^\"]+)\",\"subtitle\":\"([^\"]+)\"', html)
                    for t_name, s_name in ts_matches:
                        t, s = html_lib.unescape(t_name).strip(), html_lib.unescape(s_name).strip()
                        if t.lower() not in ("spotify", "playlist", "album"):
                            tracks.append(f"{t} {s}")

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

                snippet = html[:500].replace("\n", " ")
                logger.warning("Spotify extraction failed for %s. Snippet: %s", embed_url, snippet)
                return None
    except Exception as exc:
        logger.warning("Failed to extract Spotify metadata from %s: %s", url, exc)
        return None


def get_yt_dlp_auth_config() -> dict:
    use_cookies = os.getenv("YTDLP_USE_COOKIES", "false").strip().lower() in ("1", "true", "yes", "on")
    if not use_cookies:
        return {}

    cookies_path = os.getenv("YTDLP_COOKIES") or os.getenv("YOUTUBE_COOKIES_PATH")
    cookies_from_browser = os.getenv("YTDLP_COOKIES_FROM_BROWSER")
    auth_options: dict = {}

    if cookies_from_browser:
        auth_options["cookiesfrombrowser"] = (cookies_from_browser,)
    elif cookies_path:
        if os.path.exists(cookies_path):
            if os.access(cookies_path, os.R_OK):
                auth_options["cookiefile"] = cookies_path
            else:
                logger.error("Configured yt-dlp cookie file is NOT READABLE: %s (Check permissions!)", cookies_path)
        else:
            logger.info("Configured yt-dlp cookie file does not exist; continuing without cookies: %s", cookies_path)
    elif os.path.exists(DEFAULT_COOKIE_FILE):
        if os.access(DEFAULT_COOKIE_FILE, os.R_OK):
            auth_options["cookiefile"] = DEFAULT_COOKIE_FILE
        else:
            logger.warning("Default yt-dlp cookie file is NOT READABLE: %s", DEFAULT_COOKIE_FILE)

    return auth_options


def build_ydl_options(base_options: dict) -> dict:
    auth_cfg = get_yt_dlp_auth_config()
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
        options["remote_components"] = [remote_components]

    if auth_cfg.get("cookiefile"):
        logger.info("Using yt-dlp cookies from %s", auth_cfg["cookiefile"])
    elif auth_cfg.get("cookiesfrombrowser"):
        logger.info("Using yt-dlp cookies from browser profile: %s", auth_cfg["cookiesfrombrowser"][0])

    return options


async def read_cached_info(keys: list[str]) -> dict | None:
    now = time.monotonic()
    async with _info_cache_lock:
        for key in keys:
            cached = _info_cache.get(key)
            if cached and now - cached[0] < _INFO_CACHE_TTL:
                return clone_info(cached[1])
    return None


async def store_cached_info(info: dict, *keys: str | None):
    now = time.monotonic()
    cached_info = clone_info(info)
    cache_keys = {normalize_query(key) for key in keys}
    cache_keys.update(
        {
            normalize_query(info.get("id")),
            normalize_query(info.get("webpage_url")),
            normalize_query(info.get("original_url")),
            normalize_query(info.get("title")),
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
    patterns = [
        os.path.join(TEMP_DIR, f"{video_id}.opus"),
        os.path.join(TEMP_DIR, f"{video_id}.m4a"),
        os.path.join(TEMP_DIR, f"{video_id}.webm"),
        os.path.join(TEMP_DIR, f"{video_id}.mp4"),
        os.path.join(TEMP_DIR, f"{video_id}.*"),
    ]
    for pattern in patterns:
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
    return None


def cleanup_all():
    for path in glob.glob(os.path.join(TEMP_DIR, "*")):
        try:
            os.remove(path)
        except Exception:
            pass


def cleanup_stale_audio_files(max_age_seconds: int = 7200):
    now = time.time()
    for path in glob.glob(os.path.join(TEMP_DIR, "*")):
        try:
            if os.path.isfile(path) and now - os.path.getmtime(path) > max_age_seconds:
                os.remove(path)
        except Exception:
            pass


def is_url_query(query: str) -> bool:
    return query.startswith(("http://", "https://"))


def is_playable_video_info(info: dict | None) -> bool:
    if not info or not info.get("id"):
        return False
    if info.get("_type") in {"channel", "playlist", "multi_video"}:
        return False

    video_id = str(info.get("id", ""))
    if info.get("channel_id") == info.get("id") and video_id.startswith("UC"):
        return False
    if video_id.startswith("UC") and not info.get("duration"):
        return False

    # yt-dlp search results can be flat entries with only an 11-char YouTube id.
    if info.get("ie_key") == "Youtube" and len(video_id) == 11:
        return True

    return bool(info.get("url") or info.get("webpage_url") or info.get("original_url"))


def first_playable_video(entries) -> dict | None:
    for entry in entries or []:
        if is_playable_video_info(entry):
            return entry
    return None


async def search_and_download(query: str, *, refresh: bool = False, download: bool = True) -> tuple[dict, str]:
    original_query = query
    is_spotify = "open.spotify.com" in query
    if is_spotify:
        metadata = await extract_spotify_metadata(query)
        if isinstance(metadata, list):
            query = metadata[0] if metadata else query
        elif metadata:
            query = metadata
        else:
            raise Exception("Could not extract song info from this Spotify link. Please try searching for the song name instead.")

        if not query.lower().endswith("audio") and not query.lower().endswith("lyrics"):
            query = f"{query} audio"

    normalized = normalize_query(query)

    if not refresh:
        cache_lookup = [normalized] if normalized else []
        if is_spotify:
            cache_lookup.append(normalize_query(original_query))

        cached = await read_cached_info(cache_lookup)
        if cached and cached.get("id"):
            existing_path = get_audio_path(cached["id"])
            if existing_path:
                return cached, existing_path

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
        return clone_info(result[0]), result[1]

    try:
        loop = asyncio.get_running_loop()

        def do_extract():
            search_query = query
            if not is_url_query(query) and not re.search(r"\b(audio|lyrics|song|official|music|mv|video)\b", query, re.I):
                search_query = f"{query} audio"

            def extract_with_options(extract_query: str, *, should_download: bool, playlist_items: str = "1"):
                opts = build_ydl_options(YDL_OPTIONS_FAST)
                opts["skip_download"] = not should_download
                opts["playlist_items"] = playlist_items
                if is_url_query(extract_query):
                    opts["default_search"] = "auto"
                with yt_dlp.YoutubeDL(opts) as ydl:
                    return ydl.extract_info(extract_query, download=should_download)

            def normalize_extracted_info(info: dict | None) -> dict:
                if info and "entries" in info:
                    info = first_playable_video(info["entries"])
                    if not info:
                        raise Exception("No playable video results found.")
                if not is_playable_video_info(info):
                    raise Exception("Could not extract playable video info.")
                return info

            def result_from_info(info: dict) -> tuple[dict, str]:
                if download:
                    path = get_audio_path(info["id"])
                    if not path:
                        raise FileNotFoundError(f"Download finished but file not found for {info.get('id')}")
                    return info, path
                return info, info["url"]

            def fallback_search() -> tuple[dict, str]:
                search_info = extract_with_options(f"ytsearch5:{search_query}", should_download=False, playlist_items="1:5")
                info = first_playable_video(search_info.get("entries") if search_info else None)
                if not info:
                    raise Exception("No playable video results found.")

                video_query = info.get("webpage_url") or info.get("original_url")
                if not video_query and info.get("ie_key") == "Youtube":
                    video_query = f"https://www.youtube.com/watch?v={info['id']}"
                if not video_query:
                    video_query = info.get("url")
                if not video_query:
                    raise Exception("Search result is missing a playable URL.")

                info = normalize_extracted_info(extract_with_options(video_query, should_download=download))
                return result_from_info(info)

            try:
                try:
                    if is_url_query(query):
                        info = normalize_extracted_info(extract_with_options(query, should_download=download))
                        return result_from_info(info)

                    info = normalize_extracted_info(extract_with_options(search_query, should_download=download))
                    return result_from_info(info)
                except FileNotFoundError as exc:
                    logger.warning("First yt-dlp result did not create audio, retrying broader search query=%r: %s", query, exc)
                    if is_url_query(query):
                        raise
                    return fallback_search()
                except Exception as exc:
                    if is_url_query(query):
                        raise
                    logger.warning("First yt-dlp result was not playable, retrying broader search query=%r: %s", query, exc)
                    return fallback_search()
            except yt_dlp.utils.DownloadError as exc:
                err_msg = str(exc)
                if "[DRM]" in err_msg or "DRM protected" in err_msg:
                    raise Exception("This content is DRM protected and cannot be played directly. Try searching for the song name instead.") from exc
                raise

        async with _extract_semaphore:
            info, result_path = await loop.run_in_executor(_ydl_executor, do_extract)

        if download:
            await store_cached_info(info, query, original_query if is_spotify else None)

        future.set_result((clone_info(info), result_path))
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



async def resolve_track_info(query: str) -> dict:
    """Resolve a user query to playable metadata without downloading audio."""
    info, _ = await search_and_download(query, download=False)
    return info

def parse_cookies_for_ffmpeg(cookiefile: str) -> str:
    if not cookiefile or not os.path.exists(cookiefile):
        return ""

    cookies = []
    try:
        with open(cookiefile, "r") as f:
            for line in f:
                if not line.strip() or line.startswith("#"):
                    continue
                parts = line.split("	")
                if len(parts) >= 7:
                    cookies.append(f"{parts[5]}={parts[6].strip()}")
        return "; ".join(cookies)
    except Exception as exc:
        logger.warning("Failed to parse cookies for FFmpeg: %s", exc)
        return ""


async def warmup_extractors(*, warmup_youtube: bool, delay_seconds: int = 5):
    await asyncio.sleep(delay_seconds)
    loop = asyncio.get_running_loop()
    start = time.perf_counter()
    
    def do_warmup():
        opts = build_ydl_options(YDL_OPTIONS_FAST)
        # Force download of remote components by running a search
        opts['quiet'] = True
        with yt_dlp.YoutubeDL(opts) as ydl:
            # Just warming up the internal extractors and challenge solver
            ydl._ies = ydl._ies 
            if warmup_youtube:
                try:
                    ydl.extract_info("ytsearch1:youtube", download=False)
                except Exception:
                    pass

    await loop.run_in_executor(_ydl_executor, do_warmup)
    elapsed_ms = (time.perf_counter() - start) * 1000
    logger.info("Music extractors warmed and challenge solvers ready in %.0fms", elapsed_ms)
