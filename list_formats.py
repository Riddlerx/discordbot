import yt_dlp
import os

YDL_OPTIONS = {
    "quiet": True,
    "no_warnings": True,
    "extractor_args": {
        "youtube": {
            "player_client": ["ios", "android", "mweb"],
        }
    },
    "youtube_include_dash_manifest": True,
    "youtube_include_hls_manifest": True,
}

# Try to find cookies
cookies_path = "/home/winhtutooheart3/discordbot/cookies.txt"
if os.path.exists(cookies_path):
    YDL_OPTIONS["cookiefile"] = cookies_path
    print(f"Using cookies from {cookies_path}")

def list_formats():
    query = "p6U7zIY6zkA"
    with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
        try:
            info = ydl.extract_info(query, download=False)
            print(f"Title: {info.get('title')}")
            formats = info.get("formats", [])
            print(f"Found {len(formats)} formats.")
            for f in formats:
                print(f"ID: {f.get('format_id')}, Ext: {f.get('ext')}, Note: {f.get('format_note')}, ACR: {f.get('acodec')}")
        except Exception as e:
            print(f"Extraction failed: {e}")

if __name__ == "__main__":
    list_formats()
