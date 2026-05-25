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
        self.model = None
        
        if self.api_key:
            genai.configure(api_key=self.api_key)
            self.model = genai.GenerativeModel('gemini-1.5-flash')
            logger.info("Gemini AI model configured successfully.")
        else:
            logger.warning("GEMINI_API_KEY not found in environment. AI chat will be disabled.")

    @commands.command(name="ask")
    async def ask(self, ctx, *, prompt: str = None):
        """Ask the AI a question."""
        if not self.model:
            await ctx.send("⚠️ AI chat is not configured. Missing API key.")
            return

        if not prompt:
            await ctx.send("⚠️ Please provide a prompt. Example: `!ask What is the capital of France?`")
            return

        # Show typing indicator while generating response
        async with ctx.typing():
            try:
                # Run the blocking API call in an executor to prevent freezing the bot
                response = await self.bot.loop.run_in_executor(
                    None, 
                    lambda: self.model.generate_content(prompt)
                )
                
                # Discord has a 2000 character limit per message
                text = response.text
                if len(text) > 2000:
                    # If it's too long, truncate it
                    text = text[:1996] + "..."
                
                await ctx.send(text)
            except Exception as e:
                logger.error("Error generating AI content: %s", e)
                await ctx.send("⚠️ Sorry, I encountered an error while trying to answer that. The AI might be busy or blocked the prompt.")

async def setup(bot):
    await bot.add_cog(AIChat(bot))
