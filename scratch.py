import asyncio
import os
from google import genai
from google.genai import types

async def main():
    client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))
    contents = [
        {"role": "user", "parts": [{"text": "Hello, my name is Bob."}]},
        {"role": "model", "parts": [{"text": "Hello Bob! Nice to meet you."}]},
        {"role": "user", "parts": [{"text": "What is my name?"}]}
    ]
    response = await client.aio.models.generate_content(
        model="gemini-2.5-flash",
        contents=contents
    )
    print("Response:", response.text)

asyncio.run(main())
