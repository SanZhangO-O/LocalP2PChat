"""Reset the user's data dir: drop contacts/messages my repro added, keep
identity (device id), nickname and port so the user's identity survives."""
import os
import sqlite3

DB = r"C:\MyOutput\LocalP2PChat\windows\data\localchat.db"
LIVE = r"C:\MyOutput\LocalP2PChat\windows\data\localchat.db"

db = sqlite3.connect(DB)
db.execute("DELETE FROM settings WHERE key IN ('direct_contacts','direct_removed_marks')")
db.execute("DELETE FROM saved_messages")
db.commit()
print("settings now:")
for k, v in db.execute("SELECT key, value FROM settings ORDER BY key"):
    if k in ("direct_contacts", "direct_removed_marks", "device_id", "nickname", "port"):
        print("  ", k, "=", v[:200])
print("message groups:", db.execute("SELECT COUNT(DISTINCT groupId) FROM saved_messages").fetchone()[0])
db.close()
print("CLEANED")