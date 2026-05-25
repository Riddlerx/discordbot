import os
import logging
import discord
from discord.ext import commands
import google.generativeai as genai

logger = logging.getLogger("discordbot.ai_chat")

class AIChat(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.api_key = os.getenv("GEMINI_API_KEY")
        self.model_name = "gemini-1.5-flash"
        
        if self.api_key:
            genai.configure(api_key=self.api_key)
            self.model = genai.GenerativeModel(
                model_name=self.model_name,
                system_instruction=(
                    "You are a helpful and friendly AI assistant in a Discord server. "
                    "Keep your responses concise and engaging. Use markdown formatting "
                    "supported by Discord (bold, italic, code blocks, etc). "
                    "Respond in the same language as the user's message."
                )
            )
            logger.info("Gemini AI configured (model: %s)", self.model_name)
        else:
            logger.warning("GEMINI_API_KEY not found. AI chat will be disabled.")

    async def _call_gemini(self, prompt: str) -> str:
        """Call the Gemini API and return the response text."""
        try:
            response = await self.model.generate_content_async(prompt)
            if not response.text:
                logger.warning("Gemini returned an empty response.")
                return "I'm sorry, I couldn't generate a response."
            return response.text
        except Exception as e:
            logger.error("Error calling Gemini API: %s", e)
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

        async with ctx.typing():
            try:
                text = await self._call_gemini(prompt)

                # Discord has a 2000 character limit per message
                if len(text) > 2000:
                    # Split into chunks instead of truncating
                    chunks = [text[i:i + 1990] for i in range(0, len(text), 1990)]
                    for chunk in chunks[:3]:  # max 3 chunks to avoid spam
                        await ctx.send(chunk)
                else:
                    await ctx.send(text)

            except APIError as e:
                logger.error("AI API error: %s", e)
                await ctx.send("⚠️ The AI service returned an error. Please try again later.")
            except Exception as e:
                logger.error("Unexpected error in AI chat: %s", e)
                await ctx.send("⚠️ Something went wrong. Please try again.")


class APIError(Exception):
    pass


async def setup(bot):
    await bot.add_cog(AIChat(bot))
