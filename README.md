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
3. **Default:** If neither of the above is configured, it defaults to using `/home/ubuntu/discordbot/cookies.txt`.

## Dependencies
- Built with `discord.py` (with voice support).
- Uses `yt-dlp` for media extraction.

## Deployment
- Managed as a systemd service (`discordbot.service`).
- Requires a virtual environment (`venv`).
