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

class ItemSelectionView(discord.ui.View):
    def __init__(self, items, callback):
        super().__init__(timeout=60)
        self.callback_func = callback
        for i, item in enumerate(items[:5]):
            button = discord.ui.Button(label=f"{i+1}. {item['name'][:15]}", custom_id=str(i))
            button.callback = self.button_callback
            self.add_item(button)

    async def button_callback(self, interaction: discord.Interaction):
        await self.callback_func(interaction, int(interaction.data['custom_id']))


TIER_LABELS = {1: "Q1 — Base", 2: "Q2 — Crafted", 3: "Q3 — Max"}


class TierSelectionView(discord.ui.View):
    def __init__(self, variants: list, callback):
        super().__init__(timeout=60)
        self.callback_func = callback
        for i, item in enumerate(variants[:5]):
            tier = item.get("tier")
            label = TIER_LABELS.get(tier, f"Tier {tier}") if isinstance(tier, int) else "Standard"
            button = discord.ui.Button(label=label, custom_id=str(i))
            button.callback = self.button_callback
            self.add_item(button)

    async def button_callback(self, interaction: discord.Interaction):
        await self.callback_func(interaction, int(interaction.data['custom_id']))


class WoW(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.blizzard_client_id = os.getenv("BLIZZARD_CLIENT_ID")
        self.blizzard_client_secret = os.getenv("BLIZZARD_CLIENT_SECRET")
        self.guild_channel_id = int(os.getenv("GUILD_CHANNEL_ID", 0))
        self.guild_name = os.getenv("GUILD_NAME", "sinful-garden")
        self.guild_realm = os.getenv("GUILD_REALM", "frostmourne")

        self.raider_cache: Dict[str, tuple] = {}
        self.blizzard_token: Optional[str] = None
        self.blizzard_token_expiry: float = 0
        self.commodities_cache: Optional[Dict] = None
        self.commodities_cache_time: float = 0

        self.guild_vault_message_id: Optional[int] = None
        self.last_content: Optional[str] = None

        self.blizzard_semaphore = asyncio.Semaphore(10)
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
        timeout = aiohttp.ClientTimeout(total=15)
        for attempt in range(1, retries + 1):
            try:
                async with self.blizzard_semaphore:
                    async with session.get(url, params=params, headers=headers, timeout=timeout) as response:
                        if response.status == 200:
                            return await response.json()
                        elif response.status in [400, 404]:
                            return None
                        elif response.status == 429:
                            wait_time = delay * 5 + random.uniform(1, 5)
                            logger.warning("Rate limited, waiting %.1fs", wait_time)
                            await asyncio.sleep(wait_time)
            except Exception as e:
                if attempt == retries:
                    logger.error(f"Request failed after {retries} attempts: {e}")
            if attempt < retries:
                await asyncio.sleep(delay)
        return None

    async def get_access_token(self, session: aiohttp.ClientSession) -> Optional[str]:
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
            logger.error(f"Failed to get token: {e}")
        return None

    async def get_item_icon(self, session: aiohttp.ClientSession, item_id: int) -> Optional[str]:
        token = await self.get_access_token(session)
        if not token:
            return None
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
        if not token:
            return None
        url = f"https://us.api.blizzard.com/data/wow/item/{item_id}"
        headers = {"Authorization": f"Bearer {token}"}
        params = {"namespace": "static-us", "locale": "en_US"}
        data = await self.safe_get(session, url, headers=headers, params=params)
        if data:
            preview_item = data.get("preview_item", {})
            crafted_quality = data.get("crafted_quality") or preview_item.get("crafted_quality")
            
            tier = None
            if isinstance(crafted_quality, dict):
                tier = crafted_quality.get("tier")
            
            if tier is None:
                quality = data.get("quality", {})
                if isinstance(quality, dict) and "tier" in quality:
                    tier = quality.get("tier")
            
            if tier is None:
                preview_quality = preview_item.get("quality", {})
                if isinstance(preview_quality, dict) and "tier" in preview_quality:
                    tier = preview_quality.get("tier")

            return {
                "id": data["id"],
                "name": data["name"],
                "tier": tier,
                "item_level": data.get("level") or preview_item.get("level", {}).get("value", 0),
                "item_class_id": data.get("item_class", {}).get("id"),
                "modified_crafting_category_id": data.get("modified_crafting", {}).get("category", {}).get("id") or preview_item.get("modified_crafting", {}).get("category", {}).get("id"),
            }
        return None

    async def enrich_item_results(self, session: aiohttp.ClientSession, items: List[Dict]) -> List[Dict]:
        item_details = await asyncio.gather(*(self.get_item_by_id(session, item["id"]) for item in items))
        enriched_items, seen_keys = [], set()
        for item, details in zip(items, item_details):
            merged = dict(item)
            if details:
                for key in ("tier", "item_level", "item_class_id", "modified_crafting_category_id"):
                    if details.get(key) is not None:
                        merged[key] = details[key]
            if merged["id"] not in seen_keys:
                enriched_items.append(merged)
                seen_keys.add(merged["id"])
        return enriched_items

    async def search_items(self, session: aiohttp.ClientSession, item_name: str) -> List[Dict]:
        token = await self.get_access_token(session)
        if not token:
            return []
        url = "https://us.api.blizzard.com/data/wow/search/item"
        headers = {"Authorization": f"Bearer {token}"}
        clean_name = item_name.strip().rstrip(".,").lower()

        STOP_WORDS = {"the", "of", "a", "an", "and", "in", "for"}
        words = clean_name.split()
        keywords = [w for w in words if w not in STOP_WORDS] or words
        search_terms = {clean_name} | set(keywords)

        all_matches: Dict[int, Dict] = {}
        for term in search_terms:
            params = {
                "namespace": "static-us",
                "locale": "en_US",
                "name.en_US": f"*{term}*",
                "orderby": "id:desc",
                "_pageSize": 20,
            }
            data = await self.safe_get(session, url, headers=headers, params=params)
            if data and data.get("results"):
                for r in data["results"]:
                    item_data = r["data"]
                    item_id = item_data["id"]
                    if item_id not in all_matches:
                        name = item_data.get("name", {}).get("en_US", "")
                        all_matches[item_id] = {"id": item_id, "name": name}

        if not all_matches:
            return []

        def keyword_in_name(kw: str, name_lower: str) -> bool:
            if kw in name_lower: return True
            if kw + "s" in name_lower: return True
            if kw + "es" in name_lower: return True
            if kw.endswith("s") and kw[:-1] in name_lower: return True
            return False

        def match_score(item: Dict) -> int:
            name_lower = item["name"].lower()
            return sum(1 for kw in keywords if keyword_in_name(kw, name_lower))

        max_score = len(keywords)
        best_matches = [i for i in all_matches.values() if match_score(i) == max_score]
        if not best_matches:
            best_matches = sorted(all_matches.values(), key=match_score, reverse=True)[:15]

        enriched_matches = await self.enrich_item_results(session, best_matches)
        enriched_matches.sort(key=lambda x: (len(x["name"]), -x["id"]))
        return enriched_matches[:25]

    @commands.command()
    async def price(self, ctx, *, search: str):
        async with ctx.typing():
            item_name, realm = search, self.guild_realm
            if ":" in search:
                parts = search.rsplit(":", 1)
                item_name, realm = parts[0].strip(), parts[1].strip()

            async with aiohttp.ClientSession() as session:
                item_results = await self.search_items(session, item_name)
                if not item_results:
                    return await ctx.send(f"❌ Item **{item_name}** not found.")

            name_groups: Dict[str, list] = {}
            for item in item_results:
                name_groups.setdefault(item["name"], []).append(item)
            
            unique_names = sorted(name_groups.keys(), key=lambda n: abs(len(n) - len(item_name)))

            async def on_name_selected(interaction: discord.Interaction, index: int):
                selected_name = unique_names[index]
                variants = name_groups[selected_name]
                async with aiohttp.ClientSession() as new_session:
                    await self.display_item_price(interaction, variants, realm, new_session)

            if len(unique_names) > 1:
                exact_match = next((n for n in unique_names if n.lower() == item_name.lower()), None)
                if exact_match:
                    async with aiohttp.ClientSession() as new_session:
                        return await self.display_item_price(ctx, name_groups[exact_match], realm, new_session)

                embed = discord.Embed(
                    title="💰 Multiple matches found",
                    description="Select an item below:",
                    color=discord.Color.gold()
                )
                display_items = [{"name": n} for n in unique_names[:5]]
                return await ctx.send(embed=embed, view=ItemSelectionView(display_items, on_name_selected))

            async with aiohttp.ClientSession() as new_session:
                await self.display_item_price(ctx, name_groups[unique_names[0]], realm, new_session)

    async def get_commodities_cached(self, session: aiohttp.ClientSession) -> Dict:
        now = time.time()
        if self.commodities_cache and now - self.commodities_cache_time < CACHE_DURATION:
            return self.commodities_cache
        token = await self.get_access_token(session)
        if not token:
            return {}
        url = "https://us.api.blizzard.com/data/wow/auctions/commodities"
        headers = {"Authorization": f"Bearer {token}"}
        data = await self.safe_get(session, url, headers=headers, params={"namespace": "dynamic-us", "locale": "en_US"})
        if data:
            self.commodities_cache = data
            self.commodities_cache_time = now
        return self.commodities_cache or {}

    async def display_item_price(self, context, variants, realm, session):
        if not isinstance(variants, list):
            variants = [variants]

        commodities = await self.get_commodities_cached(session)
        commodity_auctions = commodities.get("auctions", [])
        
        target_ids = {v["id"] for v in variants}
        commodity_prices = {}
        for a in commodity_auctions:
            item_id = a["item"]["id"]
            if item_id in target_ids:
                commodity_prices.setdefault(item_id, []).append(a["unit_price"])

        is_commodity = any(vid in commodity_prices for vid in target_ids)

        realm_data = None
        if realm and not is_commodity:
            realm_key = realm.lower().replace(" ", "").replace("-", "").replace("'", "")
            realm_id = REALMS.get(realm_key)
            if realm_id:
                token = await self.get_access_token(session)
                url = f"https://us.api.blizzard.com/data/wow/connected-realm/{realm_id}/auctions"
                headers = {"Authorization": f"Bearer {token}"}
                realm_data = await self.safe_get(session, url, headers=headers, params={"namespace": "dynamic-us", "locale": "en_US"})

        active_variants = []
        for item in variants:
            item_id = item["id"]
            prices = commodity_prices.get(item_id, [])
            if not prices and realm_data:
                for auction in realm_data.get("auctions", []):
                    if auction["item"]["id"] == item_id:
                        p = auction.get("unit_price") or auction.get("buyout")
                        if p: prices.append(p)
            
            if prices:
                item["prices"] = prices
                active_variants.append(item)

        display_variants = active_variants if active_variants else variants
        display_variants.sort(key=lambda x: (x.get("tier") or 0, x["id"]))

        name = display_variants[0]['name']
        location = "Global" if is_commodity else (realm.title() if realm else "Unknown Realm")
        title = f"💰 {name} ({location})"
            
        embed = discord.Embed(title=title, color=discord.Color.gold())
        icon = await self.get_item_icon(session, display_variants[0]["id"])
        if icon:
            embed.set_thumbnail(url=icon)

        any_found = False
        for i, item in enumerate(display_variants):
            prices = item.get("prices", [])
            tier = item.get("tier")
            
            if len(display_variants) == 1:
                field_name = "Current Price"
            else:
                if tier:
                    field_name = TIER_LABELS.get(tier, f"Quality {tier}")
                else:
                    field_name = f"Quality {i+1}"
                
            if prices:
                lowest = min(prices) / 10000
                avg = sum(prices) / len(prices) / 10000
                embed.add_field(
                    name=field_name,
                    value=f"**Lowest:** {lowest:,.2f}g\n**Avg:** {avg:,.2f}g\n**Listings:** {len(prices):,}",
                    inline=True
                )
                any_found = True
            elif len(display_variants) > 1:
                embed.add_field(name=field_name, value="No listings found", inline=True)

        if not any_found:
            msg = f"❌ No auctions found for **{name}**."
            if isinstance(context, discord.Interaction):
                return await context.response.edit_message(content=msg, embed=None, view=None)
            return await context.send(msg)

        if isinstance(context, discord.Interaction):
            await context.response.edit_message(embed=embed, view=None)
        else:
            await context.send(embed=embed)

    async def get_guild_roster(self, session: aiohttp.ClientSession, realm: str, guild: str) -> List[Dict]:
        token = await self.get_access_token(session)
        if not token:
            return []
        url = f"https://us.api.blizzard.com/data/wow/guild/{realm}/{guild}/roster"
        data = await self.safe_get(
            session, url,
            params={"namespace": "profile-us", "locale": "en_US"},
            headers={"Authorization": f"Bearer {token}"}
        )
        if not data:
            return []
        return [
            {
                "name": m["character"]["name"],
                "realm": m["character"]["realm"]["slug"],
                "class_id": m["character"]["playable_class"]["id"],
                "level": m["character"]["level"]
            }
            for m in data.get("members", [])
        ]

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

            CURRENT_EXPANSION_NAMES = ["Midnight", "The Midnight Expansion", "The War Within"]
            CURRENT_EXPANSION_IDS = [501, 17, 506]
            weekly_bosses = {"mythic": set(), "heroic": set(), "normal": set()}

            for exp in raid_data.get("expansions", []):
                expansion_info = exp.get("expansion", {})
                is_current = (expansion_info.get("name") in CURRENT_EXPANSION_NAMES) or (expansion_info.get("id") in CURRENT_EXPANSION_IDS)
                if is_current:
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

    def get_class_emoji(self, class_id: int) -> str:
        classes = {
            1: "warrior", 2: "paladin", 3: "hunter", 4: "rogue", 5: "priest", 
            6: "deathknight", 7: "shaman", 8: "mage", 9: "warlock", 10: "monk", 
            11: "druid", 12: "demonhunter", 13: "evoker"
        }
        name = classes.get(class_id, "unknown")
        target = f"wow{name}"
        for emoji in self.bot.emojis:
            if emoji.name.lower() == target:
                return str(emoji)
        
        fallbacks = {
            1: "🛡️", 2: "🔨", 3: "🏹", 4: "🗡️", 5: "✨", 
            6: "❄️", 7: "🌀", 8: "🔮", 9: "💀", 10: "🤜", 
            11: "🍃", 12: "🦇", 13: "🐲"
        }
        return fallbacks.get(class_id, "👤")

    async def fetch_char_stats(self, session: aiohttp.ClientSession, char: Dict) -> Optional[tuple]:
        keys, raid, score = await self.get_vault_data(session, char["name"], char["realm"])
        if sum(keys) > 0 or any(r != "-" for r in raid):
            return (char["name"], keys, raid, score, char["class_id"])
        return None

    async def get_wow_token_price(self, session: aiohttp.ClientSession) -> float:
        token = await self.get_access_token(session)
        if not token: return 0
        url = "https://us.api.blizzard.com/data/wow/token/index"
        headers = {"Authorization": f"Bearer {token}"}
        params = {"namespace": "dynamic-us", "locale": "en_US"}
        data = await self.safe_get(session, url, headers=headers, params=params)
        return data.get("price", 0) / 10000 if data else 0

    async def build_guild_vault_text(self, session: aiohttp.ClientSession) -> str:
        all_members = await self.get_guild_roster(session, self.guild_realm, self.guild_name)
        if not all_members: return "⚠️ Error fetching guild roster."

        guild = [m for m in all_members if m.get("level", 0) >= 80]
        if not guild:
            guild = all_members[:100]

        semaphore = asyncio.Semaphore(5)
        async def sem_fetch(char):
            async with semaphore:
                result = await self.fetch_char_stats(session, char)
                await asyncio.sleep(0.05)
                return result

        tasks = [sem_fetch(char) for char in guild]
        results = await asyncio.gather(*tasks)
        rows = [r for r in results if r is not None]
        rows.sort(key=lambda x: (sum(x[1]), x[3]), reverse=True)

        max_name_len = max((len(name) for name, _, _, _, _ in rows[:25]), default=10)
        max_name_len = min(max_name_len, 20)

        lines = ["🔥 **WEEKLY VAULT LEADERBOARD** 🔥"]
        header = f"| {'Name':<{max_name_len + 3}} | Key Vault | Raid Vault | Score |"
        lines.append(f"👤 `{header}`")
        separator = f"|{'-'*(max_name_len+5)}+-----------+-----------+--------|"
        lines.append(f"⠀ `{separator}`")

        for i, (name, keys, raid, score, class_id) in enumerate(rows[:25], start=1):
            emoji = self.get_class_emoji(class_id)
            key_str = f"{keys[0]}/{keys[1]}/{keys[2]}"
            raid_str = "/".join(raid)
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
                    
                    # If it fits in one message, just send it
                    if len(content) <= 2000:
                        message = await ctx.send(content)
                        self.guild_vault_message_id = message.id
                        self.last_content = content
                        self.save_state()
                        return

                    # Otherwise, paginate
                    lines = content.split('\n')
                    
                    # Extract the footer (assume lines starting with 💰 or Last Updated:)
                    footer_lines = [line for line in lines if line.startswith("💰") or line.startswith("Last Updated:")]
                    body_lines = [line for line in lines if not (line.startswith("💰") or line.startswith("Last Updated:"))]
                    footer = "\n".join(footer_lines)
                    
                    parts = []
                    current_part = ""
                    for line in body_lines:
                        if len(current_part) + len(line) + 1 > 1900:
                            parts.append(current_part)
                            current_part = line
                        else:
                            current_part += ("\n" if current_part else "") + line
                    
                    # Add remaining body and footer
                    if current_part:
                        parts.append(current_part)
                    
                    # Append footer to the last part
                    if footer:
                        if parts:
                            parts[-1] += "\n\n" + footer
                        else:
                            parts.append(footer)
                            
                    for part in parts:
                        await ctx.send(part)
                        
            except Exception as e:
                logger.error(f"Error in guildvault command: {e}")
                await ctx.send(f"⚠️ An error occurred: {e}")

    async def get_character_profile(self, session: aiohttp.ClientSession, name: str, realm: str) -> Optional[Dict]:
        token = await self.get_access_token(session)
        if not token: return None
        url = f"https://us.api.blizzard.com/profile/wow/character/{realm}/{urllib.parse.quote(name.lower())}"
        headers = {"Authorization": f"Bearer {token}"}
        return await self.safe_get(session, url, headers=headers, params={"namespace": "profile-us", "locale": "en_US"})

    async def get_character_media(self, session: aiohttp.ClientSession, name: str, realm: str) -> Optional[str]:
        token = await self.get_access_token(session)
        if not token: return None
        url = f"https://us.api.blizzard.com/profile/wow/character/{realm}/{urllib.parse.quote(name.lower())}/character-media"
        headers = {"Authorization": f"Bearer {token}"}
        data = await self.safe_get(session, url, headers=headers, params={"namespace": "profile-us", "locale": "en_US"})
        if data and data.get("assets"):
            for asset in data["assets"]:
                if asset.get("key") == "main-raw": return asset.get("value")
                if asset.get("key") == "avatar": avatar = asset.get("value")
            return avatar if 'avatar' in locals() else None
        return None

    @commands.command(aliases=['char', 'whois'])
    async def lookup(self, ctx, *, query: str):
        """Lookup a WoW character: name-realm or name."""
        async with ctx.typing():
            name, realm = query, self.guild_realm
            if "-" in query:
                parts = query.rsplit("-", 1)
                name, realm = parts[0].strip(), parts[1].strip().lower().replace(" ", "").replace("'", "")

            async with aiohttp.ClientSession() as session:
                profile = await self.get_character_profile(session, name, realm)
                if not profile:
                    return await ctx.send(f"❌ Character **{name}** on **{realm}** not found.")

                keys, raid, score = await self.get_vault_data(session, name, realm)
                media_url = await self.get_character_media(session, name, realm)

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
                    url=f"https://raider.io/characters/us/{realm}/{urllib.parse.quote(name)}"
                )

                if media_url:
                    embed.set_thumbnail(url=media_url)

                embed.add_field(name="Stats", value=f"**ilvl:** {ilvl}\n**M+ Score:** {score}", inline=True)
                embed.add_field(name="Weekly Vault", value=f"**Keys:** {keys[0]}/{keys[1]}/{keys[2]}\n**Raid:** {'/'.join(raid)}", inline=True)
                
                await ctx.send(embed=embed)


async def setup(bot):
    await bot.add_cog(WoW(bot))
