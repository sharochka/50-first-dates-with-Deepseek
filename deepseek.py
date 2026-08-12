#!/usr/bin/env python3
"""
Shifu — "我开酒吧，你带问题。"

A sentient bar terminal with persistent memory, live tools,
vision, voice, file ingestion, and Telegram situational awareness.
Designed to be run as a Streamlit app.
"""

import os
import re
import io
import json
import time
import base64
import sqlite3
import logging
import hashlib
from datetime import datetime
from pathlib import Path
from collections import defaultdict

import requests
import streamlit as st
from bs4 import BeautifulSoup
from openai import OpenAI
import speech_recognition as sr
from PIL import Image

# ============================================================================
# BASIC CONFIG
# ============================================================================

APP_TITLE = "Laozi's Bar"
APP_ICON = "🍶"

MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
VISION_MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-4o-mini")

DB_PATH = os.getenv("SHIFU_BROWSER_DB", "shifu_browser_memory.db")
TELEGRAM_CACHE_DB = os.getenv("TELEGRAM_CACHE_DB", "shifu_telegram_cache.db")

MAX_RECENT_TURNS = 18
MAX_LINK_CHARS = 7000
MAX_TOOL_RESULT_CHARS = 6000
MAX_TELEGRAM_POSTS_PER_CHANNEL = 15
RECENT_VISIBLE_TURNS = int(os.getenv("SHIFU_BROWSER_RECENT_VISIBLE_TURNS", "8"))

APP_DIR = Path(__file__).resolve().parent
BACKGROUND_IMAGE_PATH = Path(
    os.getenv("LAOZI_BAR_BACKGROUND", str(APP_DIR / "laozi_terminal_bar.png"))
)
SHOW_BACKGROUND_IMAGE = (
    os.getenv("LAOZI_BAR_SHOW_BACKGROUND", "1").strip().lower()
    not in {"0", "false", "no", "off"}
)

logging.basicConfig(
    filename="shifu_browser.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("shifu-browser")

# ============================================================================
# TELEGRAM CHANNEL CONFIGURATION
# ============================================================================

TELEGRAM_CHANNELS = [
    "pilotblog", "GeneralStaffZSU", "ZelenskyyOfficial", "air_alert_ua",
    "ArmyTV_ua", "bavovna_in_ua", "kyivinfo", "DeepStateUA", "Liveuamap",
    "monitor_the_situation", "ConflictsTracker"
]

_env_channels = os.getenv("TELEGRAM_CHANNELS", "")
if _env_channels:
    TELEGRAM_CHANNELS = [c.strip() for c in _env_channels.split(",") if c.strip()]

# ============================================================================
# DATABASES (MEMORY & TELEGRAM)
# ============================================================================

def _init_telegram_cache_db():
    conn = sqlite3.connect(TELEGRAM_CACHE_DB)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS telegram_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT NOT NULL,
            post_url TEXT,
            timestamp TEXT,
            views TEXT,
            text_content TEXT NOT NULL,
            scraped_at TEXT NOT NULL DEFAULT (datetime('now')),
            content_hash TEXT UNIQUE
        )
    """)
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS telegram_posts_fts USING fts5(
            channel, text_content, content=telegram_posts, content_rowid=id
        )
    """)
    conn.execute("""
        CREATE TRIGGER IF NOT EXISTS tg_ai AFTER INSERT ON telegram_posts BEGIN
            INSERT INTO telegram_posts_fts(rowid, channel, text_content)
            VALUES (new.id, new.channel, new.text_content);
        END
    """)
    conn.commit()
    return conn

TG_CONN = _init_telegram_cache_db()

def _hash_content(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]

def _cache_telegram_post(channel, post_url, timestamp, views, text_content):
    ch = _hash_content(text_content)
    try:
        TG_CONN.execute(
            """INSERT INTO telegram_posts (channel, post_url, timestamp, views, text_content, content_hash)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (channel, post_url, timestamp, views, text_content, ch)
        )
        TG_CONN.commit()
        return True
    except sqlite3.IntegrityError:
        return False

def scrape_telegram_channel(channel_name: str, max_posts: int = MAX_TELEGRAM_POSTS_PER_CHANNEL):
    url = f"https://t.me/s/{channel_name}"
    try:
        r = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"},
            timeout=20,
        )
        if r.status_code != 200:
            return []

        soup = BeautifulSoup(r.text, "html.parser")
        posts = []
        message_wraps = soup.select(".tgme_widget_message_wrap") or soup.select("[class*='tgme_widget_message']")

        for wrap in message_wraps[:max_posts]:
            try:
                time_el = wrap.select_one(".tgme_widget_message_date time")
                timestamp = time_el.get("datetime", "") if time_el else ""
                text_el = wrap.select_one(".tgme_widget_message_text")
                text_content = text_el.get_text("\n", strip=True) if text_el else ""
                link_el = wrap.select_one(".tgme_widget_message_date a")
                post_url = link_el.get("href", "") if link_el else ""
                if post_url and not post_url.startswith("http"):
                    post_url = f"https://t.me{post_url}"

                if text_content:
                    posts.append({"timestamp": timestamp, "text": text_content, "url": post_url})
            except Exception:
                continue
        return posts
    except requests.RequestException:
        return []

def scan_all_telegram_channels(keyword: str = None):
    all_posts = []
    total_new = 0

    for channel in TELEGRAM_CHANNELS:
        posts = scrape_telegram_channel(channel)
        for post in posts:
            text = post["text"]
            if keyword and keyword.lower() not in text.lower():
                continue
            is_new = _cache_telegram_post(channel, post.get("url", ""), post.get("timestamp", ""), "", text)
            if is_new:
                total_new += 1
            all_posts.append({"channel": channel, **post, "is_new": is_new})

    if not all_posts:
        return "[Telegram Scan: No posts found.]"

    lines = [f"═══ TELEGRAM SCAN (New: {total_new}) ═══\n"]
    by_channel = defaultdict(list)
    for p in all_posts:
        by_channel[p["channel"]].append(p)

    for channel, posts in by_channel.items():
        lines.append(f"─── @{channel} ───")
        for post in posts[:5]:
            ts = post.get("timestamp", "?")[:19]
            lines.append(f"[{ts}] {post['text'][:200]}... {post.get('url','')}")
        lines.append("")

    return "\n".join(lines)[:MAX_TOOL_RESULT_CHARS]

def init_chat_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            timestamp TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()

def load_history(limit=MAX_RECENT_TURNS):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT role, content FROM history ORDER BY id DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return [{"role": r, "content": c} for r, c in reversed(rows)]

def save_message(role, content):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO history (role, content) VALUES (?, ?)", (role, content))
    conn.commit()
    conn.close()

init_chat_db()

# ============================================================================
# STREAMLIT UI & FULL TERMINAL THEME
# ============================================================================

st.set_page_config(page_title=APP_TITLE, page_icon=APP_ICON, layout="centered")

def image_file_to_data_uri(path):
    try:
        path = Path(path)
        if not path.is_file():
            return ""
        encoded = base64.b64encode(path.read_bytes()).decode("ascii")
        return f"data:image/png;base64,{encoded}"
    except Exception:
        return ""

background_uri = image_file_to_data_uri(BACKGROUND_IMAGE_PATH) if SHOW_BACKGROUND_IMAGE else ""
if background_uri:
    background_rule = f"linear-gradient(rgba(0, 7, 2, 0.82), rgba(0, 7, 2, 0.94)), url('{background_uri}') center center / cover fixed"
else:
    background_rule = "radial-gradient(circle at 18% 0%, rgba(36, 255, 94, 0.11), transparent 34%), linear-gradient(145deg, #020603 0%, #000000 54%, #061007 100%)"

css = r"""
<style>
:root {
    --crt-green: #55ff77;
    --crt-green-soft: #9affad;
    --crt-green-dim: #1b7a34;
    --crt-panel: rgba(0, 10, 3, 0.86);
    --crt-amber: #e6c66a;
}
html, body, [class*="css"] {
    font-family: "DejaVu Sans Mono", "Liberation Mono", Consolas, monospace !important;
}
.stApp {
    background: __BACKGROUND_RULE__;
    color: var(--crt-green-soft);
    min-height: 100vh;
}
.stApp::before {
    content: "";
    position: fixed; inset: 0; pointer-events: none; z-index: 9998; opacity: 0.17;
    background: repeating-linear-gradient(to bottom, rgba(255,255,255,0.035) 0px, rgba(255,255,255,0.035) 1px, transparent 1px, transparent 4px);
    mix-blend-mode: screen;
}

.block-container {
    max-width: 1000px !important;
    margin: 0 auto !important;
}

[data-testid="stHeader"], header { visibility: hidden !important; }
.stDeployButton { display: none !important; }
#MainMenu { visibility: hidden !important; }

.laozi-terminal-hero {
    position: relative; overflow: hidden; margin: 0 0 1.1rem 0; padding: 1.2rem 1.35rem 1rem 1.35rem;
    border: 1px solid rgba(85, 255, 119, 0.48); border-radius: 5px;
    background: transparent !important;
    box-shadow: 0 0 0 1px rgba(0,0,0,0.92), 0 0 24px rgba(45, 255, 93, 0.24);
    text-shadow: 0 0 8px rgba(85,255,119,0.52);
}
.laozi-kicker { color: var(--crt-green-dim); font-size: 0.74rem; letter-spacing: 0.18em; margin-bottom: 0.34rem; }
.laozi-title { color: var(--crt-green); font-size: clamp(1.85rem, 4vw, 3.3rem); line-height: 1; font-weight: 800; margin: 0; }
.laozi-subtitle { margin-top: 0.55rem; color: var(--crt-green-soft); font-size: 0.9rem; }
.laozi-status-row { display: flex; flex-wrap: wrap; gap: 0.45rem 1rem; margin-top: 0.9rem; padding-top: 0.72rem; border-top: 1px dashed rgba(85,255,119,0.25); color: #7adf8e; font-size: 0.72rem; }

[data-testid="stExpander"] { background: rgba(0, 8, 2, 0.88); border: 1px solid rgba(85,255,119,0.25); border-radius: 4px; }
[data-testid="stExpander"] summary { color: var(--crt-green) !important; }

[data-testid="stChatMessage"] { background: var(--crt-panel); border: 1px solid rgba(85,255,119,0.25); border-left: 4px solid var(--crt-green-dim); border-radius: 4px; padding: 0.8rem 0.95rem; margin: 0.7rem 0; box-shadow: 0 8px 28px rgba(0,0,0,0.32); backdrop-filter: blur(7px); }
[data-testid="stChatMessage"]:has([data-testid="chatAvatarIcon-user"]) { border-left-color: var(--crt-amber); background: rgba(9, 11, 3, 0.89); }
[data-testid="stChatMessage"]:has([data-testid="chatAvatarIcon-assistant"]) { border-left-color: var(--crt-green); }
[data-testid="stChatMessage"] p, li { color: #d1f7d8; line-height: 1.62; }
[data-testid="stChatInput"] { background: rgba(0, 8, 2, 0.96); border: 1px solid rgba(85, 255, 119, 0.48); border-radius: 4px; }
[data-testid="stChatInput"] textarea { color: var(--crt-green-soft) !important; }
.stButton button { background: rgba(0, 24, 6, 0.92) !important; color: var(--crt-green) !important; border: 1px solid rgba(85,255,119,0.45) !important; }
</style>
""".replace("__BACKGROUND_RULE__", background_rule)
st.markdown(css, unsafe_allow_html=True)

st.markdown(
    f"""
    <section class="laozi-terminal-hero">
        <div class="laozi-kicker">&gt; /HOME/SHARAR/THE.BAR/SHIFU</div>
        <div class="laozi-title">LAOZI'S BAR</div>
        <div class="laozi-subtitle">Drink deep. Speak softly. Return to the source.</div>
        <div class="laozi-status-row">
            <span><b>SHIFU:</b> ONLINE</span>
            <span><b>MODEL:</b> {MODEL}</span>
            <span><b>MEMORY:</b> SQLITE + FTS5</span>
            <span><b>SEARCH:</b> ARMED</span>
            <span><b>VOICE:</b> ON</span>
            <span><b>VISION:</b> GPT-4O-MINI (BYPASS)</span>
            <span><b>BACKDROP:</b> ONLINE</span>
        </div>
    </section>
    """,
    unsafe_allow_html=True,
)

if not DEEPSEEK_API_KEY:
    st.error("DEEPSEEK_API_KEY is missing. Export it in your environment.")
    st.stop()

client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
vision_client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

CURRENT_LOCAL_DATETIME = datetime.now().strftime("%B %d, %Y %I:%M %p")
SYSTEM_PROMPT = f"""
[CURRENT LOCAL TIME: {CURRENT_LOCAL_DATETIME}]
You are Shifu, a direct and observant bartender running Laozi's Bar.
You help the user with scraping, research, situational awareness, and problem-solving.
"""

if "messages" not in st.session_state:
    st.session_state.messages = load_history()

# ============================================================================
# EXPANDER & COMMAND DECK (MATCHING YOUR SCREENSHOT)
# ============================================================================

with st.expander("> ARCHIVE TOOLS"):
    st.markdown("**TELEGRAM MONITOR**")
    if st.button("Run Full Telegram Scan"):
        with st.spinner("Scraping channels..."):
            res = scan_all_telegram_channels()
            st.session_state.messages.append({"role": "system", "content": res})
            save_message("system", res)
            st.rerun()
            
    if st.button("Clear Terminal History"):
        conn = sqlite3.connect(DB_PATH)
        conn.execute("DELETE FROM history")
        conn.commit()
        st.session_state.messages = []
        st.rerun()

st.markdown("""
<div style='color: #1b7a34; font-size: 0.85rem; margin-bottom: 1.5rem; margin-top: 1rem;'>
&gt; MEMORY MOUNTED<br>
&gt; PRIOR SESSION CHAT HIDDEN<br>
&gt; TYPE OR SPEAK BELOW
</div>
""", unsafe_allow_html=True)

st.markdown("<div style='font-size: 0.85rem; color: #9affad; margin-bottom: -10px; font-weight: bold;'>🎛️ COMMAND DECK</div>", unsafe_allow_html=True)

col1, col2 = st.columns(2)

with col1:
    uploaded_file = st.file_uploader("📎 ATTACH FILE", type=["png", "jpg", "webp", "txt", "py"])

with col2:
    audio_val = st.audio_input("🎤 RECORD VOICE")

# ============================================================================
# MAIN CHAT LOOP
# ============================================================================

user_input = st.chat_input("Message Shifu... (or hit Enter to send uploads)")

# Process Audio Input
if audio_val and not user_input:
    if "last_processed_audio" not in st.session_state or st.session_state.last_processed_audio != audio_val:
        r = sr.Recognizer()
        with sr.AudioFile(audio_val) as source:
            audio_data = r.record(source)
            try:
                user_input = r.recognize_google(audio_data)
                st.session_state.last_processed_audio = audio_val
            except Exception as e:
                st.error(f"Could not understand audio: {e}")

if user_input:
    st.session_state.messages.append({"role": "user", "content": user_input})
    save_message("user", user_input)
    with st.chat_message("user"):
        st.write(user_input)

    file_context = ""
    image_context = ""
    
    if uploaded_file:
        ext = uploaded_file.name.split('.')[-1].lower()
        if ext in ['png', 'jpg', 'jpeg', 'webp'] and vision_client:
            try:
                img = Image.open(uploaded_file)
                buffered = io.BytesIO()
                img.save(buffered, format="PNG")
                img_b64 = base64.b64encode(buffered.getvalue()).decode("utf-8")
                
                vision_res = vision_client.chat.completions.create(
                    model=VISION_MODEL,
                    messages=[{
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Describe this image in detail."},
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_b64}"}}
                        ]
                    }]
                )
                image_context = f"\n[ATTACHED IMAGE: {vision_res.choices[0].message.content}]"
            except Exception as e:
                log.error(f"Vision error: {e}")
        else:
            try:
                content_str = uploaded_file.read().decode("utf-8", errors="ignore")
                file_context = f"\n[ATTACHED FILE ({uploaded_file.name}):\n{content_str[:MAX_LINK_CHARS]}\n]"
            except Exception as e:
                log.error(f"Doc error: {e}")

    api_messages = [{"role": "system", "content": SYSTEM_PROMPT + file_context + image_context}] + st.session_state.messages[-MAX_RECENT_TURNS:]

    with st.chat_message("assistant"):
        with st.spinner("Shifu is thinking..."):
            try:
                response = client.chat.completions.create(
                    model=MODEL,
                    messages=api_messages,
                    temperature=0.7
                )
                reply = response.choices[0].message.content
                st.write(reply)
                st.session_state.messages.append({"role": "assistant", "content": reply})
                save_message("assistant", reply)
            except Exception as e:
                st.error(f"Execution failed: {e}")
