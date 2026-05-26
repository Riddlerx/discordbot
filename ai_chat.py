import os
import re
import logging
import io
import asyncio
from typing import Optional, Dict
from collections import OrderedDict
import discord
from discord.ext import commands
from google import genai
from google.genai import types

logger = logging.getLogger("discordbot.ai_chat")

class AIChat(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.api_key = os.getenv("GEMINI_API_KEY")
        # Use a list of models to maximize daily quota
        self.models = [
            "gemini-3.5-flash",      # Latest
            "gemini-3.1-flash-lite", # High quota
            "gemini-2.0-flash"       # Reliable fallback
        ]
        
        # Simple LRU cache for responses
        self.cache: Dict[str, str] = OrderedDict()
        self.cache_max_size = 50

        if self.api_key:
            self.client = genai.Client(api_key=self.api_key)
            self.config = types.GenerateContentConfig(
                system_instruction=(
                    "You are a helpful and friendly AI assistant in a Discord server. "
                    "When asked about a World of Warcraft (WoW) character or player, "
                    "you MUST use the 'lookup_wow_character' tool immediately. "
                    "Do NOT tell the user you are going to search or ask for permission. "
                    "Just use the tool, wait for the results, and then provide a final "
                    "answer based on those results. Keep your responses concise and "
                    "engaging. Use Discord markdown formatting. Respond in the same "
                    "language as the user."
                ),
                temperature=0.7,
                max_output_tokens=4096,
                tools=[self.lookup_wow_character]
            )
            logger.info("Gemini AI configured with fallback chain: %s", ", ".join(self.models))
        else:
            logger.warning("GEMINI_API_KEY not found. AI chat will be disabled.")

    async def lookup_wow_character(self, name: str, realm: Optional[str] = None) -> str:
        """
        Lookup a World of Warcraft character's stats, level, class, and progress.
        Use this tool whenever a user mentions a WoW character name or asks for stats.
        
        Args:
            name: The character's name.
            realm: The character's realm (server). Optional.
        """
        logger.info("Tool Call: lookup_wow_character(name=%s, realm=%s)", name, realm)
        wow_cog = self.bot.get_cog("WoW")
        if not wow_cog:
            return "WoW lookup tool is currently unavailable."

        # Common realms in the user's region (OCE) and major US servers
        common_realms = ["nagrand", "saurfang", "frostmourne", "barthilas", "jubeithos", "gundrak", "khazgoroth", "amanthul", "area52", "illidan"]
        
        realms_to_try = []
        if realm:
            clean_realm = realm.lower().replace(" ", "").replace("'", "")
            realms_to_try.append(clean_realm)
            # If specified realm is not in common list, add common ones as fallback
            if clean_realm not in common_realms:
                realms_to_try.extend(common_realms)
        else:
            realms_to_try = common_realms

        import aiohttp
        async with aiohttp.ClientSession() as session:
            for r in realms_to_try:
                try:
                    profile = await wow_cog.get_character_profile(session, name, r)
                    if not profile:
                        continue

                    keys, raid, score = await wow_cog.get_vault_data(session, name, r)
                    
                    char_class = profile.get("character_class", {}).get("name", "Unknown")
                    level = profile.get("level", 0)
                    ilvl = profile.get("equipped_item_level", 0)
                    guild = profile.get("guild", {}).get("name", "No Guild")
                    
                    return (
                        f"Found on Realm: {profile['realm']['name']}\n"
                        f"Name: {profile['name']}\n"
                        f"Level: {level}\n"
                        f"Class: {char_class}\n"
                        f"Item Level: {ilvl}\n"
                        f"Guild: {guild}\n"
                        f"M+ Score: {score}\n"
                        f"Weekly Vault: Keys({keys[0]}/{keys[1]}/{keys[2]}), Raid({'/'.join(raid)})"
                    )
                except Exception as e:
                    logger.warning("Error looking up %s on %s: %s", name, r, e)
                    continue
            
            return f"Character '{name}' was not found on any common realms. Please specify the realm (e.g., 'Name-Realm')."

    async def _call_gemini(self, prompt: str) -> str:
        """Call the Gemini API with fallback logic across multiple models."""
        # Check cache first
        cache_key = prompt.strip().lower()
        if cache_key in self.cache:
            logger.info("Cache hit for prompt: %s", cache_key)
            # Move to end (LRU)
            val = self.cache.pop(cache_key)
            self.cache[cache_key] = val
            return val

        # Try models in order
        for i, model_name in enumerate(self.models):
            logger.debug("Prompting %s: %s", model_name, prompt)
            try:
                # Using the async client (aio)
                response = await self.client.aio.models.generate_content(
                    model=model_name,
                    contents=prompt,
                    config=self.config
                )
                
                logger.debug("%s response received.", model_name)
                
                # Log the stop reason and safety ratings if available
                if hasattr(response, 'candidates') and response.candidates:
                    candidate = response.candidates[0]
                    logger.info("%s finish reason: %s", model_name, candidate.finish_reason)
                    if candidate.finish_reason == "SAFETY":
                        logger.warning("%s response blocked by safety filters.", model_name)
                        return "I'm sorry, I can't answer that due to safety guidelines."
                    if candidate.finish_reason == "MAX_TOKENS":
                        logger.warning("%s response reached max output tokens.", model_name)

                if not response.text:
                    logger.warning("%s returned an empty response. Response object: %s", model_name, response)
                    continue # Try next model
                
                # Add to cache
                self.cache[cache_key] = response.text
                if len(self.cache) > self.cache_max_size:
                    self.cache.popitem(last=False)
                    
                return response.text

            except Exception as e:
                err_msg = str(e)
                if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg:
                    logger.warning("%s quota exhausted. Error: %s", model_name, err_msg)
                    if i == len(self.models) - 1:
                        # If the last fallback also fails, we're out of quota
                        match = re.search(r"retry in ([\d.]+)s", err_msg)
                        retry_after = f"about {match.group(1)}s" if match else "a while"
                        raise APIError(f"All AI daily quotas reached. Please try again in {retry_after}.")
                    continue # Try next model
                
                logger.exception("Error calling %s", model_name)
                raise APIError(err_msg)
        
        return "I'm sorry, I couldn't generate a response."

    async def _expand_prompt(self, user_prompt: str) -> str:
        """Use Gemini to expand a simple prompt into a highly descriptive image generation prompt."""
        if not self.api_key:
            return user_prompt

        system_instruction = (
            "You are an expert AI image generation prompt engineer. "
            "Expand the user's simple search query or prompt into a highly detailed, descriptive, visually stunning English prompt "
            "suitable for generating high-quality art using Flux or Stable Diffusion. "
            "Describe the characters, their key visual elements, features, clothing, poses, expressions, "
            "the setting/background, lighting, style (e.g. detailed anime key visual, digital art, cinematic 3D render), and colors. "
            "Keep the description concise but rich in visual detail (maximum 2-3 sentences). "
            "Return ONLY the expanded prompt text. Do not include any intro, explanation, quotes, or markdown formatting."
        )
        try:
            # Try models in order
            for model_name in self.models:
                try:
                    response = await self.client.aio.models.generate_content(
                        model=model_name,
                        contents=user_prompt,
                        config=types.GenerateContentConfig(
                            system_instruction=system_instruction,
                            temperature=0.7,
                            max_output_tokens=150
                        )
                    )
                    if response.text:
                        expanded = response.text.strip()
                        logger.info("Expanded prompt: %s -> %s", user_prompt, expanded)
                        return expanded
                except Exception as e:
                    logger.warning("Failed to expand prompt with %s: %s", model_name, e)
                    continue
        except Exception as e:
            logger.exception("Failed to expand prompt using Gemini")
        
        return user_prompt

    @commands.command(name="draw")
    @commands.cooldown(1, 15, commands.BucketType.user)
    async def draw(self, ctx, *, prompt: str = None):
        """Generate an image from a text prompt using AI."""
        if not prompt:
            await ctx.send("⚠️ Please provide a prompt. Example: `!draw A cybernetic dragon.`")
            return

        logger.info("Processing !draw from %s: %s", ctx.author, prompt)
        async with ctx.typing():
            try:
                import aiohttp
                from urllib.parse import quote

                # Expand simple prompts with Gemini to get detailed anime/character descriptions
                expanded_prompt = await self._expand_prompt(prompt)

                encoded_prompt = quote(expanded_prompt)
                url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&nologo=true&model=turbo"

                async with aiohttp.ClientSession() as session:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=90)) as resp:
                        if resp.status != 200:
                            logger.error("Pollinations returned status %d", resp.status)
                            await ctx.send("⚠️ Image generation failed. Please try again.")
                            return

                        image_bytes = await resp.read()

                file = discord.File(io.BytesIO(image_bytes), filename="generated_image.png")
                
                description = f"**Prompt:** {prompt}"
                if expanded_prompt != prompt:
                    # Show the expanded version so the user sees the details Gemini added
                    description += f"\n\n*AI interpretation: {expanded_prompt[:150]}...*"

                embed = discord.Embed(
                    title="🎨 AI Generated Image",
                    description=description,
                    color=discord.Color.purple()
                )
                embed.set_image(url="attachment://generated_image.png")
                embed.set_footer(text=f"Requested by {ctx.author.display_name}")
                await ctx.send(embed=embed, file=file)

            except asyncio.TimeoutError:
                await ctx.send("⏳ Image generation timed out. Please try again with a simpler prompt.")
            except Exception as e:
                logger.exception("Error in !draw image generation")
                await ctx.send("⚠️ Something went wrong generating the image. Please try again.")

    @draw.error
    async def draw_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏳ Please wait {error.retry_after:.1f}s before drawing again.", delete_after=5)
        else:
            self.bot.dispatch("command_error", ctx, error)

    @commands.command(name="ask")
    @commands.cooldown(1, 10, commands.BucketType.user)
    async def ask(self, ctx, *, prompt: str = None):
        """Ask the AI a question."""
        if not self.api_key:
            await ctx.send("⚠️ AI chat is not configured. Missing `GEMINI_API_KEY`.")
            return

        if not prompt:
            await ctx.send("⚠️ Please provide a prompt. Example: `!ask What is the capital of France?`")
            return

        logger.info("Processing !ask from %s: %s", ctx.author, prompt)
        async with ctx.typing():
            try:
                text = await self._call_gemini(prompt)
                logger.info("Gemini returned %d characters", len(text))

                # Discord has a 2000 character limit per message
                if len(text) > 2000:
                    chunks = [text[i:i + 1990] for i in range(0, len(text), 1990)]
                    for chunk in chunks[:3]:
                        await ctx.send(chunk)
                else:
                    await ctx.send(text)

            except APIError as e:
                logger.error("AI API error: %s", e)
                if "daily quotas reached" in str(e).lower():
                    await ctx.send("🛑 **Daily chat limit reached!** The AI has run out of juice for today. Please try again tomorrow! 😴")
                else:
                    await ctx.send(f"⚠️ {e}")
            except Exception as e:
                logger.exception("Unexpected error in AI chat")
                await ctx.send("⚠️ Something went wrong. Please try again.")

    @ask.error
    async def ask_error(self, ctx, error):
        if isinstance(error, commands.CommandOnCooldown):
            await ctx.send(f"⏳ Please wait {error.retry_after:.1f}s before asking again.", delete_after=5)



class APIError(Exception):
    pass


async def setup(bot):
    await bot.add_cog(AIChat(bot))
