import os
import time
import asyncio
import sqlite3
from typing import List, Optional

import discord
from discord.ext import commands, tasks

DB_PATH = os.path.join(os.path.dirname(__file__), "boosters.db")
DEFAULT_SCAN_INTERVAL = int(os.getenv("BOOST_SCAN_INTERVAL", "300"))  # seconds
KEYWORDS = [
    "boost",
    "boosting",
    "carry",
    "selling carries",
    "buy carry",
    "m+ carry",
    "mythic+ carry",
    "selling keystone",
    "sell carry",
    "wts carry",
    "wts boost",
    "wts m+",
    "wts",
    "wtt",
    "selling boost",
]


class BoosterTracker(commands.Cog):
    """Track suspected booster accounts by scanning recent messages for keywords.

    - Scans channels listed in `BOOST_TRACK_CHANNELS` (comma-separated names).
    - Stores sightings in a local SQLite DB at `boosters.db`.
    - Provides `!boosters` commands for moderators to inspect results.
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db_path = DB_PATH
        self.scan_interval = DEFAULT_SCAN_INTERVAL
        self.channels_to_scan = [c.strip() for c in os.getenv("BOOST_TRACK_CHANNELS", "").split(",") if c.strip()]
        self._scan_task = None

    async def cog_load(self) -> None:
        await self._init_db()
        self._scan_task = self._start_scanner()

    async def cog_unload(self) -> None:
        if self._scan_task:
            self._scan_task.cancel()

    async def _init_db(self) -> None:
        def init():
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS boosters (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    count INTEGER DEFAULT 0,
                    first_seen REAL,
                    last_seen REAL,
                    sample_message TEXT,
                    sample_jump_url TEXT
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS sightings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id INTEGER UNIQUE,
                    user_id INTEGER,
                    username TEXT,
                    channel_id INTEGER,
                    jump_url TEXT,
                    content TEXT,
                    ts REAL
                )
                """
            )
            conn.commit()
            conn.close()

        await asyncio.to_thread(init)

    def _start_scanner(self):
        # Use tasks.loop to run scanning in background
        @tasks.loop(seconds=self.scan_interval)
        async def scanner():
            try:
                await self.scan_channels()
            except Exception:
                pass

        scanner.add_exception_type(Exception)
        scanner.start()
        return scanner

    async def scan_channels(self) -> None:
        # Choose channels: either configured names or all text channels
        guilds = list(self.bot.guilds)
        if not guilds:
            return

        for guild in guilds:
            channels: List[discord.TextChannel]
            if self.channels_to_scan:
                channels = [c for c in guild.text_channels if c.name in self.channels_to_scan]
            else:
                channels = guild.text_channels

            for ch in channels:
                await self._scan_channel(ch)

    async def _scan_channel(self, channel: discord.TextChannel) -> None:
        # Scan recent messages and record sightings
        try:
            async for msg in channel.history(limit=200):
                if msg.author.bot:
                    continue
                content = (msg.content or "").lower()
                if any(kw in content for kw in KEYWORDS):
                    await self._record_sighting(msg)
        except discord.Forbidden:
            return
        except Exception:
            return

    async def _record_sighting(self, msg: discord.Message) -> None:
        ts = msg.created_at.timestamp()
        jump_url = getattr(msg, "jump_url", "")

        def write():
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            # insert sighting if message_id not seen
            try:
                cur.execute(
                    "INSERT INTO sightings (message_id, user_id, username, channel_id, jump_url, content, ts) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (msg.id, msg.author.id, str(msg.author), channel_id := msg.channel.id, jump_url, msg.content[:2000], ts),
                )
            except sqlite3.IntegrityError:
                # already seen
                conn.close()
                return

            # update boosters table
            cur.execute("SELECT count, first_seen FROM boosters WHERE user_id = ?", (msg.author.id,))
            row = cur.fetchone()
            now = ts
            if row:
                new_count = row[0] + 1
                cur.execute(
                    "UPDATE boosters SET count = ?, last_seen = ?, sample_message = ?, sample_jump_url = ?, username = ? WHERE user_id = ?",
                    (new_count, now, msg.content[:2000], jump_url, str(msg.author), msg.author.id),
                )
            else:
                cur.execute(
                    "INSERT INTO boosters (user_id, username, count, first_seen, last_seen, sample_message, sample_jump_url) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (msg.author.id, str(msg.author), 1, now, now, msg.content[:2000], jump_url),
                )

            conn.commit()
            conn.close()

        await asyncio.to_thread(write)

    @commands.group(name="boosters", invoke_without_command=True)
    async def boosters(self, ctx, min_count: int = 1):
        """List suspected boosters. Optionally provide a minimum sighting count."""
        rows = await asyncio.to_thread(self._fetch_boosters, min_count)
        if not rows:
            return await ctx.send("No suspected boosters found.")

        lines = []
        for user_id, username, count, first_seen, last_seen, sample, jump in rows:
            when = time.strftime("%Y-%m-%d", time.localtime(last_seen)) if last_seen else "?"
            sample_display = (sample[:150] + "...") if sample and len(sample) > 150 else (sample or "")
            lines.append(f"<{jump}> {username} — {count} sightings — last: {when}\n{sample_display}")
        msg = "\n\n".join(lines)
        for chunk in self._chunk_text(msg):
            await ctx.send(chunk)

    def _fetch_boosters(self, min_count: int):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id, username, count, first_seen, last_seen, sample_message, sample_jump_url FROM boosters WHERE count >= ? ORDER BY count DESC",
            (min_count,)
        )
        rows = cur.fetchall()
        conn.close()
        return rows

    @boosters.command(name="info")
    async def boosters_info(self, ctx, member: discord.Member):
        """Show detailed info for a suspected booster (mention a member)."""
        row = await asyncio.to_thread(self._fetch_booster, member.id)
        if not row:
            return await ctx.send(f"No data for {member.mention}.")
        user_id, username, count, first_seen, last_seen, sample, jump = row
        first = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(first_seen)) if first_seen else "?"
        last = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(last_seen)) if last_seen else "?"
        embed = discord.Embed(title=f"Booster info — {username}")
        embed.add_field(name="Sightings", value=str(count), inline=True)
        embed.add_field(name="First seen", value=first, inline=True)
        embed.add_field(name="Last seen", value=last, inline=True)
        if sample:
            embed.add_field(name="Sample message", value=sample[:1024], inline=False)
        if jump:
            embed.add_field(name="Sample link", value=jump, inline=False)
        await ctx.send(embed=embed)

    def _fetch_booster(self, user_id: int):
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id, username, count, first_seen, last_seen, sample_message, sample_jump_url FROM boosters WHERE user_id = ?",
            (user_id,)
        )
        row = cur.fetchone()
        conn.close()
        return row

    @boosters.command(name="clear")
    @commands.has_permissions(manage_messages=True)
    async def boosters_clear(self, ctx, member: discord.Member):
        """Clear stored booster data for a member (moderator only)."""
        await asyncio.to_thread(self._delete_booster, member.id)
        await ctx.send(f"Cleared booster data for {member.mention}.")

    def _delete_booster(self, user_id: int) -> None:
        conn = sqlite3.connect(self.db_path)
        cur = conn.cursor()
        cur.execute("DELETE FROM boosters WHERE user_id = ?", (user_id,))
        cur.execute("DELETE FROM sightings WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()

    def _chunk_text(self, text: str, limit: int = 1900):
        chunks = []
        while len(text) > limit:
            idx = text.rfind("\n", 0, limit)
            if idx == -1:
                idx = limit
            chunks.append(text[:idx])
            text = text[idx:]
        if text:
            chunks.append(text)
        return chunks


def setup(bot: commands.Bot):
    bot.add_cog(BoosterTracker(bot))
