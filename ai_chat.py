import os
import logging
import aiohttp
import discord
from discord.ext import commands

logger = logging.getLogger("discordbot.ai_chat")

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"


class AIChat(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.groq_api_key = os.getenv("GROQ_API_KEY")
        self.model_name = "llama-3.3-70b-versatile"

        if self.groq_api_key:
            logger.info("Groq AI configured (model: %s)", self.model_name)
        else:
            logger.warning("GROQ_API_KEY not found. AI chat will be disabled.")

    async def _call_groq(self, prompt: str) -> str:
        """Call the Groq API and return the response text."""
        headers = {
            "Authorization": f"Bearer {self.groq_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model_name,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a helpful and friendly AI assistant in a Discord server. "
                        "Keep your responses concise and engaging. Use markdown formatting "
                        "supported by Discord (bold, italic, code blocks, etc). "
                        "Respond in the same language as the user's message."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.7,
            "max_tokens": 1024,
            "top_p": 0.95,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(GROQ_API_URL, headers=headers, json=payload) as resp:
                data = await resp.json()

                if resp.status == 429:
                    retry_after = data.get("error", {}).get("message", "")
                    logger.warning("Groq rate limited: %s", retry_after)
                    raise RateLimitError(retry_after)

                if resp.status != 200:
                    error_msg = data.get("error", {}).get("message", f"HTTP {resp.status}")
                    logger.error("Groq API error (%d): %s", resp.status, error_msg)
                    raise APIError(error_msg)

                return data["choices"][0]["message"]["content"]

    @commands.command(name="ask")
    async def ask(self, ctx, *, prompt: str = None):
        """Ask the AI a question."""
        if not self.groq_api_key:
            await ctx.send("⚠️ AI chat is not configured. Missing API key.")
            return

        if not prompt:
            await ctx.send("⚠️ Please provide a prompt. Example: `!ask What is the capital of France?`")
            return

        async with ctx.typing():
            try:
                text = await self._call_groq(prompt)

                # Discord has a 2000 character limit per message
                if len(text) > 2000:
                    # Split into chunks instead of truncating
                    chunks = [text[i:i + 1990] for i in range(0, len(text), 1990)]
                    for chunk in chunks[:3]:  # max 3 chunks to avoid spam
                        await ctx.send(chunk)
                else:
                    await ctx.send(text)

            except RateLimitError:
                await ctx.send(
                    "⏳ **Rate limited.** Too many requests right now. "
                    "Please wait a moment and try again."
                )
            except APIError as e:
                logger.error("AI API error: %s", e)
                await ctx.send("⚠️ The AI service returned an error. Please try again later.")
            except Exception as e:
                logger.error("Unexpected error in AI chat: %s", e)
                await ctx.send("⚠️ Something went wrong. Please try again.")


class RateLimitError(Exception):
    pass


class APIError(Exception):
    pass


async def setup(bot):
    await bot.add_cog(AIChat(bot))
