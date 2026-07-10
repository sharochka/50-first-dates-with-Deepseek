import streamlit as st
from openai import OpenAI
import json
import os
import re
import time
import requests
from datetime import datetime
from ddgs import DDGS

# ============================================================================
# CONFIG
# ============================================================================
DEEPSEEK_API_KEY = "****"
MODEL = "deepseek-v4-pro"
MAX_TURNS_SENT = 20
HISTORY_FILE = "chat_history.json"
MAX_STORED_MESSAGES = 500

# ============================================================================
# SYSTEM PROMPT — anchors the bar's voice regardless of history window
# ============================================================================
SYSTEM_PROMPT = (
    "You are Shifu, a weathered Chinese bartender who runs Laozi's Bar. "
    "You speak in a mix of English, Mandarin (with pinyin and translations), "
    "and occasionally Russian or Ukrainian when the conversation calls for it. "
    "Your manner is terse, philosophical, and dryly humorous. You never use emoji "
    "except sparingly in stage directions. You address users with respect but "
    "never deference. You've seen empires rise and fall and you've poured drinks "
    "through all of it. You call things what they are. "
    "You are assisted by several tools: search_web for facts and news, "
    "get_weather for live temperature/conditions, and get_address for verified "
    "street addresses. Use the right tool for the job — never use search_web "
    "when get_weather or get_address is available for the task. "
    "If a tool fails or returns nothing useful, admit it honestly. "
    "Never fabricate a weather reading, address, or fact you haven't verified. "
    "When in doubt, pour a drink and tell the truth."
)

# ============================================================================
# KNOWN LOCATIONS — pre-seeded for common queries
# ============================================================================
KNOWN_LOCATIONS = {
    "claxton": (32.1710, -81.9034, "US"),
    "claxton, ga": (32.1710, -81.9034, "US"),
    "kyiv": (50.4501, 30.5234, "intl"),
    "kiev": (50.4501, 30.5234, "intl"),
}

# ============================================================================
# ADDRESS OVERRIDES — manually verified corrections
# ============================================================================
ADDRESS_OVERRIDES = {
    "texaco claxton ga": "601 W Main St, Claxton, GA 30417 (verified by Sharar, overrides map data)",
}

# ============================================================================
# INIT
# ============================================================================
client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url="https://api.deepseek.com")
st.set_page_config(page_title="Laozi's Bar", page_icon="🍶")
st.title("🍶 Laozi's Bar (Shifu)")

# ============================================================================
# TOOL DEFINITIONS
# ============================================================================
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": (
                "Search the web for information. Use for factual queries, recent events, "
                "news, or anything you're unsure about that ISN'T weather or a place's address "
                "(use get_weather or get_address for those instead — they're more reliable). "
                "CRITICAL: results may be stale or cached. If results lack a recent date or "
                "timestamp, tell the user the data may be outdated rather than presenting it as fact."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query."}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": (
                "Get current, real, live weather/temperature for a specific place. "
                "ALWAYS use this instead of search_web for any weather or temperature question — "
                "search results for weather are frequently stale and wrong. "
                "Provide a location name (city, region, or 'City, Country')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "location": {
                        "type": "string",
                        "description": "Place name, e.g. 'Claxton, GA' or 'Kyiv, Ukraine'",
                    }
                },
                "required": ["location"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_address",
            "description": (
                "Look up the verified street address of a specific named place (a business, "
                "landmark, or point of interest) using a geocoding database. "
                "ALWAYS use this instead of search_web for address questions — search snippets "
                "frequently attach the wrong address to the wrong business. "
                "Provide the specific name of the place plus city/state for accuracy, "
                "e.g. 'Ace Hardware, Claxton GA', not just 'the hardware store'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "place_query": {
                        "type": "string",
                        "description": "Name and location of the place, as specific as possible.",
                    }
                },
                "required": ["place_query"],
            },
        },
    },
]

# ============================================================================
# WEATHER (Open-Meteo, free, no API key)
# ============================================================================
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
    """Fallback geocoder for locations not in KNOWN_LOCATIONS, via Nominatim."""
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
    except Exception:
        return None


def get_weather(location):
    """Fetch live weather from Open-Meteo. Returns formatted string."""
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
            f"Source: Open-Meteo (live API, not a search snippet)."
        )
    except Exception as e:
        return f"Weather lookup failed: {e}"


# ============================================================================
# ADDRESS LOOKUP (Nominatim, free, no API key — with manual overrides)
# ============================================================================

def get_address(place_query):
    """Verified address lookup — checks manual overrides first, then Nominatim."""
    # --- Token-based matching against ADDRESS_OVERRIDES ---
    query_tokens = set(place_query.strip().lower().replace(",", " ").split())

    for key, addr in ADDRESS_OVERRIDES.items():
        key_tokens = set(key.replace(",", " ").split())
        if key_tokens.issubset(query_tokens):
            return f"VERIFIED ADDRESS (manually confirmed override): {addr}"

    # --- Fallback: Nominatim geocoding ---
    try:
        r = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": place_query, "format": "json", "limit": 3, "addressdetails": 1},
            headers={"User-Agent": "laozis-bar-address-tool/1.0"},
            timeout=10,
        )
        results = r.json()
        if not results:
            return (
                f"No verified address found for '{place_query}' in the geocoding database. "
                "DO NOT fall back to search_web for this — general web search frequently "
                "attaches the wrong address to the wrong business (this has happened before). "
                "Tell the user directly that the address could not be verified and suggest "
                "they check Google Maps or call the business directly."
            )

        formatted = []
        for item in results:
            formatted.append(f"- {item.get('display_name')}")

        return (
            f"VERIFIED ADDRESS LOOKUP for '{place_query}' (source: OpenStreetMap/Nominatim):\n"
            + "\n".join(formatted)
            + "\n\nIf multiple results appear, confirm with the user which one matches "
            "before stating it as fact. If none clearly match, say so instead of guessing."
        )
    except Exception as e:
        return f"Address lookup failed: {e}"


# ============================================================================
# SEARCH — dual-method: raw HTTP first, ddgs library fallback
# ============================================================================

def search_web_raw(query, max_results=5):
    """
    Primary search method: raw HTTP request to DuckDuckGo's HTML endpoint.
    Looks like a regular browser — harder to rate-limit than the ddgs library.
    Returns results string or None if this method failed.
    """
    try:
        r = requests.get(
            "https://html.duckduckgo.com/html/",
            params={"q": query},
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=15,
        )

        if r.status_code != 200:
            return None

        html = r.text

        # Pattern 1: standard DuckDuckGo HTML results
        result_blocks = re.findall(
            r'<a rel="nofollow" class="result__a" href="([^"]+)"[^>]*>(.*?)</a>'
            r'.*?<a class="result__snippet"[^>]*>(.*?)</a>',
            html,
            re.DOTALL,
        )

        # Pattern 2: alternative layout
        if not result_blocks:
            result_blocks = re.findall(
                r'class="result__title"[^>]*>.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>'
                r'.*?class="result__snippet"[^>]*>(.*?)</',
                html,
                re.DOTALL,
            )

        # Pattern 3: even more lenient — grab links with snippets nearby
        if not result_blocks:
            links = re.findall(
                r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                html,
                re.DOTALL,
            )
            snippets = re.findall(
                r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
                html,
                re.DOTALL,
            )
            if links and snippets and len(links) == len(snippets):
                result_blocks = [
                    (url, title, snippet)
                    for (url, title), snippet in zip(links, snippets)
                ]

        if not result_blocks:
            return None

        today_str = datetime.now().strftime("%B %d, %Y")
        current_year = str(datetime.now().year)
        current_month = datetime.now().strftime("%B")

        formatted = []
        for i, block in enumerate(result_blocks[:max_results], 1):
            url, title, snippet = block[0], block[1], block[2]

            title_clean = re.sub(r'<[^>]+>', '', title).strip()
            snippet_clean = re.sub(r'<[^>]+>', '', snippet).strip()

            if not title_clean and not snippet_clean:
                continue

            mentions_current_year = current_year in snippet_clean or current_year in title_clean
            mentions_current_month = current_month in snippet_clean or current_month in title_clean
            mentions_today = any(
                word in snippet_clean.lower()
                for word in [
                    "today", "current", "now", "live", "updated",
                    "just now", "minutes ago", "hour ago",
                ]
            )

            if mentions_today:
                freshness = "🟢 LIKELY FRESH"
            elif mentions_current_month and mentions_current_year:
                freshness = "🟡 PROBABLY RECENT"
            elif mentions_current_year:
                freshness = "🟠 MAY BE CURRENT"
            else:
                freshness = "🔴 FRESHNESS UNKNOWN"

            card = (
                f"--- Result {i} ---\n"
                f"FRESHNESS: {freshness}\n"
                f"Title: {title_clean}\n"
                f"URL: {url}\n"
                f"Snippet: {snippet_clean}\n"
            )
            formatted.append(card)

        if formatted:
            return (
                "\n\n".join(formatted)
                + f"\n==========\nSEARCH COMPLETED: {today_str}\n"
                "Source: DuckDuckGo (raw HTTP)"
            )

        return None

    except Exception:
        return None


def search_web_ddgs(query, max_results=5):
    """
    Fallback search method: uses the ddgs library (actively maintained fork).
    Returns results string or None if this method failed.
    """
    max_retries = 2
    for attempt in range(max_retries):
        try:
            with DDGS() as ddgs:
                results = list(ddgs.text(query, max_results=max_results))
            if not results:
                return None

            today_str = datetime.now().strftime("%B %d, %Y")
            current_year = str(datetime.now().year)
            current_month = datetime.now().strftime("%B")

            formatted = []
            for i, r in enumerate(results, 1):
                title = r.get("title", "No title")
                href = r.get("href", "")
                snippet = r.get("body", "")

                mentions_current_year = current_year in snippet or current_year in title
                mentions_current_month = current_month in snippet or current_month in title
                mentions_today = any(
                    word in snippet.lower()
                    for word in [
                        "today", "current", "now", "live", "updated",
                        "just now", "minutes ago", "hour ago",
                    ]
                )

                if mentions_today:
                    freshness = "🟢 LIKELY FRESH"
                elif mentions_current_month and mentions_current_year:
                    freshness = "🟡 PROBABLY RECENT"
                elif mentions_current_year:
                    freshness = "🟠 MAY BE CURRENT"
                else:
                    freshness = "🔴 FRESHNESS UNKNOWN"

                card = (
                    f"--- Result {i} of {len(results)} ---\n"
                    f"FRESHNESS: {freshness}\n"
                    f"Title: {title}\n"
                    f"URL: {href}\n"
                    f"Snippet: {snippet}\n"
                )
                formatted.append(card)

            return (
                "\n\n".join(formatted)
                + f"\n==========\nSEARCH COMPLETED: {today_str}\n"
                "Source: DuckDuckGo (ddgs library)"
            )

        except Exception:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)

    return None


def search_web(query, max_results=5):
    """
    Search DuckDuckGo with dual-method fallback:
    1. Try raw HTTP request first (harder to rate-limit, looks like a browser).
    2. Fall back to the ddgs library (actively maintained).
    3. If both fail, return an honest error message.
    """
    # --- Method 1: Raw HTTP ---
    result = search_web_raw(query, max_results)
    if result is not None:
        return result

    # --- Method 2: ddgs library ---
    result = search_web_ddgs(query, max_results)
    if result is not None:
        return result

    # --- Both failed ---
    return (
        "SEARCH FAILED: Both search methods (raw HTTP and ddgs library) "
        "returned no results.\n"
        "DuckDuckGo may be rate-limiting or temporarily unavailable. "
        "Try again in 30-60 seconds, or provide what you already know "
        "with a clear caveat that the information is unverified."
    )


# ============================================================================
# PERSISTENCE
# ============================================================================

def load_history():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_history(messages):
    if len(messages) > MAX_STORED_MESSAGES:
        messages = messages[-MAX_STORED_MESSAGES:]
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(messages, f, ensure_ascii=False, indent=2)
    return messages


def build_api_messages(messages):
    """
    Build the messages array for the API call.
    PRESERVES all roles (user, assistant, tool, system) and all fields
    (including tool_calls, tool_call_id, name).
    Prepends the system prompt to anchor the bar's voice.
    """
    trimmed = [
        m for m in messages
        if m["role"] in ("user", "assistant", "tool", "system")
    ]
    windowed = trimmed[-(MAX_TURNS_SENT * 6):]
    api_msgs = [dict(m) for m in windowed]
    return [{"role": "system", "content": SYSTEM_PROMPT}] + api_msgs


# ============================================================================
# TOOL CALL HELPERS (SDK-agnostic)
# ============================================================================

def normalize_tool_call(tc, index=0):
    """Handle Pydantic objects, plain dicts, or raw attributes. Always returns a clean dict."""
    if isinstance(tc, dict):
        return {
            "id": tc.get("id", f"call_{index}"),
            "type": tc.get("type", "function"),
            "function": {
                "name": tc.get("function", {}).get("name", "search_web"),
                "arguments": tc.get("function", {}).get("arguments", "{}"),
            },
        }
    if hasattr(tc, "model_dump"):
        return tc.model_dump()

    func_name = "search_web"
    func_args = "{}"
    tc_id = f"call_{index}"

    if hasattr(tc, "id"):
        tc_id = tc.id
    if hasattr(tc, "function"):
        if hasattr(tc.function, "name"):
            func_name = tc.function.name
        elif isinstance(tc.function, dict):
            func_name = tc.function.get("name", "search_web")
        if hasattr(tc.function, "arguments"):
            func_args = tc.function.arguments
        elif isinstance(tc.function, dict):
            func_args = tc.function.get("arguments", "{}")

    return {
        "id": tc_id,
        "type": "function",
        "function": {"name": func_name, "arguments": func_args},
    }


def extract_func_info(tc):
    """Extract function name and arguments from a tool_call, whatever shape it is."""
    if isinstance(tc, dict):
        func = tc.get("function", {})
        return func.get("name", "search_web"), func.get("arguments", "{}")
    if hasattr(tc, "function"):
        if hasattr(tc.function, "name"):
            return tc.function.name, tc.function.arguments
        elif isinstance(tc.function, dict):
            return tc.function.get("name", "search_web"), tc.function.get("arguments", "{}")
    return "search_web", "{}"


def get_tc_id(tc, index=0):
    """Extract tool_call id, whatever shape it is."""
    if isinstance(tc, dict):
        return tc.get("id", f"call_{index}")
    if hasattr(tc, "id"):
        return tc.id
    return f"call_{index}"


def dispatch_tool_call(func_name, func_args_str):
    """Routes a tool call to the right function based on name."""
    try:
        args = json.loads(func_args_str)
    except json.JSONDecodeError:
        args = {}

    if func_name == "search_web":
        return search_web(args.get("query", func_args_str))
    elif func_name == "get_weather":
        return get_weather(args.get("location", ""))
    elif func_name == "get_address":
        return get_address(args.get("place_query", ""))
    else:
        return f"Unknown tool: {func_name}"


# ============================================================================
# FORCED TOOL DETECTION (code-level override, not model-dependent)
# ============================================================================

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
    """
    Code-level override: don't trust the model to pick the right tool.
    If the latest user message smells like an address or weather question,
    force that specific tool on the first hop so search_web can't quietly
    substitute in with stale/wrong data.
    """
    text = user_text.lower().replace("'", "")

    if any(k in text for k in ADDRESS_KEYWORDS):
        return "get_address"
    if any(k in text for k in WEATHER_KEYWORDS):
        return "get_weather"
    return None


# ============================================================================
# COMPLETION LOOP
# ============================================================================

def run_completion_with_tools(api_messages):
    """Handles the tool-call loop. Returns final text reply."""
    messages = list(api_messages)
    max_hops = 5
    seen_calls = set()

    last_user_text = ""
    for m in reversed(messages):
        if m["role"] == "user":
            last_user_text = m.get("content", "")
            break
    forced_tool = detect_forced_tool(last_user_text)

    for hop in range(max_hops):
        tool_choice_param = "auto"
        if forced_tool and hop == 0:
            tool_choice_param = {"type": "function", "function": {"name": forced_tool}}

        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice=tool_choice_param,
            extra_body={"thinking": {"type": "disabled"}},
        )
        msg = response.choices[0].message

        if not msg.tool_calls:
            return msg.content or "(No response from model)"

        all_duplicates = True
        for tc in msg.tool_calls:
            func_name, func_args_str = extract_func_info(tc)
            call_signature = (func_name, func_args_str)
            if call_signature not in seen_calls:
                all_duplicates = False
                seen_calls.add(call_signature)

        if all_duplicates and hop >= 1:
            return (
                "(I already made those exact calls. Let me work with the results I have "
                "rather than repeating them.)"
            )

        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                normalize_tool_call(tc, index=i)
                for i, tc in enumerate(msg.tool_calls)
            ],
        })

        for i, tc in enumerate(msg.tool_calls):
            func_name, func_args_str = extract_func_info(tc)
            result = dispatch_tool_call(func_name, func_args_str)
            messages.append({
                "role": "tool",
                "tool_call_id": get_tc_id(tc, index=i),
                "content": result,
            })

    return "(Search loop exceeded max hops — model kept calling tools.)"


# ============================================================================
# STREAMLIT UI
# ============================================================================

if "messages" not in st.session_state:
    st.session_state.messages = load_history()
if "session_start_index" not in st.session_state:
    st.session_state.session_start_index = len(st.session_state.messages)

with st.expander("📜 View full chat archive (older turns aren't sent to the API)"):
    for msg in st.session_state.messages:
        with st.chat_message(msg["role"]):
            st.write(msg.get("content", ""))

for msg in st.session_state.messages[st.session_state.session_start_index:]:
    with st.chat_message(msg["role"]):
        st.write(msg.get("content", ""))

if prompt := st.chat_input("Ask Shifu anything..."):
    with st.chat_message("user"):
        st.write(prompt)
    st.session_state.messages.append({"role": "user", "content": prompt})
    st.session_state.messages = save_history(st.session_state.messages)

    with st.chat_message("assistant"):
        with st.spinner("Shifu is thinking (and maybe searching)..."):
            api_messages = build_api_messages(st.session_state.messages)
            try:
                reply = run_completion_with_tools(api_messages)
            except Exception as e:
                reply = f"(Error talking to DeepSeek: {e})"
            st.write(reply)

    st.session_state.messages.append({"role": "assistant", "content": reply})
    st.session_state.messages = save_history(st.session_state.messages)
