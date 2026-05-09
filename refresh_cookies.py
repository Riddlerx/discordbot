import sqlite3
import os

DB_PATH = "/home/ubuntu/.youtube-profile/Default/Cookies"
COOKIES_PATH = "/home/ubuntu/discordbot/cookies.txt"

def refresh_cookies():
    try:
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute('SELECT host_key, path, is_secure, expires_utc, name, value FROM cookies WHERE host_key LIKE "%youtube.com%"')
        
        with open(COOKIES_PATH, 'w') as f:
            f.write("# Netscape HTTP Cookie File\n")
            for host, path, secure, expires, name, value in cursor.fetchall():
                epoch = (expires - 11644473600000000) / 1000000 if expires != 0 else 0
                f.write(f"{host}\tTRUE\t{path}\t{'TRUE' if secure else 'FALSE'}\t{int(epoch)}\t{name}\t{value}\n")
        conn.close()
        print(f"Successfully refreshed cookies at {COOKIES_PATH}")
    except Exception as e:
        print(f"Error refreshing cookies: {e}")

if __name__ == "__main__":
    refresh_cookies()
