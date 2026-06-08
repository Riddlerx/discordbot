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

STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_state.json")
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
        
        # Booster tracking state
        self.booster_config: List[Dict] = []  # List of {discord_id, name, realm, last_run_at, weekly_count}
        self.last_weekly_report: str = ""  # ISO date of last Tuesday report

        self.blizzard_semaphore = asyncio.Semaphore(10)
        self.auto_update_task: Optional[asyncio.Task] = None
        self.state_lock = asyncio.Lock()
        self._token_lock = asyncio.Lock()  # prevents concurrent token refresh races
        self._session: Optional[aiohttp.ClientSession] = None  # shared session, created in cog_load
        # Cache run-details per session to avoid hammering a broken endpoint with the same IDs.
        # Maps run_id -> dict (successful data) or run_id -> None (confirmed unavailable).
        self._run_details_cache: Dict[int, Optional[Dict]] = {}
        # In-flight deduplication: if two coroutines ask for the same run_id concurrently,
        # only one HTTP request is made; the second awaits the first's Future.
        self._run_details_inflight: Dict[int, asyncio.Future] = {}

    async def cog_load(self):
        # Shared session avoids per-command TCP handshakes and DNS lookups
        self._session = aiohttp.ClientSession()
        self._run_details_cache = {} # Force re-evaluation on restart
        await self.load_state()

    async def cog_unload(self):
        if self.auto_update_task:
            self.auto_update_task.cancel()
        if hasattr(self, 'booster_tracker_task') and self.booster_tracker_task:
            self.booster_tracker_task.cancel()
        if hasattr(self, 'weekly_report_task') and self.weekly_report_task:
            self.weekly_report_task.cancel()
        if self._session and not self._session.closed:
            await self._session.close()

    def _read_state_file(self) -> dict:
        with open(STATE_FILE, "r") as f:
            return json.load(f)

    def _write_state_file(self, state: dict) -> None:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)

    def _parse_char_query(self, query: str) -> tuple:
        """Extract name and realm from a query string. Supports 'name-realm' or just 'name'."""
        name, realm = query.strip(), self.guild_realm
        if "-" in query:
            # WoW names cannot have dashes, so the first dash must be the separator
            parts = query.split("-", 1)
            name = parts[0].strip()
            # Normalize realm slug: spaces/dashes to hyphens, remove quotes
            realm = parts[1].strip().lower().replace(" ", "-").replace("'", "")
            # Ensure multiple hyphens (from user input like area--52) are collapsed
            while "--" in realm:
                realm = realm.replace("--", "-")
        return name, realm

    async def load_state(self):
        """Load persistent bot state without blocking the event loop."""
        async with self.state_lock:
            if not os.path.exists(STATE_FILE):
                return
            try:
                state = await asyncio.to_thread(self._read_state_file)
                self.guild_vault_message_id = state.get("guild_vault_message_id")
                self.last_content = state.get("last_content")
                self.booster_config = state.get("booster_config", [])
                self.last_weekly_report = state.get("last_weekly_report", "")
            except Exception as e:
                logger.warning("Error loading state: %s", e)

    async def save_state(self):
        """Persist bot state without blocking the event loop."""
        state = {
            "guild_vault_message_id": self.guild_vault_message_id,
            "last_content": self.last_content,
            "booster_config": self.booster_config,
            "last_weekly_report": self.last_weekly_report
        }
        async with self.state_lock:
            await asyncio.to_thread(self._write_state_file, state)

    async def get_run_details(self, session: aiohttp.ClientSession, run_id: int) -> Optional[Dict]:
        """Fetch run details with caching and in-flight deduplication.

        - If the result is already cached (including a cached None for 500s), return immediately.
        - If another coroutine is already fetching the same run_id, await its result instead
          of making a duplicate HTTP request (fixes concurrent scan_booster + deep_scan races).
        - Caches None for failed requests so they are never retried in the same session.
        """
        # 1. Already have the result.
        if run_id in self._run_details_cache:
            return self._run_details_cache[run_id]

        # 2. Another coroutine is fetching right now — piggyback on it.
        if run_id in self._run_details_inflight:
            return await asyncio.shield(self._run_details_inflight[run_id])

        # 3. We are the first — create a Future so others can wait on us.
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self._run_details_inflight[run_id] = future
        try:
            # Raider.io API is currently bugged where season=current throws a 500 error for run-details.
            # Using the explicit season slug season-mn-1 fixes it.
            url = f"https://raider.io/api/v1/mythic-plus/run-details?season=season-mn-1&id={run_id}"
            logger.info(f"  -> Fetching details: {url}")
            result = await self.safe_get(session, url, retries=5)
            self._run_details_cache[run_id] = result  # cache None too — no retries
            future.set_result(result)
            return result
        except Exception as exc:
            future.set_exception(exc)
            raise
        finally:
            self._run_details_inflight.pop(run_id, None)

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
                            logger.debug(f"HTTP {response.status} for {url}")
                            return None
                        elif response.status == 429:
                            wait_time = delay * 5 + random.uniform(1, 5)
                            logger.warning("Rate limited (429), waiting %.1fs", wait_time)
                            await asyncio.sleep(wait_time)
                        else:
                            if response.status == 500:
                                wait_time = delay * (2 ** attempt) + random.uniform(1, 3)
                                logger.debug(f"HTTP 500 on attempt {attempt}/{retries}, waiting {wait_time:.1f}s: {url}")
                                await asyncio.sleep(wait_time)
                            else:
                                logger.warning(f"HTTP {response.status} on attempt {attempt}/{retries}: {url}")
                                if attempt < retries:
                                    await asyncio.sleep(delay)
            except Exception as e:
                logger.warning(f"Request error on attempt {attempt}/{retries}: {e} — {url}")
                if attempt == retries:
                    logger.error(f"Request failed after {retries} attempts: {e}")
                elif attempt < retries:
                    await asyncio.sleep(delay)
        return None

    async def get_access_token(self, session: aiohttp.ClientSession) -> Optional[str]:
        # Lock prevents two concurrent commands both refreshing an expired token
        async with self._token_lock:
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

            session = self._session
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
                await self.display_item_price(interaction, variants, realm, session)

            if len(unique_names) > 1:
                exact_match = next((n for n in unique_names if n.lower() == item_name.lower()), None)
                if exact_match:
                    return await self.display_item_price(ctx, name_groups[exact_match], realm, session)

                embed = discord.Embed(
                    title="💰 Multiple matches found",
                    description="Select an item below:",
                    color=discord.Color.gold()
                )
                display_items = [{"name": n} for n in unique_names[:5]]
                return await ctx.send(embed=embed, view=ItemSelectionView(display_items, on_name_selected))

            await self.display_item_price(ctx, name_groups[unique_names[0]], realm, session)

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

    async def get_wow_token_prices(self, session: aiohttp.ClientSession) -> str:
        """
        Get the current WoW Token price in gold for all major regions (US, EU, KR, TW).
        Use this tool whenever a user asks for WoW Token prices.
        """
        regions = {
            "us": "dynamic-us",
            "eu": "dynamic-eu",
            "kr": "dynamic-kr",
            "tw": "dynamic-tw"
        }
        
        token = await self.get_access_token(session)
        if not token:
            return "Unable to authenticate with Blizzard API."

        headers = {"Authorization": f"Bearer {token}"}
        
        async def fetch_region(region_name, namespace):
            url = f"https://{region_name}.api.blizzard.com/data/wow/token/index"
            params = {"namespace": namespace, "locale": "en_US"}
            try:
                data = await self.safe_get(session, url, headers=headers, params=params)
                if data and "price" in data:
                    return f"{region_name.upper()}: {data['price'] / 10000:,.0f} gold"
            except Exception as e:
                logger.warning("Error fetching token price for %s: %s", region_name, e)
            return None

        tasks = [fetch_region(r, ns) for r, ns in regions.items()]
        results = await asyncio.gather(*tasks)
        
        prices = [r for r in results if r]
        if not prices:
            return "Could not retrieve WoW Token prices at this time."
            
        return "\n".join(prices)

    async def get_wow_token_price(self, session: aiohttp.ClientSession) -> float:
        """Deprecated: use get_wow_token_prices for multiple regions. Still used by guild vault."""
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

        # Show at most 25, but we will truncate later if it exceeds 2000 chars
        display_rows = rows[:25]
        
        max_name_len = max((len(name) for name, _, _, _, _ in display_rows), default=10)
        max_name_len = min(max_name_len, 20)

        def generate_content(limit_rows):
            lines = ["🔥 **WEEKLY VAULT LEADERBOARD** 🔥"]
            header = f"| {'Name':<{max_name_len + 3}} | Key Vault | Raid Vault | Score |"
            lines.append(f"👤 `{header}`")
            separator = f"|{'-'*(max_name_len+5)}+-----------+-----------+--------|"
            lines.append(f"⠀ `{separator}`")

            for i, (name, keys, raid, score, class_id) in enumerate(limit_rows, start=1):
                emoji = self.get_class_emoji(class_id)
                key_str = f"{keys[0]}/{keys[1]}/{keys[2]}"
                raid_str = "/".join(raid)
                row = f"| #{i:<2} {name:<{max_name_len}} | {key_str:^9} | {raid_str:^9} | {score:>5} |"
                lines.append(f"{emoji} `{row}`")

            lines.append(f"⠀ `{separator}`")
            
            return lines

        lines = generate_content(display_rows)
        
        # Add footer
        token_price = 0
        try:
            token_price = await self.get_wow_token_price(session)
        except Exception as e:
            logger.warning("Error fetching token price: %s", e)

        footer_lines = []
        if token_price > 0:
            footer_lines.append(f"💰 **WoW Token Price:** {token_price:,.0f}g")
        
        unix_now = int(time.time())
        footer_lines.append(f"Last Updated: <t:{unix_now}:R>")
        
        # Check total length and truncate rows if necessary
        while len("\n".join(lines + footer_lines)) > 1950 and len(display_rows) > 5:
            display_rows.pop()
            lines = generate_content(display_rows)

        return "\n".join(lines + footer_lines)

    @commands.Cog.listener()
    async def on_ready(self):
        if self.auto_update_task is None or self.auto_update_task.done():
            self.auto_update_task = self.bot.loop.create_task(self.auto_update())
            logger.info("Started WoW leaderboard auto-update task.")
        
        if not hasattr(self, 'booster_tracker_task') or self.booster_tracker_task is None or self.booster_tracker_task.done():
            self.booster_tracker_task = self.bot.loop.create_task(self.booster_auto_tracker())
            logger.info("Started WoW booster auto-tracker task.")

        if not hasattr(self, 'weekly_report_task') or self.weekly_report_task is None or self.weekly_report_task.done():
            self.weekly_report_task = self.bot.loop.create_task(self.weekly_report_checker())
            logger.info("Started WoW weekly report checker task.")

    async def auto_update(self):
        await self.bot.wait_until_ready()
        async with aiohttp.ClientSession() as session:
            while not self.bot.is_closed():
                if self.guild_vault_message_id is None:
                    await asyncio.sleep(60)
                    continue

                channel = self.bot.get_channel(self.guild_channel_id)
                if not channel:
                    try:
                        channel = await self.bot.fetch_channel(self.guild_channel_id)
                    except Exception as e:
                        logger.warning(f"Could not find channel {self.guild_channel_id}: {e}")
                        await asyncio.sleep(60)
                        continue

                try:
                    try:
                        message = await channel.fetch_message(self.guild_vault_message_id)
                    except discord.NotFound:
                        logger.warning(f"Leaderboard message {self.guild_vault_message_id} not found. Stopping auto-update until manual reset.")
                        self.guild_vault_message_id = None
                        await self.save_state()
                        continue
                    except discord.Forbidden:
                        logger.error(f"Missing permissions to fetch leaderboard message in {channel.name}")
                        await asyncio.sleep(300)
                        continue
                    
                    new_content = await self.build_guild_vault_text(session)
                    if new_content != self.last_content:
                        try:
                            await message.edit(content=new_content, embed=None)
                            self.last_content = new_content
                            await self.save_state()
                            logger.info("Updated leaderboard text message_id=%s", self.guild_vault_message_id)
                        except discord.HTTPException as e:
                            logger.error(f"Failed to edit leaderboard message: {e}")
                            if e.code == 50035: # Invalid Form Body (e.g. too long)
                                logger.error("Content might be too long even after truncation.")
                except Exception as e:
                    logger.exception("Leaderboard update loop error: %s", e)
                
                await asyncio.sleep(1800)

    async def scan_booster(self, tracker, session: aiohttp.ClientSession) -> bool:
        """Scan a single booster character for new runs. Returns True if new runs were found."""
        try:
            name, realm = tracker["name"], tracker["realm"]
            last_run_at = tracker.get("last_run_at", "")

            rio_url = f"https://raider.io/api/v1/characters/profile?region=us&realm={urllib.parse.quote(realm.lower())}&name={urllib.parse.quote(name.lower())}&fields=mythic_plus_recent_runs"
            
            data = await self.safe_get(session, rio_url)
            if not data:
                return False

            recent_runs = data.get("mythic_plus_recent_runs", [])
            new_runs = []       # completed_at of confirmed boosts
            all_completed = []  # completed_at of ALL qualifying runs seen (to advance last_run_at)

            # Ensure counted_runs exists once, before the loop
            if "counted_runs" not in tracker:
                tracker["counted_runs"] = []

            for run in recent_runs:
                # Track runs that are Mythic 10+ (Midnight Expansion)
                level = run.get("mythic_level", 0)
                if level >= 10:
                    completed_at = run.get("completed_at")
                    if completed_at and completed_at > last_run_at:
                        run_id = run.get("keystone_run_id")
                        if not run_id:
                            continue

                        # Always track that we've seen this run, even if it's not a boost.
                        # This advances last_run_at so we don't re-evaluate it next hour.
                        all_completed.append(completed_at)

                        # Prevent double counting boosts
                        if run_id in tracker["counted_runs"]:
                            continue
                            
                        run_details = await self.get_run_details(session, run_id)
                        
                        # Efficiency check — always available from the basic run summary
                        clear_time_ms = run.get("clear_time_ms", 0)
                        par_time_ms = run.get("par_time_ms", 0)
                        efficiency = clear_time_ms / par_time_ms if par_time_ms > 0 else 1.0

                        is_boost = False
                        is_definitive = False # True if we successfully checked the details
                        reason = ""

                        if run_details:
                            is_definitive = True
                            roster = run_details.get("roster", [])
                            # Role Check: Multiple tanks or healers indicate a carry
                            tanks = sum(1 for m in roster if m.get("character", {}).get("spec", {}).get("role") == "TANK")
                            healers = sum(1 for m in roster if m.get("character", {}).get("spec", {}).get("role") == "HEALER")
                            # Buyer Check: player below 275 ilvl; ignore 0 (missing data)
                            buyer_found = any(
                                0 < m.get("items", {}).get("item_level_equipped", 0) < 275
                                for m in roster
                            )
                            if buyer_found:
                                is_boost = True
                                reason = "Buyer detected (<275 ilvl)"
                            elif tanks > 1 or healers > 1:
                                is_boost = True
                                reason = f"Role mismatch ({tanks}T/{healers}H)"
                            elif efficiency <= 0.75:
                                is_boost = True
                                reason = f"Fast clear ({efficiency:.1%})"
                        else:
                            # run-details unavailable (raider.io 500) — fall back to efficiency only
                            if efficiency <= 0.75:
                                is_boost = True
                                reason = f"Fast clear ({efficiency:.1%}) [details unavailable]"
                            else:
                                # DO NOT mark as definitive, DO NOT add to counted_runs
                                logger.warning(f"  -> Skipping definitive check for {run.get('dungeon')} +{level} due to API error (will retry)")
                                continue # This continues the for loop, skipping the counting logic below for THIS run

                        # --- Counting Logic (only reached if definitive check or successful fallback) ---
                        
                        # Add to counted_runs definitively
                        tracker["counted_runs"].append(run_id)
                        # Keep the list manageable
                        if len(tracker["counted_runs"]) > 100:
                            tracker["counted_runs"] = tracker["counted_runs"][-100:]

                        if is_boost:
                            new_runs.append(completed_at)
                            logger.info(f"BOOST DETECTED: {name} cleared {run.get('dungeon')} +{level} - Reason: {reason}")
                        else:
                            logger.info(f"  -> Not a boost: {run.get('dungeon')} +{level} | Eff: {efficiency:.1%}")

            # Advance last_run_at past ALL seen runs (not just boosts), so they
            # aren't re-evaluated on the next hourly cycle.
            if all_completed:
                tracker["last_run_at"] = max(all_completed)

            if new_runs:
                count = len(new_runs)
                tracker["weekly_count"] = tracker.get("weekly_count", 0) + count
                tracker["last_run_at"] = max(new_runs)
                logger.info("Added %d boost runs for %s-%s", count, name, realm)
                return True
        except Exception as e:
            logger.error("Error tracking boost runs for %s-%s: %s", tracker.get("name"), tracker.get("realm"), e)
        return False

    async def booster_auto_tracker(self):
        """Periodically poll Raider.io for new Mythic 10+ runs for registered boosters."""
        await self.bot.wait_until_ready()
        # Use the shared session (self._session) so that get_run_details' Future-based
        # deduplication works correctly across concurrent scan_booster + deep_scan calls.
        # A separate ClientSession here would bypass the inflight cache entirely.
        while not self.bot.is_closed():
            if not self.booster_config:
                await asyncio.sleep(60)
                continue

            logger.info("Starting boost auto-tracker cycle for %d characters", len(self.booster_config))
            updated = False
            for tracker in self.booster_config:
                if await self.scan_booster(tracker, self._session):
                    updated = True
                await asyncio.sleep(2) # Avoid aggressive polling

            if updated:
                await self.save_state()

            await asyncio.sleep(3600) # Run every hour

    async def weekly_report_checker(self):
        """Check for WoW Tuesday reset and post the weekly booster summary."""
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            try:
                # WoW US Reset is Tuesday 15:00 UTC (8:00 AM PST)
                now = time.gmtime()
                # 1 = Tuesday
                if now.tm_wday == 1 and now.tm_hour >= 15:
                    today_iso = f"{now.tm_year}-{now.tm_mon:02d}-{now.tm_mday:02d}"
                    if self.last_weekly_report != today_iso:
                        await self.send_weekly_booster_report()
                        self.last_weekly_report = today_iso
                        # Reset counts for all characters
                        for tracker in self.booster_config:
                            tracker["weekly_count"] = 0
                        await self.save_state()
            except Exception as e:
                logger.error("Error in weekly report checker: %s", e)
            
            await asyncio.sleep(3600) # Check every hour

    async def send_weekly_booster_report(self):
        """Send the weekly booster summary to the guild channel."""
        channel = self.bot.get_channel(self.guild_channel_id)
        if not channel:
            try:
                channel = await self.bot.fetch_channel(self.guild_channel_id)
            except Exception:
                logger.error("Could not find guild channel %s for weekly report", self.guild_channel_id)
                return

        # Sort all tracked characters by count
        sorted_trackers = sorted(self.booster_config, key=lambda x: x.get("weekly_count", 0), reverse=True)
        
        embed = discord.Embed(
            title="📊 Weekly Booster Run Summary",
            description="Mythic 8+ Boosting runs completed this week:",
            color=discord.Color.blue(),
            timestamp=discord.utils.utcnow()
        )

        if not sorted_trackers or all(t.get("weekly_count", 0) == 0 for t in sorted_trackers):
            embed.description = "No boosting runs tracked this week."
        else:
            lines = []
            for t in sorted_trackers:
                count = t.get("weekly_count", 0)
                if count > 0:
                    lines.append(f"• **{t['name']}-{t['realm'].title()}**: {count} runs")
            
            if lines:
                embed.add_field(name="Character Performance", value="\n".join(lines), inline=False)
            else:
                embed.description = "No boosting runs tracked this week."

        await channel.send(embed=embed)

    @commands.command()
    async def guildvault(self, ctx):
        async with ctx.typing():
            try:
                session = self._session
                content = await self.build_guild_vault_text(session)

                # If it fits in one message, just send it
                if len(content) <= 2000:
                    message = await ctx.send(content)
                    self.guild_vault_message_id = message.id
                    self.last_content = content
                    await self.save_state()
                    return

                # Otherwise, paginate (though build_guild_vault_text now truncates to fit)
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
        if not data or not data.get("assets"):
            return None
        avatar: Optional[str] = None
        for asset in data["assets"]:
            key = asset.get("key")
            if key == "main-raw":
                return asset.get("value")   # best quality, return immediately
            if key == "avatar":
                avatar = asset.get("value")  # keep as fallback
        return avatar  # None if neither asset type was found

    @commands.command(aliases=['char', 'whois'])
    async def lookup(self, ctx, *, query: str):
        """Lookup a WoW character: name-realm or name."""
        async with ctx.typing():
            name, realm = self._parse_char_query(query)

            session = self._session
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

    async def cog_command_error(self, ctx, error):
        if isinstance(error, commands.CheckFailure):
            await ctx.send("SEND A MESSAGE TO Riddler")
        else:
            logger.error(f"Error in {ctx.command}: {error}")

    @commands.group(name="booster", invoke_without_command=True)
    async def booster(self, ctx):
        """Weekly booster tracking (Midnight 10+)."""
        if ctx.invoked_subcommand is None:
            embed = discord.Embed(
                title="🚀 Booster Run Tracking (Midnight 12.0.5)",
                description=(
                    "Track the number of 'Boosting Runs' completed each week.\n"
                    "A boost is flagged if:\n"
                    "• **Level:** Mythic 10 or higher\n"
                    "• **Buyer:** Any player is < 275 ilvl\n"
                    "• **Roles:** > 1 Tank or > 1 Healer\n"
                    "• **Time:** Standard group clears in < 75% of timer\n\n"
                    "**Commands:**\n"
                    "`!booster register name-realm` - Start tracking\n"
                    "`!booster stats` - View weekly counts\n"
                    "`!booster adjust name-realm <amount>` - Manual fix (Mod only)"
                ),
                color=discord.Color.blue()
            )
            await ctx.send(embed=embed)

    @booster.command(name="register")
    @commands.check(lambda ctx: ctx.author.id == 692434522532479127)
    async def booster_register(self, ctx, *args):
        """Register a character. Optional: !booster register [friend_name] <Name-Realm>"""
        if len(args) == 1:
            friend_name = None
            char_query = args[0]
        elif len(args) >= 2:
            friend_name = args[0]
            char_query = " ".join(args[1:])
        else:
            return await ctx.send(f"⚠️ Usage: `{ctx.prefix}booster register [friend_name] <Name-Realm>`")

        name, realm = self._parse_char_query(char_query)

        async with ctx.typing():
            profile = await self.get_character_profile(self._session, name, realm)
            if not profile:
                return await ctx.send(f"❌ Character **{name}** on **{realm}** not found.")

            # Check if already registered
            for t in self.booster_config:
                if t["name"].lower() == name.lower() and t["realm"].lower() == realm.lower():
                    return await ctx.send(f"⚠️ **{name}-{realm}** is already being tracked.")

            # Calculate the most recent Tuesday 15:00 UTC
            now_ts = time.time()
            dt_utc = time.gmtime(now_ts)
            days_since_tue = (dt_utc.tm_wday - 1) % 7
            
            # Get Tuesday at 15:00 UTC
            import calendar
            tue_date = time.gmtime(now_ts - days_since_tue * 86400)
            tue_reset_str = f"{tue_date.tm_year}-{tue_date.tm_mon:02d}-{tue_date.tm_mday:02d} 15:00:00"
            tue_reset_ts = calendar.timegm(time.strptime(tue_reset_str, "%Y-%m-%d %H:%M:%S"))
            
            # If it is Tuesday but before 15:00, go back 7 days
            if now_ts < tue_reset_ts:
                tue_reset_ts -= 7 * 86400
                
            iso_start = time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(tue_reset_ts))
            
            new_tracker = {
                "friend_name": friend_name,
                "name": profile["name"],
                "realm": profile["realm"]["slug"],
                "last_run_at": iso_start,
                "weekly_count": 0
            }
            self.booster_config.append(new_tracker)
            
            # Immediate scan to pick up runs since reset
            await self.scan_booster(new_tracker, self._session)
            await self.save_state()
            
            msg = f"✅ Registered **{profile['name']}-{profile['realm']['name']}**"
            if friend_name:
                msg += f" for **{friend_name}**"
            msg += "! 🚀"
            await ctx.send(msg)

    @booster.command(name="register_bulk")
    @commands.check(lambda ctx: ctx.author.id == 692434522532479127)
    async def booster_register_bulk(self, ctx, friend_name: str, *, char_list: str):
        """Register multiple characters at once, comma separated. (Admin only)"""
        queries = [q.strip() for q in char_list.split(',')]
        results = []
        
        async with ctx.typing():
            for query in queries:
                name, realm = self._parse_char_query(query)
                profile = await self.get_character_profile(self._session, name, realm)
                
                if not profile:
                    results.append(f"❌ {query}: Not found")
                    continue
                
                # Check if already registered
                if any(t["name"].lower() == profile["name"].lower() and t["realm"].lower() == profile["realm"]["slug"] for t in self.booster_config):
                    results.append(f"⚠️ {query}: Already tracked")
                    continue

                # Calculate Tuesday reset
                now_ts = time.time()
                dt_utc = time.gmtime(now_ts)
                days_since_tue = (dt_utc.tm_wday - 1) % 7
                import calendar
                tue_date = time.gmtime(now_ts - days_since_tue * 86400)
                tue_reset_str = f"{tue_date.tm_year}-{tue_date.tm_mon:02d}-{tue_date.tm_mday:02d} 15:00:00"
                tue_reset_ts = calendar.timegm(time.strptime(tue_reset_str, "%Y-%m-%d %H:%M:%S"))
                if now_ts < tue_reset_ts: tue_reset_ts -= 7 * 86400
                iso_start = time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime(tue_reset_ts))
                
                new_tracker = {
                    "friend_name": friend_name,
                    "name": profile["name"],
                    "realm": profile["realm"]["slug"],
                    "last_run_at": iso_start,
                    "weekly_count": 0
                }
                self.booster_config.append(new_tracker)
                await self.scan_booster(new_tracker, self._session)
                results.append(f"✅ {profile['name']}-{profile['realm']['name']}: Registered for {friend_name}")
            
            await self.save_state()
            await ctx.send("📋 **Bulk Registration Results:**\n" + "\n".join(results))

    @booster.command(name="unregister")
    @commands.check(lambda ctx: ctx.author.id == 692434522532479127)
    async def booster_unregister(self, ctx, *, char_query: str):
        """Stop tracking a character (Admin only)."""
        name, realm = self._parse_char_query(char_query)

        found = False
        new_trackers = []
        for t in self.booster_config:
            if t["name"].lower() == name.lower() and t["realm"].lower() == realm.lower():
                found = True
            else:
                new_trackers.append(t)
        
        if found:
            self.booster_config = new_trackers
            await self.save_state()
            await ctx.send(f"✅ Stopped tracking **{name}-{realm}**.")
        else:
            await ctx.send(f"❌ **{name}-{realm}** was not being tracked.")

    @booster.command(name="list")
    @commands.check(lambda ctx: ctx.author.id == 692434522532479127)
    async def booster_list(self, ctx):
        """List all registered characters (Admin only)."""
        if not self.booster_config:
            return await ctx.send("❌ No characters are currently registered.")
            
        lines = []
        for t in self.booster_config:
            name = t["name"]
            realm = t["realm"]
            f_name = t.get("friend_name")
            line = f"• **{name}**-{realm.title()}"
            if f_name:
                line += f" (Linked to: {f_name})"
            lines.append(line)
        
        await ctx.send("📋 **Registered Boosters:**\n" + "\n".join(lines))

    @booster.command(name="clear_cache")
    @commands.check(lambda ctx: ctx.author.id == 692434522532479127)
    async def booster_clear_cache(self, ctx):
        """Clear the run details cache to force re-evaluation of runs."""
        self._run_details_cache = {}
        await ctx.send("✅ Cleared run details cache. You can now run `!booster deep_scan` again.")

    async def _perform_deep_scan(self, ctx, tracker):
        """Helper to re-evaluate recent runs for a tracker."""
        original_last_run = tracker.get("last_run_at")
        # Reset last_run_at to re-scan recent runs
        tracker["last_run_at"] = ""
        
        await self.scan_booster(tracker, self._session)
        
        # Restore original last_run_at if it was newer, 
        # but keep it updated if we found newer runs.
        if original_last_run and tracker.get("last_run_at", "") < original_last_run:
            tracker["last_run_at"] = original_last_run
            
        await self.save_state()
        await ctx.send(f"✅ Deep scan complete for **{tracker['name']}-{tracker['realm']}**.")

    @booster.command(name="deep_scan")
    @commands.check(lambda ctx: ctx.author.id == 692434522532479127)
    async def booster_deep_scan(self, ctx, *, char_query: str):
        """Manually re-evaluate the last 10 runs for a character and add any missed boosts (Admin only)."""
        self._run_details_cache = {}
        name, realm = self._parse_char_query(char_query)
        
        async with ctx.typing():
            # Find the tracker
            tracker = None
            for t in self.booster_config:
                if t["name"].lower() == name.lower() and t["realm"].lower() == realm.lower():
                    tracker = t
                    break
            
            if not tracker:
                return await ctx.send(f"❌ **{name}-{realm}** is not being tracked.")
            
            await self._perform_deep_scan(ctx, tracker)

    @booster.command(name="deep_scan_all")
    @commands.check(lambda ctx: ctx.author.id == 692434522532479127)
    async def booster_deep_scan_all(self, ctx):
        """Manually re-evaluate the last 10 runs for ALL registered characters (Admin only)."""
        if not self.booster_config:
            return await ctx.send("❌ No characters are currently registered.")
            
        await ctx.send(f"🔄 Starting deep scan for all {len(self.booster_config)} registered characters... This may take a minute.")
        self._run_details_cache = {}
        async with ctx.typing():
            for tracker in self.booster_config:
                await self._perform_deep_scan(ctx, tracker)
                await asyncio.sleep(1)
        await ctx.send("🎉 Finished deep scanning all characters!")

    @booster.command(name="stats")
    async def booster_stats(self, ctx):
        """View the current weekly boosting run counts, grouped by friend."""
        async with ctx.typing():
            # Force a deep scan of recent runs to catch anything missed
            updated = False
            for tracker in self.booster_config:
                if await self.scan_booster(tracker, self._session):
                    updated = True

            if updated:
                await self.save_state()

            embed = discord.Embed(
                title="🚀 Weekly Booster Stats",
                color=discord.Color.green(),
                timestamp=discord.utils.utcnow()
            )

            if not self.booster_config:
                embed.description = "No characters registered for tracking."
            else:
                # Group by friend_name
                friend_data = {}
                for t in self.booster_config:
                    f_name = t.get("friend_name")
                    if not f_name or f_name == "Unknown":
                        # If no friend name, treat the character name as the group name
                        f_name = t["name"]

                    if f_name not in friend_data:
                        friend_data[f_name] = {"total": 0, "chars": []}

                    count = t.get("weekly_count", 0)
                    friend_data[f_name]["total"] += count
                    # Only add to chars list if the friend group has more than one char or the name differs
                    if len(friend_data[f_name]["chars"]) > 0 or f_name != t["name"]:
                        friend_data[f_name]["chars"].append(f"{t['name']} ({count})")

                # Sort friends by total count
                sorted_friends = sorted(friend_data.items(), key=lambda x: x[1]["total"], reverse=True)

                lines = []
                for f_name, data in sorted_friends:
                    if data["total"] > 0:
                        display = f"• **{f_name}**: {data['total']} runs"
                        if data["chars"]:
                            display += f" ({', '.join(data['chars'])})"
                        lines.append(display)

                if lines:
                    embed.description = "\n".join(lines)
                else:
                    embed.description = "No boosting runs completed this week yet."

            await ctx.send(embed=embed)

    @booster.command(name="link_chars")
    @commands.check(lambda ctx: ctx.author.id == 692434522532479127)
    async def booster_link_chars(self, ctx, friend_name: str, *, char_list: str):
        """Link multiple characters to a single friend name (Admin only)."""
        queries = [q.strip() for q in char_list.split(',')]
        linked_count = 0
        
        for query in queries:
            name, realm = self._parse_char_query(query)
            
            # Find the character
            found = False
            for t in self.booster_config:
                if t["name"].lower() == name.lower() and t["realm"].lower() == realm.lower():
                    t["friend_name"] = friend_name
                    found = True
                    linked_count += 1
                    break
            
            if not found:
                await ctx.send(f"⚠️ Could not find registered character: **{query}**")
        
        if linked_count > 0:
            await self.save_state()
            await ctx.send(f"✅ Successfully linked **{linked_count}** character(s) to **{friend_name}**.")
        else:
            await ctx.send("❌ No characters were linked.")

    @booster.command(name="adjust")
    @commands.check(lambda ctx: ctx.author.id == 692434522532479127)
    async def booster_adjust(self, ctx, *, args: str):
        """Add or subtract from a character's weekly count (Admin only)."""
        parts = args.rsplit(None, 1)
        if len(parts) < 2:
            return await ctx.send(f"⚠️ Usage: `{ctx.prefix}booster adjust <name-realm> <amount>`")
            
        char_query, amount_str = parts[0], parts[1]
        try:
            amount = int(amount_str)
        except ValueError:
            return await ctx.send("⚠️ Amount must be a number (e.g., -5 or 2).")

        name, realm = self._parse_char_query(char_query)
        
        def norm(r): return r.lower().replace(" ", "").replace("-", "").replace("'", "")
        
        target_norm = norm(realm)
        found_tracker = None
        for t in self.booster_config:
            if t["name"].lower() == name.lower() and norm(t["realm"]) == target_norm:
                t["weekly_count"] = max(0, t.get("weekly_count", 0) + amount)
                found_tracker = t
                break
        
        if found_tracker:
            await self.save_state()
            action = "Added" if amount > 0 else "Removed"
            await ctx.send(f"✅ {action} **{abs(amount)}** runs for **{found_tracker['name']}-{found_tracker['realm']}**. New total: **{found_tracker['weekly_count']}**.")
        else:
            await ctx.send(f"❌ **{char_query}** is not being tracked. Check the name and realm.")


async def setup(bot):
    await bot.add_cog(WoW(bot))
