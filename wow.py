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
        # FIX: use aiohttp.ClientTimeout instead of raw int
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
            crafted_quality = data.get("crafted_quality")
            tier = crafted_quality.get("tier") if isinstance(crafted_quality, dict) else data.get("quality", {}).get("tier")
            return {
                "id": data["id"],
                "name": data["name"],
                "tier": tier,
                "item_level": data.get("level"),
                "item_class_id": data.get("item_class", {}).get("id"),
                "modified_crafting_category_id": data.get("modified_crafting", {}).get("category", {}).get("id"),
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
            unique_key = (merged.get("id"), merged.get("tier"))
            if unique_key not in seen_keys:
                enriched_items.append(merged)
                seen_keys.add(unique_key)
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
        # Keywords = meaningful words only (no stop words)
        keywords = [w for w in words if w not in STOP_WORDS] or words

        # Search terms: full phrase + each individual keyword
        # This ensures "flask of magister" searches both "*flask of magister*"
        # and "*flask*" and "*magister*" separately, catching "Flask of the Magisters"
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
                        tier = item_data.get("quality", {}).get("tier")
                        all_matches[item_id] = {"id": item_id, "name": name, "tier": tier}

        if not all_matches:
            return []

        def keyword_in_name(kw: str, name_lower: str) -> bool:
            """Check if a keyword appears in the name, handling plural/singular."""
            if kw in name_lower:
                return True
            if kw + "s" in name_lower:       # magister -> magisters
                return True
            if kw + "es" in name_lower:       # box -> boxes
                return True
            if kw.endswith("s") and kw[:-1] in name_lower:   # magisters -> magister
                return True
            return False

        def match_score(item: Dict) -> int:
            """Count how many keywords appear in the item name."""
            name_lower = item["name"].lower()
            return sum(1 for kw in keywords if keyword_in_name(kw, name_lower))

        # Keep only items that match ALL keywords; fall back to best partial matches
        max_score = len(keywords)
        full_matches = [i for i in all_matches.values() if match_score(i) == max_score]
        candidates = full_matches if full_matches else sorted(
            all_matches.values(), key=match_score, reverse=True
        )[:20]

        # Final tie-break: prefer shorter names (closer to what user typed)
        candidates.sort(key=lambda x: len(x["name"]))
        return candidates[:5]

    @commands.command()
    async def price(self, ctx, *, search: str):
        async with ctx.typing():
            item_name, realm = search, "frostmourne"
            if ":" in search:
                parts = search.rsplit(":", 1)
                item_name, realm = parts[0].strip(), parts[1].strip()

            async with aiohttp.ClientSession() as session:
                item_results = await self.search_items(session, item_name)
                if not item_results:
                    return await ctx.send(f"❌ Item **{item_name}** not found.")
                item_results = await self.enrich_item_results(session, item_results)

            # Group variants by name — same item with Q1/Q2/Q3 becomes one entry
            name_groups: Dict[str, list] = {}
            for item in item_results:
                name_groups.setdefault(item["name"], []).append(item)
            unique_names = list(name_groups.keys())

            async def on_name_selected(interaction: discord.Interaction, index: int):
                selected_name = unique_names[index]
                variants = name_groups[selected_name]
                async with aiohttp.ClientSession() as new_session:
                    await self.display_item_price(interaction, variants, realm, new_session)

            if len(unique_names) > 1:
                embed = discord.Embed(
                    title="💰 Multiple matches found",
                    description="Select an item below:",
                    color=discord.Color.gold()
                )
                display_items = [{"name": n} for n in unique_names]
                return await ctx.send(embed=embed, view=ItemSelectionView(display_items, on_name_selected))

            # Single unique name — show all tier prices at once
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

    async def _get_prices_for_item(self, item: Dict, commodities: Dict, realm_data: Optional[Dict]) -> list:
        """Collect auction prices for a single item id."""
        prices = []
        for auction in commodities.get("auctions", []):
            if auction["item"]["id"] == item["id"]:
                prices.append(auction["unit_price"])
        if realm_data:
            for auction in realm_data.get("auctions", []):
                if auction["item"]["id"] == item["id"]:
                    price = auction.get("unit_price") or auction.get("buyout")
                    if price:
                        prices.append(price)
        return prices

    async def display_item_price(self, context, variants, realm, session):
        """Show price embed for one item name, with one field per quality tier."""
        if not isinstance(variants, list):
            variants = [variants]

        commodities = await self.get_commodities_cached(session)
        realm_data = None
        if realm:
            realm_key = realm.lower().replace(" ", "").replace("-", "").replace("'", "")
            realm_id = REALMS.get(realm_key)
            if realm_id:
                token = await self.get_access_token(session)
                url = f"https://us.api.blizzard.com/data/wow/connected-realm/{realm_id}/auctions"
                headers = {"Authorization": f"Bearer {token}"}
                realm_data = await self.safe_get(session, url, headers=headers, params={"namespace": "dynamic-us", "locale": "en_US"})

        TIER_NAMES = {1: "Q1 — Base", 2: "Q2 — Crafted", 3: "Q3 — Max"}
        # Sort variants by tier so fields appear Q1 → Q2 → Q3
        variants_sorted = sorted(variants, key=lambda x: x.get("tier") or 0)

        embed = discord.Embed(title=f"💰 {variants_sorted[0]['name']}", color=discord.Color.gold())
        icon = await self.get_item_icon(session, variants_sorted[0]["id"])
        if icon:
            embed.set_thumbnail(url=icon)

        any_found = False
        for item in variants_sorted:
            prices = await self._get_prices_for_item(item, commodities, realm_data)
            tier = item.get("tier")
            field_name = TIER_NAMES.get(tier, "Standard") if isinstance(tier, int) else "Standard"
            if not prices:
                embed.add_field(name=field_name, value="No listings found", inline=True)
            else:
                lowest = min(prices) / 10000
                avg = sum(prices) / len(prices) / 10000
                embed.add_field(
                    name=field_name,
                    value=f"**Lowest:** {lowest:,.2f}g\n**Avg:** {avg:,.2f}g\n**Listings:** {len(prices):,}",
                    inline=True
                )
                any_found = True

        if not any_found:
            msg = f"❌ No auctions found for **{variants_sorted[0]['name']}**."
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
                "class_id": m["character"]["playable_class"]["id"]
            }
            for m in data.get("members", [])
        ]

    async def get_vault_data(self, session: aiohttp.ClientSession, name: str, realm: str) -> tuple:
        # Stub — implement guild vault logic here
        return [0, 0, 0], ["-", "-", "-"], 0


async def setup(bot):
    await bot.add_cog(WoW(bot))
