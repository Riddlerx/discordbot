# discordbot

A Discord bot featuring music playback and WoW integration.

## Features

- **Music Playback:** Supports YouTube searching and playback.
- **WoW Integration:** Leaderboard updates and guild vault tracking.

## Configuration

### YouTube Authentication
The bot authenticates with YouTube using one of the following methods, in order of priority:

1. **Browser Profile:** If the environment variable `YTDLP_COOKIES_FROM_BROWSER` is set, the bot uses your browser's saved session to authenticate.
2. **Cookie File:** If no browser profile is configured, it looks for a cookie file at the path specified by `YTDLP_COOKIES` or `YOUTUBE_COOKIES_PATH`.
3. **Repo Default:** If neither of the above is configured, it uses `cookies.txt` in the repo root, but only if that file exists.


### Performance Tuning
- `MUSIC_WARMUP_DELAY`: Delay before extractor warmup starts. Default: `2`.
- `MUSIC_WARMUP_YOUTUBE=1`: Warms a real YouTube lookup on startup to reduce first-play latency.
- `MUSIC_PREFETCH_DELAY`: Delay before prefetching the next queued track. Default: `2`.

## Dependencies
- Built with `discord.py` (with voice support).
- Uses `yt-dlp` for media extraction.

## Deployment
- Managed as a systemd service (`discordbot.service`).
- Requires a virtual environment (`venv`).
