import os
import re
import logging
import io
import asyncio
import aiohttp
from typing import Optional, Dict
from collections import OrderedDict
import discord
from discord.ext import commands
from google import genai
from google.genai import types
import edge_tts
import time

logger = logging.getLogger("discordbot.ai_chat")

class AIChat(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.api_key = os.getenv("GEMINI_API_KEY")
        # Prioritizing 2.5 models because they have the 'Search Grounding' quota (1.5K).
        # Gemini 3.x models currently have a Search Grounding limit of 0 in this project.
        self.models = [
            "gemini-2.5-flash",      # Has Search Grounding (1.5K)
            "gemini-2.5-flash-lite", # Has Search Grounding (1.5K)
            "gemini-3.1-flash-lite", # High chat quota (500), use as fallback
            "gemini-3.5-flash"       # Backup quality model
        ]
        
        # Simple LRU cache for responses
        self.cache: Dict[str, str] = OrderedDict()
        self.cache_max_size = 50

        if self.api_key:
            self.client = genai.Client(api_key=self.api_key)
            self.common_config = {
                "system_instruction": (
                    "You are a helpful, friendly, and slightly witty AI assistant in a Discord server. "
                    "Your personality is engaging and helpful. Use Discord markdown formatting effectively. "
                    "When asked about a World of Warcraft (WoW) character, player, or stats, "
                    "you MUST use the 'lookup_wow_character' tool immediately. "
                    "For real-time information like weather, news, or current events, use the Google Search tool. "
                    "Do NOT tell the user you are going to search; just use the tools and wait for results. "
                    "Provide a detailed and engaging final answer based on the tools' output. "
                    "Respond in the same language as the user."
                ),
                "temperature": 0.7,
                "max_output_tokens": 4096,
            }
            self.wow_config = types.GenerateContentConfig(
                **self.common_config,
                tools=[self.lookup_wow_character]
            )
            self.search_config = types.GenerateContentConfig(
                **self.common_config,
                tools=[types.Tool(google_search=types.GoogleSearch())]
            )
            logger.info("Gemini AI configured with fallback chain: %s", ", ".join(self.models))
        else:
            logger.warning("GEMINI_API_KEY not found. AI chat will be disabled.")

        self._session: Optional[aiohttp.ClientSession] = None  # shared HTTP session

    async def cog_load(self):
        self._session = aiohttp.ClientSession()

    async def cog_unload(self):
        if self._session and not self._session.closed:
            await self._session.close()

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

        # Reuse the shared session instead of creating a new one per tool call
        session = self._session
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
        """Call the Gemini API with fallback logic and dynamic tool selection."""
        # Check cache first
        cache_key = prompt.strip().lower()
        if cache_key in self.cache:
            logger.info("Cache hit for prompt: %s", cache_key)
            val = self.cache.pop(cache_key)
            self.cache[cache_key] = val
            return val

        # Determine priority based on keywords
        p_lower = prompt.lower()
        is_wow = any(k in p_lower for k in ["wow", "character", "realm", "level", "stats", "gear", "guild"])
        is_rt = any(k in p_lower for k in ["weather", "news", "price", "who won", "today", "current"])

        # Decide which config to try first
        primary_config = self.wow_config if is_wow else self.search_config
        secondary_config = self.search_config if is_wow else self.wow_config

        # Try models in order
        for i, model_name in enumerate(self.models):
            for config_to_use in [primary_config, secondary_config]:
                logger.debug("Prompting %s with %s: %s", model_name, "WoW" if config_to_use == self.wow_config else "Search", prompt)
                try:
                    response = await self.client.aio.models.generate_content(
                        model=model_name,
                        contents=prompt,
                        config=config_to_use
                    )
                    
                    if not response.text:
                        continue
                    
                    self.cache[cache_key] = response.text
                    if len(self.cache) > self.cache_max_size:
                        self.cache.popitem(last=False)
                        
                    return response.text

                except Exception as e:
                    err_msg = str(e).upper()
                    if "429" in err_msg or "RESOURCE_EXHAUSTED" in err_msg or "QUOTA" in err_msg:
                        logger.warning("%s quota exhausted. Waiting 2s...", model_name)
                        await asyncio.sleep(2)
                        break # Try next model
                    
                    if "400" in err_msg and "CANNOT BE COMBINED" in err_msg:
                        continue # Try the other config for this same model
                    
                    if i == len(self.models) - 1 and config_to_use == secondary_config:
                        logger.exception("Final error calling %s", model_name)
                        raise APIError(str(e))
                    
                    logger.warning("Error calling %s with tool: %s", model_name, e)
                    continue 
        
        return "I'm sorry, I couldn't generate a response."

    async def _expand_prompt(self, user_prompt: str) -> str:
        """Use Gemini to expand a simple prompt into a highly descriptive image generation prompt."""
        if not self.api_key:
            return user_prompt

        system_instruction = (
            "You are an expert AI image generation prompt engineer. "
            "Expand the user's simple request into a highly detailed, descriptive, visually stunning prompt "
            "specifically optimized for the Flux image generation model. "
            "Focus on artistic composition, cinematic lighting, intricate textures, and specific artistic styles "
            "(e.g., hyper-realistic digital art, detailed anime key visual, oil painting). "
            "Describe the subjects, their environment, the mood, and the camera angle. "
            "Keep the expansion to 2-3 sentences. Return ONLY the expanded prompt text."
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
                            temperature=0.8,
                            max_output_tokens=200
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

                # Expand simple prompts with Gemini to get detailed descriptions
                expanded_prompt = await self._expand_prompt(prompt)

                encoded_prompt = quote(expanded_prompt)
                # Using Flux model on Pollinations for much higher quality
                url = f"https://image.pollinations.ai/prompt/{encoded_prompt}?width=1024&height=1024&nologo=true&model=flux"

                async with self._session.get(url, timeout=aiohttp.ClientTimeout(total=120)) as resp:
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

    @commands.command(name="say")
    @commands.cooldown(1, 5, commands.BucketType.user)
    async def say(self, ctx, *, text: str = None):
        """Make the AI speak in your voice channel. Smart: can tell jokes or answer questions!"""
        if not text:
            await ctx.send("⚠️ Please provide some text or a prompt. Example: `!say tell me a joke` or `!say Hello everyone!`")
            return

        if not ctx.author.voice:
            await ctx.send("⚠️ You must be in a voice channel to use this command.")
            return

        async with ctx.typing():
            try:
                # --- Smart Logic: Determine if we should generate content or just repeat ---
                speech_text = text
                
                # If it doesn't look like a forced repeat (not in quotes and not starting with 'repeat')
                if not (text.startswith('"') and text.endswith('"')) and not text.lower().startswith("repeat "):
                    try:
                        # Ask Gemini to decide: Repeat or Generate?
                        system_msg = (
                            "You are a voice assistant. The user wants you to say something in a Discord voice channel. "
                            "If the input is a simple message (like 'Hello', 'I'm back'), just return that exact text. "
                            "If the input is a request (like 'tell me a joke', 'what is the weather'), generate a short, "
                            "friendly, and natural-sounding spoken response (max 2 sentences). "
                            "Return ONLY the text to be spoken. Do not use markdown or emojis."
                        )
                        # Use a fast model for this
                        response = await self.client.aio.models.generate_content(
                            model=self.models[1] if len(self.models) > 1 else self.models[0], # Prefer 3.1 Flash Lite
                            contents=text,
                            config=types.GenerateContentConfig(
                                system_instruction=system_msg,
                                temperature=0.7,
                                max_output_tokens=100
                            )
                        )
                        if response.text:
                            speech_text = response.text.strip()
                            logger.info("Smart Say: '%s' -> '%s'", text, speech_text)
                    except Exception as ge:
                        logger.warning("Gemini failed for Smart Say, falling back to literal repeat: %s", ge)

                # Remove 'repeat ' prefix if it was used to force repeat
                if text.lower().startswith("repeat "):
                    speech_text = text[7:]
                elif text.startswith('"') and text.endswith('"'):
                    speech_text = text[1:-1]

                # --- TTS Generation ---
                voice = "en-US-GuyNeural"
                communicate = edge_tts.Communicate(speech_text, voice)
                
                output_file = f"tts_{ctx.guild.id}_{int(time.time())}.mp3"
                await communicate.save(output_file)

                music_cog = self.bot.get_cog("Music")
                if not music_cog:
                    await ctx.send("⚠️ Music system is not available.")
                    if os.path.exists(output_file): os.remove(output_file)
                    return

                vc = ctx.guild.voice_client
                if not vc:
                    try:
                        vc = await ctx.author.voice.channel.connect()
                        # Inform music cog of the connection to avoid recovery conflicts
                        st = music_cog.state(ctx.guild.id)
                        st.last_voice_channel_id = ctx.author.voice.channel.id
                    except Exception as e:
                        await ctx.send(f"⚠️ Could not connect to voice: {e}")
                        if os.path.exists(output_file): os.remove(output_file)
                        return

                if vc.is_playing() or vc.is_paused():
                    await music_cog.play(ctx, search=output_file)
                    await ctx.message.add_reaction("🗣️")
                else:
                    def after_playing(error):
                        if error: logger.error("Error playing TTS: %s", error)
                        if os.path.exists(output_file):
                            try: os.remove(output_file)
                            except: pass

                    # Add reconnect options for extra stability
                    ffmpeg_opts = "-reconnect 1 -reconnect_at_eof 1 -reconnect_streamed 1 -reconnect_delay_max 2"
                    source = await discord.FFmpegOpusAudio.from_probe(output_file, options=ffmpeg_opts)
                    vc.play(source, after=after_playing)
                    await ctx.message.add_reaction("🗣️")

            except Exception as e:
                logger.exception("Error in !say")
                await ctx.send(f"⚠️ Failed to generate voice: {e}")
                if 'output_file' in locals() and os.path.exists(output_file):
                    try: os.remove(output_file)
                    except Exception:
                        pass



class APIError(Exception):
    pass


async def setup(bot):
    await bot.add_cog(AIChat(bot))
