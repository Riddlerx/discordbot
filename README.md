# discordbot

A Discord bot featuring music playback and WoW integration.

## Features

- **Music Playback:** Supports YouTube searching and playback.
- **WoW Integration:** Leaderboard updates and guild vault tracking.

## Configuration

### YouTube Authentication
The bot authenticates with YouTube using one of the following methods, in order of priority:

Cookies are ignored by default because no-cookie downloads are faster on some cloud VMs.
Set `YTDLP_USE_COOKIES=1` to enable cookie authentication. When enabled, the bot uses:

1. **Browser Profile:** If the environment variable `YTDLP_COOKIES_FROM_BROWSER` is set, the bot uses your browser's saved session to authenticate.
2. **Cookie File:** If no browser profile is configured, it looks for a cookie file at the path specified by `YTDLP_COOKIES` or `YOUTUBE_COOKIES_PATH`.


### Performance Tuning
- `MUSIC_WARMUP_DELAY`: Delay before extractor warmup starts. Default: `2`.
- `MUSIC_WARMUP_YOUTUBE=1`: Warms a real YouTube lookup on startup to reduce first-play latency.
- `MUSIC_PREFETCH_DELAY`: Delay before prefetching the next queued track. Default: `2`.
- `MUSIC_PREFETCH_ENABLED=1`: Pre-download queued tracks in the background. Default is off to avoid blocking foreground playback.
- `MUSIC_FAST_START_STREAMING=1`: Start playback from a stream URL first, then download in the background for recovery. Default is off for reliability.
- `MUSIC_CLEANUP_ON_START=1`: Delete all cached audio on startup. Default is off, so restarts can reuse recent downloads.
- `AUTO_DISCONNECT_EMPTY_DELAY`: Seconds to stay in an empty voice channel before disconnecting. Default: `60`.

## Dependencies
- Built with `discord.py` (with voice support).
- Uses `yt-dlp` for media extraction.

## Deployment
- Managed as a systemd service (`discordbot.service`).
- Requires a virtual environment (`venv`).
