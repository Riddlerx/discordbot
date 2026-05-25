import os
import logging
from typing import Optional
import discord
from discord.ext import commands
from google import genai
from google.genai import types

logger = logging.getLogger("discordbot.ai_chat")

class AIChat(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.api_key = os.getenv("GEMINI_API_KEY")
        # In May 2026, gemini-3.5-flash is the latest stable release.
        self.model_name = "gemini-3.5-flash"

        
        if self.api_key:
            self.client = genai.Client(api_key=self.api_key)
            self.config = types.GenerateContentConfig(
                system_instruction=(
                    "You are a helpful and friendly AI assistant in a Discord server. "
                    "Keep your responses concise and engaging. Use markdown formatting "
                    "supported by Discord (bold, italic, code blocks, etc). "
                    "Respond in the same language as the user's message. "
                    "You have access to World of Warcraft character lookup tools. "
                    "When asked about a WoW player, use these tools to get their real stats."
                ),
                temperature=0.7,
                max_output_tokens=4096,
                tools=[self.lookup_wow_character]
            )
            logger.info("Gemini AI configured with google-genai (model: %s)", self.model_name)
        else:
            logger.warning("GEMINI_API_KEY not found. AI chat will be disabled.")

    async def lookup_wow_character(self, name: str, realm: Optional[str] = None) -> str:
        """
        Lookup a World of Warcraft character's stats, level, class, and progress.
        If the realm is unknown, the tool will try to find the character on common OCE/US realms.
        
        Args:
            name: The character's name.
            realm: The character's realm (server). Optional.
        """
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
        """Call the Gemini API using the new google-genai SDK."""
        logger.debug("Prompting Gemini: %s", prompt)
        try:
            # Using the async client (aio)
            response = await self.client.aio.models.generate_content(
                model=self.model_name,
                contents=prompt,
                config=self.config
            )
            
            logger.debug("Gemini response received.")
            
            # Log the stop reason and safety ratings if available
            if hasattr(response, 'candidates') and response.candidates:
                candidate = response.candidates[0]
                logger.info("Gemini finish reason: %s", candidate.finish_reason)
                if candidate.finish_reason == "SAFETY":
                    logger.warning("Gemini response blocked by safety filters.")
                    return "I'm sorry, I can't answer that due to safety guidelines."
                if candidate.finish_reason == "MAX_TOKENS":
                    logger.warning("Gemini response reached max output tokens.")

            if not response.text:
                logger.warning("Gemini returned an empty response or was blocked. Response object: %s", response)
                return "I'm sorry, I couldn't generate a response."
            
            return response.text
        except Exception as e:
            logger.exception("Error calling Gemini API")
            raise APIError(str(e))

    @commands.command(name="ask")
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
                await ctx.send(f"⚠️ The AI service returned an error: `{e}`")
            except Exception as e:
                logger.exception("Unexpected error in AI chat")
                await ctx.send("⚠️ Something went wrong. Please try again.")


class APIError(Exception):
    pass


async def setup(bot):
    await bot.add_cog(AIChat(bot))
