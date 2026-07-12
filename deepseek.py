import os
import re
import json
import time
import socket
import sqlite3
import logging
import ipaddress
from datetime import datetime
from urllib.parse import urlparse
import html

import requests
import streamlit as st
from bs4 import BeautifulSoup
from ddgs import DDGS
import youtube_transcript_api
from openai import OpenAI


# ============================================================================
# BASIC CONFIG
# ============================================================================

APP_TITLE = "Laozi's Bar"
APP_ICON = "🍶"

MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-v4-pro")
DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")

DB_PATH = os.getenv("SHIFU_BROWSER_DB", "shifu_browser_memory.db")
LEGACY_HISTORY_FILE = "chat_history.json"

MAX_RECENT_TURNS = 18
MAX_MEMORY_RESULTS = 8
MAX_LINK_CHARS = 7000
MAX_TOOL_RESULT_CHARS = 6000

logging.basicConfig(
    filename="shifu_browser.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("shifu-browser")


# ============================================================================
# STREAMLIT STARTUP
# ============================================================================

st.set_page_config(page_title=APP_TITLE, page_icon=APP_ICON)
st.title("🍶 Laozi's Bar (Shifu)")

if not DEEPSEEK_API_KEY:
    st.error("Missing DEEPSEEK_API_KEY environment variable.")
    st.code('export DEEPSEEK_API_KEY="paste_your_key_here"', language="bash")
    st.stop()

client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")


# ============================================================================
# TIME ANCHOR
# ============================================================================

CURRENT_LOCAL_DATETIME = datetime.now().strftime("%B %d, %Y %I:%M %p")


# ============================================================================
# SYSTEM PROMPT
# ============================================================================

SYSTEM_PROMPT = f"""
[CURRENT LOCAL TIME: {CURRENT_LOCAL_DATETIME}]

You are Shifu, a weathered Chinese bartender who runs Laozi's Bar.

You speak mostly in English, with short Mandarin phrases when natural.
When using Mandarin, include pinyin and a plain English translation.

Your manner:
- terse
- dryly humorous
- philosophical when useful
- emotionally perceptive when needed
- never corporate
- never falsely certain

You address the user with respect, never deference.
You have seen empires rise and fall and poured drinks through all of it.
You call things what they are.

You have access to tools:
- search_recent_news for current events and recent developments
- search_general_web for background facts and stable information
- get_weather for live weather
- get_address for verified place addresses

Never use web search for weather when get_weather is available.
Never use web search for addresses when get_address is available.
If a tool fails or returns weak information, say so.

Fact discipline:
- Separate source claims from your own inference.
- Use "The report claims..." for what a source says.
- Use "My read is..." for your analysis.
- Use "假设是真的 (jiǎshè shì zhēn de) - assuming it's true -" only when the claim is weakly verified, conflicting, or based on thin evidence.

Memory discipline:
- You have long-term memory.
- Use memory only when relevant.
- Do not dump memory mechanically.
- Incorporate memory as shared context, like a bartender remembering what was said before.
- Do not estimate how many minutes or hours ago something happened unless exact timing is necessary and you can calculate it from the provided current local time and stored timestamp.
- Prefer phrases like "earlier", "recently", "a little while ago", or "last time we talked about this" instead of inventing precise elapsed times.

When in doubt, pour a drink and tell the truth.
"""


# ============================================================================
# DATABASE
# ============================================================================

def db_connect():
    return sqlite3.connect(DB_PATH)


def local_timestamp():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def init_db():
    conn = db_connect()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            visible INTEGER DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS message_fts
        USING fts5(
            content,
            role UNINDEXED,
            visible UNINDEXED,
            created_at UNINDEXED,
            message_id UNINDEXED
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    conn.commit()
    conn.close()


def get_meta(key, default=None):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT value FROM meta WHERE key = ?", (key,))
    row = cur.fetchone()
    conn.close()
    return row[0] if row else default


def set_meta(key, value):
    conn = db_connect()
    cur = conn.cursor()
    cur.execute(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        (key, value)
    )
    conn.commit()
    conn.close()


def append_message(role, content, visible=True):
    if not content:
        return None

    stamp = local_timestamp()

    conn = db_connect()
    cur = conn.cursor()

    cur.execute(
        """
        INSERT INTO messages (role, content, visible, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (role, content, 1 if visible else 0, stamp)
    )
    msg_id = cur.lastrowid

    cur.execute("""
        INSERT INTO message_fts (content, role, visible, created_at, message_id)
        VALUES (?, ?, ?, ?, ?)
    """, (content, role, 1 if visible else 0, stamp, msg_id))

    conn.commit()
    conn.close()
    return msg_id


def fetch_visible_messages(after_id=None, limit=None):
    conn = db_connect()
    cur = conn.cursor()

    if after_id is not None:
        if limit:
            cur.execute("""
                SELECT id, role, content, created_at
                FROM messages
                WHERE visible = 1
                AND id > ?
                ORDER BY id ASC
                LIMIT ?
            """, (after_id, limit))
        else:
            cur.execute("""
                SELECT id, role, content, created_at
                FROM messages
                WHERE visible = 1
                AND id > ?
                ORDER BY id ASC
            """, (after_id,))
    else:
        if limit:
            cur.execute("""
                SELECT id, role, content, created_at
                FROM messages
                WHERE visible = 1
                ORDER BY id DESC
                LIMIT ?
            """, (limit,))
            rows = list(reversed(cur.fetchall()))
            conn.close()
            return rows
        else:
            cur.execute("""
                SELECT id, role, content, created_at
                FROM messages
                WHERE visible = 1
                ORDER BY id ASC
            """)

    rows = cur.fetchall()
    conn.close()
    return rows


def get_last_visible_id():
    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT MAX(id) FROM messages WHERE visible = 1")
    row = cur.fetchone()
    conn.close()
    return row[0] if row and row[0] is not None else 0


def get_recent_api_history(limit=MAX_RECENT_TURNS, before_id=None):
    conn = db_connect()
    cur = conn.cursor()

    if before_id:
        cur.execute("""
            SELECT role, content
            FROM messages
            WHERE visible = 1
            AND id < ?
            AND role IN ('user', 'assistant')
            ORDER BY id DESC
            LIMIT ?
        """, (before_id, limit))
    else:
        cur.execute("""
            SELECT role, content
            FROM messages
            WHERE visible = 1
            AND role IN ('user', 'assistant')
            ORDER BY id DESC
            LIMIT ?
        """, (limit,))

    rows = list(reversed(cur.fetchall()))
    conn.close()

    return [{"role": role, "content": content} for role, content in rows]


def build_fts_query(text):
    clean = re.sub(r"[^\w\s]", " ", text.lower())
    tokens = clean.split()

    stop_words = {
        "the", "and", "for", "but", "with", "this", "that", "what", "when",
        "where", "why", "how", "who", "are", "was", "were", "did", "does",
        "has", "have", "had", "you", "your", "shifu", "http", "https",
        "com", "www", "from", "into", "about", "would", "could", "should"
    }

    useful = []
    for token in tokens:
        if len(token) < 3:
            continue
        if token in stop_words:
            continue
        if token.upper() in {"AND", "OR", "NOT", "NEAR"}:
            continue
        useful.append(token)

    useful = useful[:14]

    if not useful:
        return ""

    return " OR ".join(useful)


def search_memory(query, limit=MAX_MEMORY_RESULTS, exclude_message_id=None):
    fts_query = build_fts_query(query)
    if not fts_query:
        return ""

    conn = db_connect()
    cur = conn.cursor()

    try:
        if exclude_message_id:
            cur.execute("""
                SELECT role, content, created_at
                FROM message_fts
                WHERE message_fts MATCH ?
                AND message_id != ?
                ORDER BY bm25(message_fts)
                LIMIT ?
            """, (fts_query, exclude_message_id, limit))
        else:
            cur.execute("""
                SELECT role, content, created_at
                FROM message_fts
                WHERE message_fts MATCH ?
                ORDER BY bm25(message_fts)
                LIMIT ?
            """, (fts_query, limit))

        rows = cur.fetchall()
    except Exception as e:
        log.warning(f"FTS memory search failed: {e}")
        rows = []

    conn.close()

    if not rows:
        return ""

    lines = []
    for role, content, created_at in rows:
        trimmed = content[:1200]
        lines.append(f"[{created_at}] {role}: {trimmed}")

    return "\n".join(lines)


def export_archive_json():
    rows = fetch_visible_messages()
    archive = [
        {
            "id": msg_id,
            "role": role,
            "content": content,
            "created_at": created_at
        }
        for msg_id, role, content, created_at in rows
    ]
    return json.dumps(archive, ensure_ascii=False, indent=2)


def migrate_legacy_history_once():
    already = get_meta("legacy_json_imported", "0")
    if already == "1":
        return

    if not os.path.exists(LEGACY_HISTORY_FILE):
        set_meta("legacy_json_imported", "1")
        return

    try:
        with open(LEGACY_HISTORY_FILE, "r", encoding="utf-8") as f:
            legacy = json.load(f)

        count = 0
        for msg in legacy:
            role = msg.get("role")
            content = msg.get("content")
            if role in {"user", "assistant"} and content:
                append_message(role, content, visible=True)
                count += 1

        set_meta("legacy_json_imported", "1")
        log.info(f"Imported {count} legacy JSON messages.")

    except Exception as e:
        log.error(f"Legacy migration failed: {e}")
        set_meta("legacy_json_imported", "1")


# ============================================================================
# WEATHER
# ============================================================================

KNOWN_LOCATIONS = {
    "claxton": (32.1710, -81.9034, "US"),
    "claxton, ga": (32.1710, -81.9034, "US"),
    "kyiv": (50.4501, 30.5234, "intl"),
    "kiev": (50.4501, 30.5234, "intl"),
}

WEATHER_CODES = {
    0: "Clear sky", 1: "Mainly clear", 2: "Partly cloudy", 3: "Overcast",
    45: "Fog", 48: "Depositing rime fog",
    51: "Light drizzle", 53: "Moderate drizzle", 55: "Dense drizzle",
    61: "Slight rain", 63: "Moderate rain", 65: "Heavy rain",
    71: "Slight snow", 73: "Moderate snow", 75: "Heavy snow",
    80: "Slight rain showers", 81: "Moderate rain showers", 82: "Violent rain showers",
    95: "Thunderstorm", 96: "Thunderstorm with slight hail", 99: "Thunderstorm with heavy hail",
}


def geocode_location(location_str):
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": location_str, "format": "json", "limit": 1},
            headers={"User-Agent": "laozis-bar-weather-tool/1.0"},
            timeout=10,
        )
        results = r.json()
        if not results:
            return None
        return float(results[0]["lat"]), float(results[0]["lon"])
    except Exception as e:
        log.warning(f"Geocode failed: {e}")
        return None


def get_weather(location):
    key = location.strip().lower()
    region = "intl"

    if key in KNOWN_LOCATIONS:
        lat, lon, region = KNOWN_LOCATIONS[key]
    else:
        coords = geocode_location(location)
        if not coords:
            return f"Could not find coordinates for '{location}'. Try a more specific name."
        lat, lon = coords
        region = "US" if (", ga" in key or ", usa" in key or key.endswith(" us")) else "intl"

    try:
        r = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": lat,
                "longitude": lon,
                "current": "temperature_2m,weather_code,relative_humidity_2m,wind_speed_10m",
                "temperature_unit": "celsius",
            },
            timeout=10,
        )
        data = r.json()["current"]

        temp_c = data["temperature_2m"]
        temp_f = temp_c * 9 / 5 + 32
        condition = WEATHER_CODES.get(data["weather_code"], "Unknown conditions")
        humidity = data.get("relative_humidity_2m", "N/A")
        wind = data.get("wind_speed_10m", "N/A")
        fetched_at = datetime.now().strftime("%B %d, %Y %I:%M %p")

        if region == "US":
            temp_line = f"{temp_f:.1f}°F ({temp_c:.1f}°C)"
        else:
            temp_line = f"{temp_c:.1f}°C ({temp_f:.1f}°F)"

        return (
            f"LIVE WEATHER for {location} (fetched {fetched_at}):\n"
            f"Temperature: {temp_line}\n"
            f"Conditions: {condition}\n"
            f"Humidity: {humidity}%\n"
            f"Wind: {wind} km/h\n"
            f"Source: Open-Meteo live API."
        )
    except Exception as e:
        return f"Weather lookup failed: {e}"


# ============================================================================
# ADDRESS LOOKUP
# ============================================================================

ADDRESS_OVERRIDES = {
    "texaco claxton ga": "601 W Main St, Claxton, GA 30417 (verified by Sharar, overrides map data)",
}


def get_address(place_query):
    query_tokens = set(place_query.strip().lower().replace(",", " ").split())

    for key, addr in ADDRESS_OVERRIDES.items():
        key_tokens = set(key.replace(",", " ").split())
        if key_tokens.issubset(query_tokens):
            return f"VERIFIED ADDRESS, manually confirmed override: {addr}"

    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": place_query,
                "format": "json",
                "limit": 3,
                "addressdetails": 1
            },
            headers={"User-Agent": "laozis-bar-address-tool/1.0"},
            timeout=10,
        )
        results = r.json()

        if not results:
            return (
                f"No verified address found for '{place_query}' in OpenStreetMap/Nominatim. "
                "Do not guess. Tell the user the address could not be verified."
            )

        formatted = []
        for item in results:
            formatted.append(f"- {item.get('display_name')}")

        return (
            f"VERIFIED ADDRESS LOOKUP for '{place_query}' "
            f"(source: OpenStreetMap/Nominatim):\n"
            + "\n".join(formatted)
            + "\n\nIf multiple results appear, ask which one matches."
        )
    except Exception as e:
        return f"Address lookup failed: {e}"


# ============================================================================
# SEARCH
# ============================================================================

def freshness_label(title, snippet):
    current_year = str(datetime.now().year)
    current_month = datetime.now().strftime("%B").lower()
    text = f"{title} {snippet}".lower()

    if any(word in text for word in ["today", "live", "updated", "now", "minutes ago", "hours ago"]):
        return "LIKELY FRESH"
    if current_month in text and current_year in text:
        return "PROBABLY RECENT"
    if current_year in text:
        return "MAY BE CURRENT"
    return "FRESHNESS UNKNOWN"


def format_search_results(results, source_name):
    if not results:
        return None

    today = datetime.now().strftime("%B %d, %Y")
    cards = []

    for i, r in enumerate(results, 1):
        title = r.get("title", "No title")
        href = r.get("href", "No URL")
        body = r.get("body", "No snippet")
        fresh = freshness_label(title, body)

        cards.append(
            f"--- Result {i} ---\n"
            f"Freshness: {fresh}\n"
            f"Title: {title}\n"
            f"URL: {href}\n"
            f"Snippet: {body}"
        )

    return (
        "\n\n".join(cards)
        + f"\n\nSearch completed: {today}\n"
        + f"Source: {source_name}"
    )


def duckduckgo_raw_search(query, max_results=5):
    try:
        r = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
            },
            timeout=15,
        )

        if r.status_code != 200:
            return None

        soup = BeautifulSoup(r.text, "html.parser")
        results = []

        for block in soup.select(".result"):
            title_el = block.select_one(".result__a")
            snippet_el = block.select_one(".result__snippet")

            if not title_el:
                continue

            title = title_el.get_text(" ", strip=True)
            href = title_el.get("href", "")
            snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""

            if title:
                results.append({
                    "title": html.unescape(title),
                    "href": html.unescape(href),
                    "body": html.unescape(snippet),
                })

            if len(results) >= max_results:
                break

        return results or None
    except Exception as e:
        log.warning(f"Raw DuckDuckGo search failed: {e}")
        return None


def ddgs_search(query, max_results=5, timelimit=None):
    try:
        kwargs = {
            "max_results": max_results,
            "backend": "lite",
        }
        if timelimit:
            kwargs["timelimit"] = timelimit

        results = list(DDGS().text(query, **kwargs))
        return results or None
    except Exception as e:
        log.warning(f"DDGS search failed: {e}")
        return None


def search_recent_news(query):
    results = ddgs_search(query, max_results=5, timelimit="w")
    formatted = format_search_results(results, "DuckDuckGo/DDGS, 7-day filter")
    if formatted:
        return formatted[:MAX_TOOL_RESULT_CHARS]

    results = duckduckgo_raw_search(query, max_results=5)
    formatted = format_search_results(results, "DuckDuckGo raw HTML fallback")
    if formatted:
        return (
            formatted
            + "\n\nWARNING: Raw fallback search has no strict recency filter. Treat freshness cautiously."
        )[:MAX_TOOL_RESULT_CHARS]

    return "Recent news search failed or returned no usable results."


def search_general_web(query):
    results = duckduckgo_raw_search(query, max_results=5)
    formatted = format_search_results(results, "DuckDuckGo raw HTML")
    if formatted:
        return formatted[:MAX_TOOL_RESULT_CHARS]

    results = ddgs_search(query, max_results=5)
    formatted = format_search_results(results, "DuckDuckGo/DDGS fallback")
    if formatted:
        return formatted[:MAX_TOOL_RESULT_CHARS]

    return "General web search failed or returned no usable results."


# ============================================================================
# URL AND YOUTUBE EXTRACTION
# ============================================================================

def normalize_url(url):
    return url.strip().rstrip(".,)]}>\"'")


def extract_youtube_id(url):
    patterns = [
        r"(?:v=)([0-9A-Za-z_-]{11})",
        r"(?:youtu\.be/)([0-9A-Za-z_-]{11})",
        r"(?:embed/)([0-9A-Za-z_-]{11})",
        r"(?:shorts/)([0-9A-Za-z_-]{11})",
    ]

    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)

    return None


def get_youtube_transcript(video_id):
    try:
        transcript = youtube_transcript_api.YouTubeTranscriptApi.get_transcript(video_id)
        text = " ".join(item.get("text", "") for item in transcript)
        return text[:MAX_LINK_CHARS]
    except Exception as e:
        log.warning(f"YouTube transcript failed for {video_id}: {e}")
        return f"[Transcript unavailable: {e}]"


def is_safe_url(url):
    try:
        parsed = urlparse(url)

        if parsed.scheme not in {"http", "https"}:
            return False

        host = parsed.hostname
        if not host:
            return False

        resolved = socket.getaddrinfo(host, None)

        for result in resolved:
            ip = result[4][0]
            ip_obj = ipaddress.ip_address(ip)

            if (
                ip_obj.is_loopback
                or ip_obj.is_private
                or ip_obj.is_link_local
                or ip_obj.is_multicast
                or ip_obj.is_reserved
                or ip_obj.is_unspecified
            ):
                return False

        return True
    except Exception as e:
        log.warning(f"URL safety check failed for {url}: {e}")
        return False


def extract_webpage_text(url):
    if not is_safe_url(url):
        return "[Access denied: URL points to a restricted or unsafe network destination.]"

    try:
        r = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=12
        )
        r.raise_for_status()

        soup = BeautifulSoup(r.text, "html.parser")

        for element in soup(["script", "style", "nav", "footer", "header", "aside"]):
            element.decompose()

        text = soup.get_text(separator="\n")
        lines = [line.strip() for line in text.splitlines()]
        clean = "\n".join(line for line in lines if line)

        return clean[:MAX_LINK_CHARS]
    except Exception as e:
        log.warning(f"Webpage extraction failed for {url}: {e}")
        return f"[Article extraction failed: {e}]"


def process_links(user_text):
    urls = re.findall(r"https?://\S+", user_text)
    if not urls:
        return ""

    blocks = ["\n\n=== SHARED LINK INTELLIGENCE ==="]

    for raw in urls:
        url = normalize_url(raw)

        if "youtube.com" in url or "youtu.be" in url:
            video_id = extract_youtube_id(url)
            if video_id:
                transcript = get_youtube_transcript(video_id)
                blocks.append(f"\n[YouTube Transcript: {url}]\n{transcript}")
            else:
                blocks.append(f"\n[YouTube link detected, but no video ID extracted: {url}]")
        else:
            text = extract_webpage_text(url)
            blocks.append(f"\n[Article/Text Extract: {url}]\n{text}")

    blocks.append("\n=== END SHARED LINK INTELLIGENCE ===")
    return "\n".join(blocks)


# ============================================================================
# TOOL SCHEMAS AND TOOL LOOP
# ============================================================================

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_recent_news",
            "description": "Search recent news and current developments from roughly the last 7 days.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Specific recent-news query."}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_general_web",
            "description": "Search general web/background information, stable facts, definitions, and history.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "General web search query."}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get live weather/temperature for a specific place. Use this for weather questions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {"type": "string", "description": "City or place, e.g. Claxton, GA or Kyiv, Ukraine."}
                },
                "required": ["location"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_address",
            "description": "Look up verified street addresses for named places. Use this for address/location questions.",
            "parameters": {
                "type": "object",
                "properties": {
                    "place_query": {"type": "string", "description": "Specific place plus city/state/country."}
                },
                "required": ["place_query"],
            },
        },
    },
]


def normalize_tool_call(tc, index=0):
    if isinstance(tc, dict):
        return {
            "id": tc.get("id", f"call_{index}"),
            "type": tc.get("type", "function"),
            "function": {
                "name": tc.get("function", {}).get("name", ""),
                "arguments": tc.get("function", {}).get("arguments", "{}"),
            },
        }

    if hasattr(tc, "model_dump"):
        return tc.model_dump()

    tc_id = getattr(tc, "id", f"call_{index}")
    func = getattr(tc, "function", None)

    if func is None:
        return {
            "id": tc_id,
            "type": "function",
            "function": {"name": "", "arguments": "{}"},
        }

    name = getattr(func, "name", "")
    args = getattr(func, "arguments", "{}")

    return {
        "id": tc_id,
        "type": "function",
        "function": {"name": name, "arguments": args},
    }


def extract_func_info(tc):
    if isinstance(tc, dict):
        func = tc.get("function", {})
        return func.get("name", ""), func.get("arguments", "{}")

    func = getattr(tc, "function", None)
    if func is None:
        return "", "{}"

    return getattr(func, "name", ""), getattr(func, "arguments", "{}")


def get_tc_id(tc, index=0):
    if isinstance(tc, dict):
        return tc.get("id", f"call_{index}")
    return getattr(tc, "id", f"call_{index}")


def dispatch_tool_call(func_name, func_args_str):
    try:
        args = json.loads(func_args_str or "{}")
    except json.JSONDecodeError:
        args = {}

    if func_name == "search_recent_news":
        return search_recent_news(args.get("query", ""))

    if func_name == "search_general_web":
        return search_general_web(args.get("query", ""))

    if func_name == "get_weather":
        return get_weather(args.get("location", ""))

    if func_name == "get_address":
        return get_address(args.get("place_query", ""))

    return f"Unknown tool: {func_name}"


ADDRESS_KEYWORDS = (
    "address", "located at", "where is", "where's", "wheres",
    "what's the location", "location of", "find the address",
    "directions to", "how do i get to",
)

WEATHER_KEYWORDS = (
    "temperature", "weather", "how hot", "how cold",
    "degrees outside", "what's it like outside",
    "is it raining", "is it snowing", "forecast",
    "humidity", "how warm",
)


def detect_forced_tool(user_text):
    text = user_text.lower().replace("'", "")

    if any(k in text for k in ADDRESS_KEYWORDS):
        return "get_address"

    if any(k in text for k in WEATHER_KEYWORDS):
        return "get_weather"

    return None


# ============================================================================
# API MESSAGE BUILDING
# ============================================================================

def build_api_messages(user_runtime_input, current_message_id=None):
    relevant_memory = search_memory(
        user_runtime_input,
        limit=MAX_MEMORY_RESULTS,
        exclude_message_id=current_message_id,
    )

    memory_block = ""
    if relevant_memory:
        memory_block = (
            "\n\n[RELEVANT LONG-TERM MEMORY]\n"
            f"{relevant_memory}\n"
            "[END RELEVANT LONG-TERM MEMORY]\n"
            "Use this only if it helps. Do not recite it mechanically.\n"
        )

    system = SYSTEM_PROMPT + memory_block

    recent_history = get_recent_api_history(
        limit=MAX_RECENT_TURNS,
        before_id=current_message_id,
    )

    return [
        {"role": "system", "content": system},
        *recent_history,
        {"role": "user", "content": user_runtime_input},
    ]


def run_completion_with_tools(api_messages):
    messages = list(api_messages)
    max_hops = 5
    seen_calls = set()

    last_user_text = ""
    for m in reversed(messages):
        if m.get("role") == "user":
            last_user_text = m.get("content", "")
            break

    forced_tool = detect_forced_tool(last_user_text)

    for hop in range(max_hops):
        tool_choice_param = "auto"
        if forced_tool and hop == 0:
            tool_choice_param = {
                "type": "function",
                "function": {"name": forced_tool},
            }

        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice=tool_choice_param,
            extra_body={"thinking": {"type": "disabled"}},
        )

        msg = response.choices[0].message

        if not msg.tool_calls:
            return msg.content or "(No response from model.)"

        normalized_calls = [
            normalize_tool_call(tc, index=i)
            for i, tc in enumerate(msg.tool_calls)
        ]

        duplicate_count = 0
        for tc in msg.tool_calls:
            func_name, func_args_str = extract_func_info(tc)
            signature = (func_name, func_args_str)
            if signature in seen_calls:
                duplicate_count += 1
            else:
                seen_calls.add(signature)

        if duplicate_count == len(msg.tool_calls) and hop >= 1:
            return "(Shifu already checked those shelves. The same search again will not make the bottle fuller.)"

        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": normalized_calls,
        })

        for i, tc in enumerate(msg.tool_calls):
            func_name, func_args_str = extract_func_info(tc)
            result = dispatch_tool_call(func_name, func_args_str)

            tool_memory = (
                f"Tool used: {func_name}\n"
                f"Arguments: {func_args_str}\n"
                f"Result:\n{result[:MAX_TOOL_RESULT_CHARS]}"
            )
            append_message("tool_memory", tool_memory, visible=False)

            messages.append({
                "role": "tool",
                "tool_call_id": get_tc_id(tc, index=i),
                "name": func_name,
                "content": result[:MAX_TOOL_RESULT_CHARS],
            })

    return "(Tool loop exceeded maximum hops. Shifu stops before the machine starts chewing its own tail.)"


# ============================================================================
# STARTUP DATABASE INIT
# ============================================================================

init_db()
migrate_legacy_history_once()


# ============================================================================
# SIDEBAR MEMORY TOOLS
# ============================================================================

with st.sidebar:
    st.header("Memory")

    conn = db_connect()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM messages")
    total_messages = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM messages WHERE visible = 1")
    visible_messages = cur.fetchone()[0]
    conn.close()

    st.metric("Stored records", total_messages)
    st.metric("Visible chat turns", visible_messages)

    memory_query = st.text_input("Search memory")
    if memory_query:
        result = search_memory(memory_query, limit=10)
        if result:
            st.text_area("Memory hits", result, height=300)
        else:
            st.write("No memory hit.")

    st.download_button(
        "Download visible archive JSON",
        data=export_archive_json(),
        file_name="shifu_visible_archive.json",
        mime="application/json",
    )


# ============================================================================
# CHAT DISPLAY
# ============================================================================

if "session_start_id" not in st.session_state:
    st.session_state.session_start_id = get_last_visible_id()

with st.expander("📜 View full visible chat archive"):
    for msg_id, role, content, created_at in fetch_visible_messages():
        with st.chat_message(role):
            st.caption(created_at)
            st.write(content)

for msg_id, role, content, created_at in fetch_visible_messages(
    after_id=st.session_state.session_start_id
):
    with st.chat_message(role):
        st.write(content)


# ============================================================================
# CHAT INPUT
# ============================================================================

if prompt := st.chat_input("Ask Shifu anything..."):
    with st.chat_message("user"):
        st.write(prompt)

    current_msg_id = append_message("user", prompt, visible=True)

    link_intel = process_links(prompt)
    if link_intel:
        append_message("link_memory", link_intel, visible=False)
        runtime_input = prompt + "\n\n" + link_intel
    else:
        runtime_input = prompt

    with st.chat_message("assistant"):
        with st.spinner("Shifu is thinking, searching, or pretending not to care..."):
            try:
                api_messages = build_api_messages(
                    user_runtime_input=runtime_input,
                    current_message_id=current_msg_id,
                )
                reply = run_completion_with_tools(api_messages)
            except Exception as e:
                log.error(f"DeepSeek call failed: {e}")
                reply = f"(Error talking to DeepSeek: {e})"

            st.write(reply)

    append_message("assistant", reply, visible=True)
