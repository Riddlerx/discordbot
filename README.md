# discordbot

A Discord bot featuring music playback and WoW integration.

## Features

- **Music Playback:** Supports YouTube searching and playback.
- **WoW Integration:** Leaderboard updates and guild vault tracking.

## Configuration

### YouTube Authentication
The bot is configured to use an existing YouTube browser profile for authentication, which bypasses the need for manual cookie file management.
- Ensure the profile directory at `/home/ubuntu/.youtube-profile` is accessible.
- Configuration is handled directly within the service environment.

## Dependencies
- Built with `discord.py` (with voice support).
- Uses `yt-dlp` for media extraction.

## Deployment
- Managed as a systemd service (`discordbot.service`).
- Requires a virtual environment (`venv`).
