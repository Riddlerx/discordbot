import os
import logging
import discord
from discord.ext import commands
from google import genai
from google.genai import types

logger = logging.getLogger("discordbot.ai_chat")

class AIChat(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.api_key = os.getenv("GEMINI_API_KEY")
        self.client = None
        self.model_name = 'gemini-1.5-flash'
        
        if self.api_key:
            try:
                self.client = genai.Client(api_key=self.api_key)
                logger.info("Gemini AI client configured successfully.")
            except Exception as e:
                logger.error("Failed to configure Gemini AI client: %s", e)
        else:
            logger.warning("GEMINI_API_KEY not found in environment. AI chat will be disabled.")

    @commands.command(name="ask")
    async def ask(self, ctx, *, prompt: str = None):
        """Ask the AI a question."""
        if not self.client:
            await ctx.send("⚠️ AI chat is not configured. Missing API key.")
            return

        if not prompt:
            await ctx.send("⚠️ Please provide a prompt. Example: `!ask What is the capital of France?`")
            return

        # Show typing indicator while generating response
        async with ctx.typing():
            try:
                # Prepare the prompt
                final_prompt = (
                    ("🔍 Give me the *current* status: " + prompt)
                    if "current" not in prompt.lower()
                    else prompt
                )

                # Use the async client to generate content
                response = await self.client.aio.models.generate_content(
                    model=self.model_name,
                    contents=final_prompt,
                    config=types.GenerateContentConfig(
                        temperature=0.7,
                        top_p=0.95,
                    ),
                )
                
                # Discord has a 2000 character limit per message
                text = response.text.strip()
                if len(text) > 2000:
                    # If it's too long, truncate it
                    text = text[:1996] + "..."
                
                await ctx.send(text)
            except Exception as e:
                logger.error("Error generating AI content: %s", e)
                await ctx.send("⚠️ Sorry, I encountered an error while trying to answer that. The AI might be busy or blocked the prompt.")

async def setup(bot):
    await bot.add_cog(AIChat(bot))
