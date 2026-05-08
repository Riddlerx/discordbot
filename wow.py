import os
import time
import json
import random
import logging
import asyncio
import aiohttp
import discord
import urllib.parse
from discord.ext import commands
from typing import Optional, Dict, List

logger = logging.getLogger("discordbot.wow")

REALMS = {
    "frostmourne": 3725,
    "barthilas": 3721,
    "area52": 3676,
    "illidan": 57
}

STATE_FILE = "bot_state.json"
CACHE_DURATION = 1800  # 30 minutes

class WoW(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.blizzard_client_id = os.getenv("BLIZZARD_CLIENT_ID")
        self.blizzard_client_secret = os.getenv("BLIZZARD_CLIENT_SECRET")
        self.guild_channel_id = int(os.getenv("GUILD_CHANNEL_ID", 0))
        
        self.raider_cache: Dict[str, tuple] = {}
        self.blizzard_token: Optional[str] = None
        self.blizzard_token_expiry: float = 0
        self.commodities_cache: Optional[Dict] = None
        self.commodities_cache_time: float = 0
        
        self.guild_vault_message_id: Optional[int] = None
        self.last_content: Optional[str] = None
        
        self.blizzard_semaphore = asyncio.Semaphore(2)
        self.auto_update_task: Optional[asyncio.Task] = None
        
        self.load_state()

    def cog_unload(self):
        if self.auto_update_task:
            self.auto_update_task.cancel()

    def load_state(self):
        """Load persistent bot state."""
        if os.path.exists(STATE_FILE):
            try:
                with open(STATE_FILE, "r") as f:
                    state = json.load(f)
                    self.guild_vault_message_id = state.get("guild_vault_message_id")
                    self.last_content = state.get("last_content")
            except Exception as e:
                logger.warning("Error loading state: %s", e)

    def save_state(self):
        """Save persistent bot state."""
        state = {
            "guild_vault_message_id": self.guild_vault_message_id,
            "last_content": self.last_content
        }
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)

    async def safe_get(self, session: aiohttp.ClientSession, url: str, params: Optional[Dict] = None, headers: Optional[Dict] = None, retries: int = 3, delay: int = 1) -> Optional[Dict]:
        """Get JSON data safely with retries and error handling."""
        for attempt in range(1, retries + 1):
            try:
                async with session.get(url, params=params, headers=headers, timeout=15) as response:
                    if response.status == 200:
                        return await response.json()
                    elif response.status in [400, 404]:
                        return None
                    elif response.status == 429:
                        wait_time = delay * 5 + random.uniform(1, 5)
                        await asyncio.sleep(wait_time)
                    
                    if response.status != 200:
                         logger.warning(f"Request failed (status {response.status}, attempt {attempt}/{retries}): {url}")
            except Exception as e:
                if attempt == retries:
                    logger.error(f"Request failed (attempt {attempt}/{retries}): {e}")
            
            if attempt < retries:
                await asyncio.sleep(delay)
        return None

    async def get_access_token(self, session: aiohttp.ClientSession) -> Optional[str]:
        """Fetch or refresh Blizzard OAuth token."""
        now = time.time()
        if self.blizzard_token and now < self.blizzard_token_expiry:
            return self.blizzard_token

        url = "https://oauth.battle.net/token"
        auth = aiohttp.BasicAuth(self.blizzard_client_id, self.blizzard_client_secret)
        
        try:
            async with session.post(url, data={"grant_type": "client_credentials"}, auth=auth) as response:
                if response.status == 200:
                    data = await response.json()
                    self.blizzard_token = data.get("access_token")
                    self.blizzard_token_expiry = now + data.get("expires_in", 3600) - 60
                    return self.blizzard_token
        except Exception as e:
            logger.error(f"Failed to get Blizzard access token: {e}")
        return None

    async def get_item_icon(self, session: aiohttp.ClientSession, item_id: int) -> Optional[str]:
        """Fetch item icon URL from Blizzard API."""
        token = await self.get_access_token(session)
        if not token: return None

        url = f"https://us.api.blizzard.com/data/wow/media/item/{item_id}"
        headers = {"Authorization": f"Bearer {token}"}
        params = {"namespace": "static-us", "locale": "en_US"}
        
        data = await self.safe_get(session, url, headers=headers, params=params)
        if data and data.get("assets"):
            for asset in data["assets"]:
                if asset.get("key") == "icon":
                    return asset.get("value")
        return None

    async def get_item_by_id(self, session: aiohttp.ClientSession, item_id: int) -> Optional[Dict]:
        token = await self.get_access_token(session)
        if not token: return None

        url = f"https://us.api.blizzard.com/data/wow/item/{item_id}"
        headers = {"Authorization": f"Bearer {token}"}
        params = {"namespace": "static-us", "locale": "en_US"}
        
        data = await self.safe_get(session, url, headers=headers, params=params)
        if data:
            preview_item = data.get("preview_item", {})
            crafted_quality = data.get("crafted_quality")
            modified_crafting = data.get("modified_crafting") or {}
            modified_crafting_category = modified_crafting.get("category") or {}
            item_class = data.get("item_class") or {}
            tier = None
            if isinstance(crafted_quality, dict):
                tier = crafted_quality.get("tier")
            if tier is None:
                tier = data.get("quality", {}).get("tier")

            return {
                "id": data["id"],
                "name": data["name"],
                "tier": tier,
                "item_level": data.get("level"),
                "item_class_id": item_class.get("id"),
                "modified_crafting_category_id": modified_crafting_category.get("id"),
            }
        return None

    async def enrich_item_results(self, session: aiohttp.ClientSession, items: List[Dict]) -> List[Dict]:
        item_details = await asyncio.gather(*(self.get_item_by_id(session, item["id"]) for item in items))
        enriched_items = []
        seen_keys = set()
        for item, details in zip(items, item_details):
            merged = dict(item)
            if details:
                for key in ("tier", "item_level", "item_class_id", "modified_crafting_category_id"):
                    if details.get(key) is not None:
                        merged[key] = details[key]
            
            # Use id and tier as a unique key for an item version
            unique_key = (merged.get("id"), merged.get("tier"))
            if unique_key not in seen_keys:
                enriched_items.append(merged)
                seen_keys.add(unique_key)
        return enriched_items

    async def get_guild_roster(self, session: aiohttp.ClientSession, realm: str, guild: str) -> List[Dict]:
        token = await self.get_access_token(session)
        if not token: return []

        url = f"https://us.api.blizzard.com/data/wow/guild/{realm}/{guild}/roster"
        headers = {"Authorization": f"Bearer {token}"}
        params = {"namespace": "profile-us", "locale": "en_US"}
        
        data = await self.safe_get(session, url, params=params, headers=headers)
        if not data: return []

        members = []
        for m in data.get("members", []):
            char = m["character"]
            members.append({
                "name": char["name"], 
                "realm": char["realm"]["slug"],
                "class_id": char["playable_class"]["id"]
            })
        return members

    async def get_vault_data(self, session: aiohttp.ClientSession, name: str, realm: str) -> tuple:
        token = await self.get_access_token(session)
        if not token: return [0, 0, 0], ["-", "-", "-"], 0

        headers = {"Authorization": f"Bearer {token}"}
        params = {"namespace": "profile-us", "locale": "en_US"}
        base_url = f"https://us.api.blizzard.com/profile/wow/character/{realm}/{urllib.parse.quote(name.lower())}"
        rio_url = f"https://raider.io/api/v1/characters/profile?region=us&realm={urllib.parse.quote(realm.lower())}&name={urllib.parse.quote(name.lower())}&fields=mythic_plus_weekly_highest_level_runs,mythic_plus_scores_by_season:current"
        
        cache_key = f"{name}-{realm}".lower()
        cached_rio = None
        if cache_key in self.raider_cache:
            ts, data = self.raider_cache[cache_key]
            if time.time() - ts < CACHE_DURATION:
                cached_rio = data

        async with self.blizzard_semaphore:
            tasks = [
                self.safe_get(session, f"{base_url}/encounters/raids", params=params, headers=headers),
                self.safe_get(session, f"{base_url}/mythic-keystone-profile", params=params, headers=headers)
            ]
            if not cached_rio:
                tasks.append(self.safe_get(session, rio_url))
            
            responses = await asyncio.gather(*tasks)
            raid_data = responses[0]
            mplus_data = responses[1]
            rio_data = cached_rio if cached_rio else (responses[2] if len(responses) > 2 else None)
            
            if rio_data and not cached_rio:
                self.raider_cache[cache_key] = (time.time(), rio_data)

        keys = [0, 0, 0]
        score = 0
        if rio_data:
            runs = rio_data.get("mythic_plus_weekly_highest_level_runs", [])
            levels = sorted([r.get("mythic_level", 0) for r in runs if isinstance(r, dict)], reverse=True)
            if levels:
                keys[0] = levels[0]
                keys[1] = levels[3] if len(levels) >= 4 else 0
                keys[2] = levels[7] if len(levels) >= 8 else 0
            seasons = rio_data.get("mythic_plus_scores_by_season", [])
            if seasons and isinstance(seasons, list):
                score = int(seasons[0].get("scores", {}).get("all", 0))
        elif mplus_data:
            curr_period = mplus_data.get("current_period", {})
            runs = curr_period.get("best_runs", [])
            levels = sorted([r.get("keystone_level", 0) for r in runs if isinstance(r, dict)], reverse=True)
            if levels:
                keys[0] = levels[0]
                keys[1] = levels[3] if len(levels) >= 4 else 0
                keys[2] = levels[7] if len(levels) >= 8 else 0

        raid = ["-", "-", "-"]
        if raid_data:
            now = time.time()
            import calendar
            dt_utc = time.gmtime(now)
            days_since_tue = (dt_utc.tm_wday - 1) % 7
            reset_day = time.gmtime(now - days_since_tue * 86400)
            reset_time_str = f"{reset_day.tm_year}-{reset_day.tm_mon:02d}-{reset_day.tm_mday:02d} 15:00:00"
            last_reset_ts = calendar.timegm(time.strptime(reset_time_str, "%Y-%m-%d %H:%M:%S"))
            if now < last_reset_ts:
                last_reset_ts -= 7 * 86400

            CURRENT_EXPANSION_NAMES = ["Midnight", "The Midnight Expansion"]
            CURRENT_EXPANSION_IDS = [501, 17, 506]
            weekly_bosses = {"mythic": set(), "heroic": set(), "normal": set()}

            for exp in raid_data.get("expansions", []):
                expansion_info = exp.get("expansion", {})
                is_midnight = (expansion_info.get("name") in CURRENT_EXPANSION_NAMES) or (expansion_info.get("id") in CURRENT_EXPANSION_IDS)
                if is_midnight:
                    for instance in exp.get("instances", []):
                        for mode in instance.get("modes", []):
                            diff = mode["difficulty"]["type"].lower()
                            if diff in weekly_bosses:
                                for encounter in mode.get("progress", {}).get("encounters", []):
                                    last_kill = encounter.get("last_kill_timestamp", 0) / 1000
                                    if last_kill >= last_reset_ts:
                                        weekly_bosses[diff].add(encounter["encounter"]["name"])

            m, h, n = len(weekly_bosses["mythic"]), len(weekly_bosses["heroic"]), len(weekly_bosses["normal"])
            h_plus, n_plus = h + m, n + h + m
            def get_diff(count):
                if m >= count: return "M"
                if h_plus >= count: return "H"
                if n_plus >= count: return "N"
                return "-"
            raid = [get_diff(2), get_diff(4), get_diff(6)]

        return keys, raid, score

    def format_row(self, rank: int, name: str, keys: List[int], raid: List[str], score: int, name_width: int) -> str:
        display_name = name if len(name) <= name_width else name[:name_width-1] + "…"
        key_str = f"{keys[0]}/{keys[1]}/{keys[2]}"
        raid_str = "/".join(raid)
        return f"| #{rank:<2} {display_name:<{name_width}} | {key_str:^9} | {raid_str:^9} | {score:>6} |"

    async def fetch_char_stats(self, session: aiohttp.ClientSession, char: Dict) -> Optional[tuple]:
        keys, raid, score = await self.get_vault_data(session, char["name"], char["realm"])
        if sum(keys) > 0 or any(r != "-" for r in raid):
            return (char["name"], keys, raid, score, char["class_id"])
        return None

    async def build_guild_vault(self, session: aiohttp.ClientSession) -> str:
        realm, guild_name = "frostmourne", "sinful-garden"
        guild = await self.get_guild_roster(session, realm, guild_name)
        if not guild: return "⚠️ Error fetching guild roster."

        class_emojis = {
            1: "🛡️", 2: "🔨", 3: "🏹", 4: "🗡️", 5: "✨", 
            6: "❄️", 7: "🌀", 8: "🔮", 9: "💀", 10: "🤜", 
            11: "🍃", 12: "🦇", 13: "🐲"
        }

        semaphore = asyncio.Semaphore(2)
        async def sem_fetch(char):
            async with semaphore:
                result = await self.fetch_char_stats(session, char)
                await asyncio.sleep(0.25)
                return result

        tasks = [sem_fetch(char) for char in guild]
        results = await asyncio.gather(*tasks)
        rows = [r for r in results if r is not None]
        rows.sort(key=lambda x: (sum(x[1]), x[3]), reverse=True)

        # Calculate max name length for alignment (excluding the emoji)
        max_name_len = max((len(name) for name, _, _, _, _ in rows[:30]), default=10)
        max_name_len = min(max_name_len, 20)

        table = ["🔥 WEEKLY VAULT LEADERBOARD 🔥"]
        # Adjusted header for 2-space emoji width
        header = f"| {'Name':<{max_name_len + 6}} | Key Vault | Raid Vault | Score |"
        table.append(header)
        table.append(f"|{'-'*(max_name_len+8)}+-----------+-----------+--------|")

        for i, (name, keys, raid, score, class_id) in enumerate(rows[:30], start=1):
            emoji = class_emojis.get(class_id, "👤")
            # Alignment fix: Use fixed padding for the emoji and separate padding for the name
            key_str = f"{keys[0]}/{keys[1]}/{keys[2]}"
            raid_str = "/".join(raid)
            # We assume the emoji takes 2 slots in the monospaced font
            row = f"| #{i:<2} {emoji} {name:<{max_name_len}} | {key_str:^9} | {raid_str:^9} | {score:>6} |"
            table.append(row)

        table.append(f"|{'-'*(max_name_len+8)}+-----------+-----------+--------|")
        token_price = await self.get_wow_token_price(session)
        if token_price > 0: table.append(f"💰 WoW Token Price: {token_price:,.0f}g")

        unix_now = int(time.time())
        return "```" + "\n".join(table) + "```" + f"Last Updated: <t:{unix_now}:R>"

    async def get_wow_token_price(self, session: aiohttp.ClientSession) -> float:
        token = await self.get_access_token(session)
        if not token: return 0
        url = "https://us.api.blizzard.com/data/wow/token/index"
        headers = {"Authorization": f"Bearer {token}"}
        params = {"namespace": "dynamic-us", "locale": "en_US"}
        data = await self.safe_get(session, url, headers=headers, params=params)
        return data.get("price", 0) / 10000 if data else 0

    async def get_commodities_cached(self, session: aiohttp.ClientSession) -> Dict:
        now = time.time()
        if self.commodities_cache and now - self.commodities_cache_time < 1800:
            return self.commodities_cache
        token = await self.get_access_token(session)
        if not token: return {}
        url = "https://us.api.blizzard.com/data/wow/auctions/commodities"
        headers = {"Authorization": f"Bearer {token}"}
        params = {"namespace": "dynamic-us", "locale": "en_US"}
        data = await self.safe_get(session, url, headers=headers, params=params)
        if data:
            self.commodities_cache, self.commodities_cache_time = data, now
            return data
        return self.commodities_cache or {}

    async def search_items(self, session: aiohttp.ClientSession, item_name: str) -> List[Dict]:
        token = await self.get_access_token(session)
        if not token: return []
        url = "https://us.api.blizzard.com/data/wow/search/item"
        headers = {"Authorization": f"Bearer {token}"}

        # Prepare variations to be more forgiving
        clean_name = item_name.strip().rstrip(".,").lower()
        variations = {clean_name}

        # Add variation without "the " prefix
        if clean_name.startswith("the "):
            variations.add(clean_name[4:])
        # Add variation with "the " prefix
        variations.add(f"the {clean_name}")

        # Add variations handling hyphens
        for v in list(variations):
            if "-" in v:
                variations.add(v.replace("-", " "))
                variations.add(v.replace("-", ""))

        all_matches = []
        for name_variant in variations:
            # We use fuzzy search directly now to be more forgiving
            params = {
                "namespace": "static-us", 
                "locale": "en_US", 
                "name.en_US": f"*{name_variant}*",
                "orderby": "id:desc"
            }
            data = await self.safe_get(session, url, headers=headers, params=params)

            if data and data.get("results"):
                for r in data.get("results"):
                    item_data = r["data"]
                    name = item_data.get("name", {}).get("en_US", "")
                    # Calculate a simple similarity score or just collect matches
                    if name.lower() not in [m["name"].lower() for m in all_matches]:
                        tier = item_data.get("quality", {}).get("tier")
                        all_matches.append({"id": item_data["id"], "name": name, "tier": tier})

        # Return top 5 matches, sorted by name length (closest to input) then quality
        if all_matches:
            all_matches.sort(key=lambda x: (abs(len(x["name"]) - len(item_name)), x.get("tier") or 0))
            return all_matches[:5]
        return []
    def get_class_emoji(self, class_id: int) -> str:
        """Try to find a custom emoji in the bot's cache (wowwarrior, wowpaladin, etc.)."""
        classes = {
            1: "warrior", 2: "paladin", 3: "hunter", 4: "rogue", 5: "priest", 
            6: "deathknight", 7: "shaman", 8: "mage", 9: "warlock", 10: "monk", 
            11: "druid", 12: "demonhunter", 13: "evoker"
        }
        name = classes.get(class_id, "unknown")
        # Search for 'wowwarrior', 'wowpaladin', etc.
        target = f"wow{name}"
        for emoji in self.bot.emojis:
            if emoji.name.lower() == target:
                return str(emoji)
        
        # Fallback to standard emojis if custom not found
        fallbacks = {
            1: "🛡️", 2: "🔨", 3: "🏹", 4: "🗡️", 5: "✨", 
            6: "❄️", 7: "🌀", 8: "🔮", 9: "💀", 10: "🤜", 
            11: "🍃", 12: "🦇", 13: "🐲"
        }
        return fallbacks.get(class_id, "👤")

    async def build_guild_vault_text(self, session: aiohttp.ClientSession) -> str:
        realm, guild_name = "frostmourne", "sinful-garden"
        guild = await self.get_guild_roster(session, realm, guild_name)
        if not guild: return "⚠️ Error fetching guild roster."

        semaphore = asyncio.Semaphore(2)
        async def sem_fetch(char):
            async with semaphore:
                result = await self.fetch_char_stats(session, char)
                await asyncio.sleep(0.25)
                return result

        tasks = [sem_fetch(char) for char in guild]
        results = await asyncio.gather(*tasks)
        rows = [r for r in results if r is not None]
        rows.sort(key=lambda x: (sum(x[1]), x[3]), reverse=True)

        max_name_len = max((len(name) for name, _, _, _, _ in rows[:25]), default=10)
        max_name_len = min(max_name_len, 20)

        # Header
        lines = ["🔥 **WEEKLY VAULT LEADERBOARD** 🔥"]
        header = f"| {'Name':<{max_name_len + 3}} | Key Vault | Raid Vault | Score |"
        lines.append(f"👤 `{header}`")
        separator = f"|{'-'*(max_name_len+5)}+-----------+-----------+--------|"
        lines.append(f"⠀ `{separator}`")

        for i, (name, keys, raid, score, class_id) in enumerate(rows[:25], start=1):
            emoji = self.get_class_emoji(class_id)
            key_str = f"{keys[0]}/{keys[1]}/{keys[2]}"
            raid_str = "/".join(raid)
            # Table row without the emoji (so it stays aligned)
            row = f"| #{i:<2} {name:<{max_name_len}} | {key_str:^9} | {raid_str:^9} | {score:>5} |"
            lines.append(f"{emoji} `{row}`")

        lines.append(f"⠀ `{separator}`")
        
        token_price = await self.get_wow_token_price(session)
        if token_price > 0:
            lines.append(f"💰 **WoW Token Price:** {token_price:,.0f}g")
        
        unix_now = int(time.time())
        lines.append(f"Last Updated: <t:{unix_now}:R>")
        return "\n".join(lines)

    @commands.Cog.listener()
    async def on_ready(self):
        if self.auto_update_task is None or self.auto_update_task.done():
            self.auto_update_task = self.bot.loop.create_task(self.auto_update())

    async def auto_update(self):
        await self.bot.wait_until_ready()
        async with aiohttp.ClientSession() as session:
            while not self.bot.is_closed():
                if self.guild_vault_message_id is None:
                    await asyncio.sleep(60)
                    continue
                channel = self.bot.get_channel(self.guild_channel_id)
                if not channel:
                    await asyncio.sleep(60)
                    continue
                try:
                    try:
                        message = await channel.fetch_message(self.guild_vault_message_id)
                    except discord.NotFound:
                        self.guild_vault_message_id = None
                        continue
                    
                    new_content = await self.build_guild_vault_text(session)
                    if new_content != self.last_content:
                        await message.edit(content=new_content, embed=None)
                        self.last_content = new_content
                        self.save_state()
                        logger.info("Updated leaderboard text message_id=%s", self.guild_vault_message_id)
                except Exception as e:
                    logger.exception("Leaderboard update error: %s", e)
                await asyncio.sleep(1800)

    @commands.command()
    async def guildvault(self, ctx):
        async with ctx.typing():
            try:
                async with aiohttp.ClientSession() as session:
                    content = await self.build_guild_vault_text(session)
                    message = await ctx.send(content)
                    self.guild_vault_message_id = message.id
                    self.last_content = content
                    self.save_state()
            except Exception as e:
                logger.error(f"Error in guildvault command: {e}")
                await ctx.send(f"⚠️ An error occurred: {e}")

    async def get_character_profile(self, session: aiohttp.ClientSession, name: str, realm: str) -> Optional[Dict]:
        token = await self.get_access_token(session)
        if not token: return None

        url = f"https://us.api.blizzard.com/profile/wow/character/{realm}/{urllib.parse.quote(name.lower())}"
        headers = {"Authorization": f"Bearer {token}"}
        params = {"namespace": "profile-us", "locale": "en_US"}
        
        return await self.safe_get(session, url, headers=headers, params=params)

    async def get_character_media(self, session: aiohttp.ClientSession, name: str, realm: str) -> Optional[str]:
        token = await self.get_access_token(session)
        if not token: return None

        url = f"https://us.api.blizzard.com/profile/wow/character/{realm}/{urllib.parse.quote(name.lower())}/character-media"
        headers = {"Authorization": f"Bearer {token}"}
        params = {"namespace": "profile-us", "locale": "en_US"}
        
        data = await self.safe_get(session, url, headers=headers, params=params)
        if data and data.get("assets"):
            for asset in data["assets"]:
                if asset.get("key") == "main-raw":
                    return asset.get("value")
                if asset.get("key") == "avatar":
                    avatar = asset.get("value")
            # Prefer avatar for small embed if main-raw not found
            return avatar if 'avatar' in locals() else None
        return None

    @commands.command(aliases=['char', 'whois'])
    async def lookup(self, ctx, *, query: str):
        """Lookup a WoW character: name-realm or name."""
        async with ctx.typing():
            name, realm = query, "frostmourne"
            if ":" in query:
                parts = query.rsplit(":", 1)
                name, realm = parts[0].strip(), parts[1].strip()
            elif "-" in query:
                parts = query.rsplit("-", 1)
                name, realm = parts[0].strip(), parts[1].strip()

            # Slugify realm: "Area 52" -> "area-52"
            realm_slug = realm.lower().replace(" ", "-").replace("'", "")

            async with aiohttp.ClientSession() as session:
                profile = await self.get_character_profile(session, name, realm_slug)
                if not profile:
                    return await ctx.send(f"❌ Character **{name}** on **{realm_slug}** not found.")

                keys, raid, score = await self.get_vault_data(session, name, realm_slug)
                media_url = await self.get_character_media(session, name, realm_slug)

                char_class = profile.get("character_class", {}).get("name", "Unknown")
                race = profile.get("race", {}).get("name", "Unknown")
                level = profile.get("level", 0)
                ilvl = profile.get("equipped_item_level", 0)
                guild = profile.get("guild", {}).get("name", "No Guild")
                faction = profile.get("faction", {}).get("name", "Neutral")

                color = discord.Color.blue()
                if faction == "Horde": color = discord.Color.red()
                elif faction == "Alliance": color = discord.Color.blue()

                embed = discord.Embed(
                    title=f"{profile['name']} - {profile['realm']['name']}",
                    description=f"{level} {race} {char_class} | <{guild}>",
                    color=color,
                    url=f"https://raider.io/characters/us/{realm_slug}/{urllib.parse.quote(name)}"
                )

                if media_url:
                    embed.set_thumbnail(url=media_url)

                embed.add_field(name="Stats", value=f"**ilvl:** {ilvl}\n**M+ Score:** {score}", inline=True)
                embed.add_field(name="Weekly Vault", value=f"**Keys:** {keys[0]}/{keys[1]}/{keys[2]}\n**Raid:** {'/'.join(raid)}", inline=True)
                
                await ctx.send(embed=embed)

    @commands.command()
    async def price(self, ctx, *, search: str):
        async with ctx.typing():
            item_name, realm = search, None
            if ":" in search:
                parts = search.rsplit(":", 1)
                potential_realm = parts[1].strip().lower().replace(" ", "").replace("-", "").replace("'", "")
                if potential_realm in REALMS:
                    item_name, realm = parts[0].strip(), parts[1].strip()
            if not realm: realm = "frostmourne"

            async with aiohttp.ClientSession() as session:
                item_results = await self.search_items(session, item_name)
                if not item_results:
                    return await ctx.send(f"❌ Item **{item_name}** not found.")

                item_results = await self.enrich_item_results(session, item_results)

                if len(item_results) > 1:
                    embed = discord.Embed(title="💰 Multiple matches found", color=discord.Color.gold())
                    description = "\n".join([f"{i+1}. {r['name']} (Tier {r.get('tier', 0)})" for i, r in enumerate(item_results[:10])])
                    embed.description = f"Please be more specific:\n{description}"
                    return await ctx.send(embed=embed)

                # Proceed with single result...
                item = item_results[0]
                display_name = item["name"]
                if realm:
                    realm_key = realm.lower().replace(" ", "").replace("-", "").replace("'", "")
                    realm_id = REALMS.get(realm_key)
                    if realm_id:
                        token = await self.get_access_token(session)
                        url = f"https://us.api.blizzard.com/data/wow/connected-realm/{realm_id}/auctions"
                        headers = {"Authorization": f"Bearer {token}"}
                        params = {"namespace": "dynamic-us", "locale": "en_US"}
                        realm_data = await self.safe_get(session, url, headers=headers, params=params)

                for item in item_results:
                    item_id = item["id"]
                    current_item_name = item["name"]
                    prices = []
                    for auction in commodities.get("auctions", []):
                        if auction["item"]["id"] == item_id: prices.append(auction["unit_price"])
                    if realm_data:
                        for auction in realm_data.get("auctions", []):
                            if auction["item"]["id"] == item_id:
                                prices.append(auction.get("unit_price") or auction.get("buyout"))

                    if prices:
                        prices_gold = [p / 10000 for p in prices]
                        lowest, avg = min(prices_gold), sum(prices_gold) / len(prices_gold)
                        
                        tier = item.get("tier")
                        item_level = item.get("item_level")
                        
                        label = current_item_name
                        if tier: label += f" (Tier {tier})"
                        if item_level: label += f" (ilvl {item_level})"
                        
                        embed.add_field(
                            name=label,
                            value=f"Lowest: {lowest:,.2f}g\nAvg: {avg:,.2f}g\nListings: {len(prices)}",
                            inline=False
                        )
                        
                        same_name_results = [r for r in item_results if r["name"] == current_item_name]
                        same_name_count = len(same_name_results)
                        
                        if tier:
                            label += f" ({'⭐' * tier})"
                        elif same_name_count > 1:
                            # Quality/Variant detection
                            distinct_item_levels = len({r.get("item_level") for r in same_name_results if r.get("item_level") is not None}) > 1
                            current_category_id = item.get("modified_crafting_category_id")
                            inferred_reagent_quality = (
                                same_name_count > 1
                                and current_category_id is not None
                                and all(r.get("modified_crafting_category_id") == current_category_id for r in same_name_results)
                                and all(r.get("item_class_id") == 7 for r in same_name_results)
                            )

                            if inferred_reagent_quality:
                                ranked = sorted(same_name_results, key=lambda x: x["id"])
                                idx = [r["id"] for r in ranked].index(item_id) + 1
                                label += f" (Q{idx})"
                            elif distinct_item_levels:
                                ranked = sorted(same_name_results, key=lambda x: (x.get("item_level") or 0, x["id"]))
                                idx = [r["id"] for r in ranked].index(item_id) + 1
                                label += f" (Q{idx}, ilvl {item_level})" if item_level else f" (Q{idx})"
                            else:
                                ranked = sorted(same_name_results, key=lambda x: x["id"])
                                idx = [r["id"] for r in ranked].index(item_id) + 1
                                label += f" (Variant {idx})"

                        val = f"**Lowest:** {lowest:,.2f}g\n**Avg:** {avg:,.2f}g\n**Listings:** {len(prices):,}"
                        embed.add_field(name=label, value=val, inline=True)

                if not embed.fields:
                    return await ctx.send(f"❌ No auctions found for **{item_name}**.")
                await ctx.send(embed=embed)

async def setup(bot):
    await bot.add_cog(WoW(bot))
