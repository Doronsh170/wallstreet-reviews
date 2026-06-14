import os
import re
import json
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

TWITTER_BASE = "https://api.twitterapi.io"
OPENAI_BASE = "https://api.openai.com/v1/responses"
FINNHUB_BASE = "https://finnhub.io/api/v1"

REVIEW_TYPE = os.environ.get("REVIEW_TYPE", "wallstreet").strip().lower()

REVIEW_CONFIGS = {
    "wallstreet": {
        "label": "טעימת וול סטריט",
        "sources_file": Path("sources/wallstreet.txt"),
        "legacy_sources_file": Path("accounts.txt"),
        "output_dir": Path("output/wallstreet"),
        "legacy_latest": True,
    },
    "israel": {
        "label": "טעימת השוק בישראל",
        "sources_file": Path("sources/israel.txt"),
        "legacy_sources_file": None,
        "output_dir": Path("output/israel"),
        "legacy_latest": False,
    },
    "trump": {
        "label": "ציוצי טראמפ",
        "sources_file": Path("sources/trump.txt"),
        "legacy_sources_file": None,
        "output_dir": Path("output/trump"),
        "legacy_latest": False,
    },
}

if REVIEW_TYPE not in REVIEW_CONFIGS:
    raise SystemExit(f"Unsupported REVIEW_TYPE: {REVIEW_TYPE}. Use one of: {', '.join(REVIEW_CONFIGS)}")

REVIEW_CONFIG = REVIEW_CONFIGS[REVIEW_TYPE]
OUTPUT_ROOT = Path("output")
OUTPUT_ROOT.mkdir(exist_ok=True)
OUTPUT_DIR = REVIEW_CONFIG["output_dir"]
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

TWITTER_API_KEY = os.environ.get("TWITTER_API_KEY", "").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.5").strip()

MAX_TWEETS_PER_ACCOUNT = int(os.environ.get("MAX_TWEETS_PER_ACCOUNT", "4"))
MAX_TWEETS_FOR_REVIEW = int(os.environ.get("MAX_TWEETS_FOR_REVIEW", "10"))
MAX_FINNHUB_SYMBOLS = int(os.environ.get("MAX_FINNHUB_SYMBOLS", "8"))

CASHTAG_RE = re.compile(r"(?<![A-Za-z0-9_])\$[A-Z]{1,6}(?![A-Za-z0-9_])")

if not TWITTER_API_KEY:
    raise SystemExit("Missing GitHub secret: TWITTER_API_KEY")

if not OPENAI_API_KEY:
    raise SystemExit("Missing GitHub secret: OPENAI_API_KEY")


def read_accounts() -> List[str]:
    sources_file = REVIEW_CONFIG["sources_file"]
    legacy = REVIEW_CONFIG.get("legacy_sources_file")

    if sources_file.exists():
        source_path = sources_file
    elif legacy and legacy.exists():
        source_path = legacy
    else:
        raise SystemExit(f"Missing sources file for {REVIEW_TYPE}: {sources_file}")

    accounts = []
    for line in source_path.read_text(encoding="utf-8").splitlines():
        account = line.strip().lstrip("@")
        if account and not account.startswith("#"):
            accounts.append(account)

    if not accounts:
        raise SystemExit(f"No X accounts configured in {source_path}")

    return accounts


def safe_float(value: Any) -> Optional[float]:
    try:
        if value is None:
            return None
        x = float(value)
        if x == 0:
            return 0.0
        return x
    except Exception:
        return None


def get_twitter_json(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    response = requests.get(
        f"{TWITTER_BASE}{path}",
        headers={"X-API-Key": TWITTER_API_KEY},
        params=params,
        timeout=40,
    )

    try:
        payload = response.json()
    except Exception:
        payload = {"raw_text": response.text[:2000]}

    payload["_http_status"] = response.status_code
    return payload


def looks_like_tweet(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    text = item.get("text") or item.get("fullText") or item.get("content")
    return isinstance(text, str) and bool(text.strip())


def find_tweets(obj: Any) -> List[Dict[str, Any]]:
    found = []

    if looks_like_tweet(obj):
        found.append(obj)

    elif isinstance(obj, list):
        for item in obj:
            found.extend(find_tweets(item))

    elif isinstance(obj, dict):
        for key in ("tweets", "data", "items", "results"):
            if key in obj:
                found.extend(find_tweets(obj[key]))

        if not found:
            for value in obj.values():
                found.extend(find_tweets(value))

    dedup = {}
    for tweet in found:
        key = str(tweet.get("id") or tweet.get("url") or tweet.get("text"))
        dedup[key] = tweet

    return list(dedup.values())


def normalize_tweet(tweet: Dict[str, Any], account: str) -> Dict[str, Any]:
    author = tweet.get("author") if isinstance(tweet.get("author"), dict) else {}

    text = tweet.get("text") or tweet.get("fullText") or tweet.get("content") or ""
    text = text.replace("\n", " ").strip()

    return {
        "account": account,
        "author": author.get("userName") or account,
        "id": tweet.get("id"),
        "url": tweet.get("url") or tweet.get("twitterUrl"),
        "createdAt": tweet.get("createdAt"),
        "text": text,
        "likeCount": tweet.get("likeCount") or 0,
        "retweetCount": tweet.get("retweetCount") or 0,
        "viewCount": tweet.get("viewCount") or 0,
        "cashtags": sorted(set(CASHTAG_RE.findall(text))),
        "is_retweet": text.startswith("RT @"),
    }


def fetch_tweets_for_account(account: str) -> List[Dict[str, Any]]:
    payload = get_twitter_json(
        "/twitter/user/last_tweets",
        {
            "userName": account,
            "cursor": "",
            "includeReplies": "false",
        },
    )

    tweets = find_tweets(payload)
    normalized = [normalize_tweet(tweet, account) for tweet in tweets]
    return normalized[:MAX_TWEETS_PER_ACCOUNT]


def tweet_score(tweet: Dict[str, Any]) -> float:
    score = 0

    text = (tweet.get("text") or "").lower()
    cashtags = tweet.get("cashtags", [])

    if cashtags:
        score += 30

    keywords = [
        "breaking", "just in", "earnings", "guidance", "upgrade", "downgrade",
        "price target", "merger", "acquisition", "contract", "export", "doj",
        "fed", "cpi", "pce", "yield", "oil", "iran", "ai", "space",
        "etf", "outflow", "inflow", "volatility", "nasdaq", "bitcoin",
    ]

    for word in keywords:
        if word in text:
            score += 8

    if tweet.get("viewCount", 0) >= 100000:
        score += 15
    elif tweet.get("viewCount", 0) >= 25000:
        score += 8

    if tweet.get("likeCount", 0) >= 500:
        score += 10
    elif tweet.get("likeCount", 0) >= 100:
        score += 5

    if tweet.get("is_retweet"):
        score -= 25

    return score


def select_tweets(tweets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    unique = {}
    for tweet in tweets:
        key = str(tweet.get("id") or tweet.get("url") or tweet.get("text"))
        unique[key] = tweet

    scored = [(tweet_score(tweet), tweet) for tweet in unique.values()]
    scored.sort(key=lambda x: x[0], reverse=True)

    return [tweet for _, tweet in scored[:MAX_TWEETS_FOR_REVIEW]]


def build_tweets_text(tweets: List[Dict[str, Any]]) -> str:
    lines = []

    for i, tweet in enumerate(tweets, start=1):
        lines.append(f"Tweet {i}")
        lines.append(f"Source: @{tweet.get('author')}")
        lines.append(f"Time: {tweet.get('createdAt')}")
        lines.append(f"URL: {tweet.get('url')}")
        lines.append(f"Cashtags: {', '.join(tweet.get('cashtags', [])) or 'None'}")
        lines.append(f"Text: {tweet.get('text')}")
        lines.append("")

    return "\n".join(lines)


def finnhub_get(path: str, params: Dict[str, Any]) -> Optional[Any]:
    if not FINNHUB_API_KEY:
        return None
    params = dict(params)
    params["token"] = FINNHUB_API_KEY
    try:
        response = requests.get(f"{FINNHUB_BASE}{path}", params=params, timeout=20)
        if response.status_code >= 400:
            return None
        return response.json()
    except Exception:
        return None


def collect_symbols(tweets: List[Dict[str, Any]]) -> List[str]:
    counts: Dict[str, int] = {}
    for tweet in tweets:
        for tag in tweet.get("cashtags", []):
            symbol = tag.replace("$", "").upper().strip()
            # Exclude obvious non-ticker artifacts if any appear.
            if 1 <= len(symbol) <= 6:
                counts[symbol] = counts.get(symbol, 0) + 1
    return [s for s, _ in sorted(counts.items(), key=lambda x: x[1], reverse=True)[:MAX_FINNHUB_SYMBOLS]]


def fetch_finnhub_context(symbols: List[str]) -> Dict[str, Any]:
    if not FINNHUB_API_KEY:
        return {"enabled": False, "symbols": {}, "note": "FINNHUB_API_KEY not configured"}

    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=5)
    context: Dict[str, Any] = {"enabled": True, "symbols": {}}

    for symbol in symbols:
        quote = finnhub_get("/quote", {"symbol": symbol})
        news = finnhub_get(
            "/company-news",
            {"symbol": symbol, "from": start.isoformat(), "to": today.isoformat()},
        )

        clean_quote = None
        if isinstance(quote, dict):
            current = safe_float(quote.get("c"))
            change_pct = safe_float(quote.get("dp"))
            prev_close = safe_float(quote.get("pc"))
            if current is not None and current > 0:
                clean_quote = {
                    "price": current,
                    "change_pct": change_pct,
                    "previous_close": prev_close,
                }

        clean_news = []
        if isinstance(news, list):
            for item in news[:3]:
                if not isinstance(item, dict):
                    continue
                headline = (item.get("headline") or "").strip()
                source = (item.get("source") or "").strip()
                if headline:
                    clean_news.append({"headline": headline, "source": source})

        if clean_quote or clean_news:
            context["symbols"][symbol] = {
                "quote": clean_quote,
                "news": clean_news,
            }

    return context


def build_market_context_text(context: Dict[str, Any]) -> str:
    if not context.get("enabled"):
        return "Market data not configured. Use only the tweets."

    symbols = context.get("symbols") or {}
    if not symbols:
        return "Market data configured, but no usable quote/news data was returned for the selected symbols."

    lines = ["Market context for selected cashtags. Use only as background validation, not as standalone content:"]
    for symbol, data in symbols.items():
        symbol_lines = []

        # Quote data is intentionally restricted: do not feed ordinary prices or small daily moves
        # to the model, because that creates irrelevant sentences like “$TSLA traded at...”.
        quote = data.get("quote")
        if quote:
            dp = quote.get("change_pct")
            if isinstance(dp, (int, float)) and abs(dp) >= 3:
                symbol_lines.append(f"Large daily move: {dp:.2f}%")

        news = data.get("news") or []
        for item in news[:2]:
            source = f" ({item.get('source')})" if item.get("source") else ""
            headline = item.get("headline")
            if headline:
                symbol_lines.append(f"News: {headline}{source}")

        if symbol_lines:
            lines.append(f"${symbol}")
            lines.extend(symbol_lines)
            lines.append("")

    if len(lines) == 1:
        return "Market data exists, but no large daily moves or useful recent headlines were found. Do not mention prices or daily changes."

    return "\n".join(lines)


def hebrew_weekday(dt: datetime) -> str:
    names = {
        0: "יום שני",
        1: "יום שלישי",
        2: "יום רביעי",
        3: "יום חמישי",
        4: "יום שישי",
        5: "שבת",
        6: "יום ראשון",
    }
    return names.get(dt.weekday(), "")


def observed_date(month: int, day: int, year: int):
    d = datetime(year, month, day).date()
    # NYSE observation rule used for federal market holidays:
    # Saturday holidays are usually observed on the prior Friday;
    # Sunday holidays are observed on the following Monday.
    if d.weekday() == 5:
        return d - timedelta(days=1)
    if d.weekday() == 6:
        return d + timedelta(days=1)
    return d


def nth_weekday(year: int, month: int, weekday: int, n: int):
    d = datetime(year, month, 1).date()
    days_until = (weekday - d.weekday()) % 7
    return d + timedelta(days=days_until + 7 * (n - 1))


def last_weekday(year: int, month: int, weekday: int):
    if month == 12:
        d = datetime(year + 1, 1, 1).date() - timedelta(days=1)
    else:
        d = datetime(year, month + 1, 1).date() - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - weekday) % 7)


def easter_date(year: int):
    # Gregorian Easter algorithm.
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return datetime(year, month, day).date()


def nyse_holiday_name(d) -> Optional[str]:
    year = d.year
    holidays = {
        observed_date(1, 1, year): "ראש השנה האזרחית",
        nth_weekday(year, 1, 0, 3): "יום מרטין לותר קינג",
        nth_weekday(year, 2, 0, 3): "יום הנשיאים",
        easter_date(year) - timedelta(days=2): "Good Friday",
        last_weekday(year, 5, 0): "Memorial Day",
        observed_date(6, 19, year): "Juneteenth",
        observed_date(7, 4, year): "יום העצמאות האמריקאי",
        nth_weekday(year, 9, 0, 1): "Labor Day",
        nth_weekday(year, 11, 3, 4): "חג ההודיה",
        observed_date(12, 25, year): "חג המולד",
    }
    return holidays.get(d)


def is_trading_day_nyse(d) -> bool:
    return d.weekday() < 5 and nyse_holiday_name(d) is None


def next_trading_day_nyse(d):
    nd = d + timedelta(days=1)
    while not is_trading_day_nyse(nd):
        nd += timedelta(days=1)
    return nd


def get_review_context(now_il: datetime) -> Dict[str, str]:
    now_ny = now_il.astimezone(ZoneInfo("America/New_York"))
    date_str = now_il.strftime("%Y-%m-%d")
    day_il = hebrew_weekday(now_il)
    ny_date = now_ny.date()

    # Weekend handling by Israel day, because the site is operated from Israel.
    if now_il.weekday() == 5:  # Saturday
        return {
            "mode": "weekly_weekend",
            "title": f"סיכום שבוע בוול סטריט והיערכות לפתיחת השבוע, {day_il} {date_str}",
            "editorial_focus": "סקירת סוף שבוע: חבר בין האירועים המרכזיים לשאלה מה חשוב לקראת פתיחת השבוע. אל תכתוב כאילו המסחר פתוח עכשיו.",
        }

    if now_il.weekday() == 6:  # Sunday
        return {
            "mode": "week_start_prep",
            "title": f"היערכות לפתיחת שבוע המסחר בוול סטריט, {day_il} {date_str}",
            "editorial_focus": "סקירת הכנה לפתיחת שבוע: התמקד בנושאים שיכולים ללוות את פתיחת המסחר הקרובה. אל תכתוב כאילו המסחר פתוח היום.",
        }

    # Monday-Friday according to New York market calendar and market hours.
    holiday = nyse_holiday_name(ny_date)
    if holiday:
        next_trade = next_trading_day_nyse(ny_date).isoformat()
        return {
            "mode": "market_holiday",
            "title": f"יום ללא מסחר בוול סטריט, {day_il} {date_str}",
            "editorial_focus": f"היום אין מסחר רגיל בארה״ב בגלל {holiday}. כתוב סקירת רקע והיערכות ליום המסחר הבא ({next_trade}). אל תכתוב 'לקראת פתיחת המסחר היום', 'בזמן מסחר' או 'סיכום יום המסחר'.",
        }

    market_open = now_ny.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now_ny.replace(hour=16, minute=0, second=0, microsecond=0)

    if now_ny < market_open:
        return {
            "mode": "premarket",
            "title": f"נקודות חשובות לקראת פתיחת המסחר בוול סטריט, {day_il} {date_str}",
            "editorial_focus": "סקירת טרום מסחר: התמקד במה שיכול להשפיע על הפתיחה, חוזים, טיקרים, דוחות, זרימות וסנטימנט.",
        }

    if market_open <= now_ny <= market_close:
        return {
            "mode": "intraday",
            "title": f"עדכון בזמן מסחר בוול סטריט, {day_il} {date_str}",
            "editorial_focus": "סקירה בזמן מסחר: כתוב כאילו המסחר פעיל עכשיו. התמקד במה שזז, אילו טיקרים/סקטורים במוקד, ומה מסביר את הסנטימנט תוך כדי יום המסחר. אל תכתוב 'לקראת פתיחה' או 'סיכום יום'.",
        }

    return {
        "mode": "postmarket",
        "title": f"סיכום יום המסחר בוול סטריט, {day_il} {date_str}",
        "editorial_focus": "סקירת אחרי נעילה: התמקד במה שהוביל את היום, אילו טיקרים וסקטורים בלטו, ומה נשאר חשוב להמשך.",
    }


def extract_openai_text(data: Dict[str, Any]) -> str:
    if isinstance(data.get("output_text"), str):
        return data["output_text"]

    parts = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if isinstance(content, dict) and "text" in content:
                parts.append(content["text"])

    return "\n".join(parts)


def get_israel_review_context(now_il: datetime) -> Dict[str, str]:
    date_str = now_il.strftime("%Y-%m-%d")
    day_il = hebrew_weekday(now_il)

    if now_il.weekday() == 5:
        return {
            "mode": "israel_weekend",
            "title": f"סיכום שבוע בבורסה בתל אביב והיערכות לשבוע הקרוב, {day_il} {date_str}",
            "editorial_focus": "סקירת סוף שבוע לשוק הישראלי: חבר בין האירועים המקומיים למה שחשוב לקראת השבוע הקרוב. אל תכתוב כאילו המסחר פתוח עכשיו.",
        }
    if now_il.weekday() == 6:
        return {
            "mode": "israel_week_start",
            "title": f"היערכות לפתיחת שבוע המסחר בתל אביב, {day_il} {date_str}",
            "editorial_focus": "סקירת הכנה לפתיחת השבוע בשוק הישראלי: התמקד במדדים, סקטורים, שקל/דולר, אג״ח, בנקים, נדל״ן, ביטחוניות ואירועים מקומיים.",
        }
    if now_il.hour < 9 or (now_il.hour == 9 and now_il.minute < 30):
        return {
            "mode": "israel_premarket",
            "title": f"נקודות חשובות לקראת פתיחת המסחר בתל אביב, {day_il} {date_str}",
            "editorial_focus": "סקירת טרום מסחר לשוק הישראלי: התמקד במה שיכול להשפיע על הפתיחה, מדדים, מניות, אג״ח, שקל/דולר וסנטימנט מקומי.",
        }
    if (now_il.hour, now_il.minute) <= (17, 35):
        return {
            "mode": "israel_intraday",
            "title": f"עדכון בזמן מסחר בבורסה בתל אביב, {day_il} {date_str}",
            "editorial_focus": "סקירה בזמן מסחר בישראל: כתוב כאילו המסחר פעיל עכשיו. התמקד במה שזז ובסקטורים/מניות במוקד.",
        }
    return {
        "mode": "israel_postmarket",
        "title": f"סיכום יום המסחר בבורסה בתל אביב, {day_il} {date_str}",
        "editorial_focus": "סקירת אחרי נעילה בשוק הישראלי: התמקד במה שהוביל את היום ומה חשוב להמשך.",
    }


def get_trump_review_context(now_il: datetime) -> Dict[str, str]:
    base = get_review_context(now_il)
    date_str = now_il.strftime("%Y-%m-%d")
    day_il = hebrew_weekday(now_il)
    base_mode = base["mode"]
    mode = "trump_" + base_mode

    if base_mode == "weekly_weekend":
        title = f"ציוצי טראמפ והשפעה אפשרית על השווקים, סיכום שבוע והיערכות לפתיחה, {day_il} {date_str}"
        focus = "סקירת סוף שבוע: חבר בין אמירות טראמפ לבין סקטורים, טיקרים, סחורות או רגולציה שיכולים להיות רלוונטיים לפתיחת השבוע. אל תכתוב פוליטיקה כללית."
    elif base_mode == "week_start_prep":
        title = f"ציוצי טראמפ והיערכות לפתיחת שבוע המסחר, {day_il} {date_str}"
        focus = "סקירת הכנה לפתיחת שבוע: התמקד באמירות עם פוטנציאל השפעה על סקטורים, מניות, סחורות, דולר, אג״ח או סנטימנט."
    elif base_mode == "market_holiday":
        title = f"ציוצי טראמפ ביום ללא מסחר בארה״ב, {day_il} {date_str}"
        focus = "יום ללא מסחר: כתוב רק נקודות רקע עם פוטנציאל שוקי ליום המסחר הבא."
    elif base_mode == "premarket":
        title = f"ציוצי טראמפ לקראת פתיחת המסחר בוול סטריט, {day_il} {date_str}"
        focus = "טרום מסחר: התמקד באמירות שעלולות להשפיע על פתיחת המסחר, סקטורים, טיקרים או סנטימנט."
    elif base_mode == "intraday":
        title = f"עדכון ציוצי טראמפ בזמן מסחר, {day_il} {date_str}"
        focus = "זמן מסחר: התמקד באמירות שעלולות להזיז סקטורים/טיקרים תוך כדי יום המסחר."
    else:
        title = f"ציוצי טראמפ וסיכום השפעה אפשרית על השוק, {day_il} {date_str}"
        focus = "אחרי נעילה: התמקד באמירות שיכולות להשפיע על המשך השבוע או על פתיחת המסחר הבאה."

    return {"mode": mode, "title": title, "editorial_focus": focus}


def get_active_review_context(now_il: datetime) -> Dict[str, str]:
    if REVIEW_TYPE == "israel":
        return get_israel_review_context(now_il)
    if REVIEW_TYPE == "trump":
        return get_trump_review_context(now_il)
    return get_review_context(now_il)


def get_review_prompt_header() -> str:
    if REVIEW_TYPE == "israel":
        return """אתה עורך סקירת שוק ההון בישראל בעברית עבור איש שוק הון מנוסה.

מקורות הקלט שלך הם ציוצים וידיעות ממקורות שהוגדרו לשוק הישראלי. השתמש בהם בלבד.
המטרה היא להפיק סקירה קצרה, קריאה ומקצועית על הבורסה בתל אביב והשוק המקומי.

התמקד רק במה שיש לו ערך שוקי ברור:
- מדדי ת״א 35 / ת״א 90 / בנקים / נדל״ן / ביטחוניות / אנרגיה / טכנולוגיה מקומית.
- אג״ח ממשלתי וקונצרני, תשואות, מרווחים, הנפקות.
- שקל/דולר ושקל/אירו, רק אם יש קשר ברור לשוק.
- רגולציה, מאקרו ישראלי, בנק ישראל, אינפלציה, תקציב, דירוג אשראי.
- אירועים ביטחוניים/פוליטיים רק אם יש להם חיבור ברור למניות, אג״ח, מט״ח, סקטור או סנטימנט.

אל תכניס פוליטיקה כללית, כותרות ביטחוניות כלליות או חדשות צרכניות אם אין להן חיבור שוקי ברור.
אל תנסה להמציא סימבולים ישראליים אם לא הופיעו בקלט. אם אין טיקר ברור, כתוב שם חברה או סקטור בלבד.
"""
    if REVIEW_TYPE == "trump":
        return """אתה עורך סקירת השפעה אפשרית של ציוצי טראמפ על שוק ההון, בעברית, עבור איש שוק הון מנוסה.

מקורות הקלט שלך הם ציוצים מחשבונות שהוגדרו לערוץ טראמפ. השתמש בהם בלבד.
המטרה אינה לסכם פוליטיקה ואינה להביע עמדה פוליטית. המטרה היא לזהות רק אמירות עם פוטנציאל השפעה שוקי.

התמקד רק כאשר יש קשר ברור לאחד מהתחומים הבאים:
- מניות, ETFים, מדדים או סקטורים ציבוריים.
- מכסים, סין, סחר חוץ, תעשייה, רכב, שבבים, ביטחון, אנרגיה, קריפטו, בנקים, ריבית, דולר, אג״ח או רגולציה.
- חברה ציבורית שהוזכרה במפורש או סקטור ציבורי שעלול להיות מושפע.

אל תכניס:
- פוליטיקה כללית בלי קשר ברור לשוק.
- עלבונות, בחירות, סקרים או משפטים אם אין להם השלכה שוקית ברורה.
- ניתוח אישי על טראמפ.
- טיקרים שלא הופיעו בקלט או שלא קשורים באופן ברור לאמירה.

כל סעיף צריך לענות על:
1. מה נאמר או מה בלט.
2. איזה סקטור/נכס/טיקר עשוי להיות רלוונטי.
3. למה זה חשוב עכשיו לשוק.
אם אין השלכה שוקית ברורה, השמט את הסעיף.
"""

    return """אתה עורך סקירת וול סטריט בעברית עבור איש שוק הון מנוסה.

מקורות הקלט שלך הם ציוצים שנאספו ונתוני שוק שנשלפו עבור חלק מהטיקרים. השתמש בהם בלבד.
אל תוסיף ידע חיצוני, אל תשלים פערים, אל תיתן המלצת השקעה, ואל תיצור קשר סיבתי שלא מופיע בקלט.
"""


def call_openai(tweets: List[Dict[str, Any]], market_context: Dict[str, Any]) -> str:
    tweets_text = build_tweets_text(tweets)
    market_context_text = build_market_context_text(market_context)

    now_il = datetime.now(ZoneInfo("Asia/Jerusalem"))
    review_context = get_active_review_context(now_il)
    review_context_title = review_context["title"]
    review_context_mode = review_context["mode"]
    review_editorial_focus = review_context["editorial_focus"]

    if review_context_mode.endswith("weekly_weekend"):
        forward_section_title = "לקראת השבוע הקרוב"
        forward_section_instruction = (
            "חובה להוסיף אחרי נקודות מרכזיות סעיף בשם 'לקראת השבוע הקרוב'. "
            "כתוב בו 4 עד 5 טריגרים קצרים שמתרגמים את הסקירה לשאלות/מוקדי מעקב לשבוע הבא. "
            "זה צריך להיות מבט קדימה, לא חזרה על מה שכבר נכתב."
        )
    elif review_context_mode.endswith("week_start_prep") or review_context_mode.endswith("market_holiday") or review_context_mode in {"week_start_prep", "market_holiday"}:
        forward_section_title = "לקראת יום המסחר הבא"
        forward_section_instruction = (
            "חובה להוסיף אחרי נקודות מרכזיות סעיף בשם 'לקראת יום המסחר הבא'. "
            "כתוב בו 3 עד 5 טריגרים קצרים על מה חשוב לפתיחה/ליום המסחר הבא. "
            "זה צריך להיות מבט קדימה, לא חזרה על מה שכבר נכתב."
        )
    else:
        forward_section_title = ""
        forward_section_instruction = (
            "אל תוסיף סעיף נפרד של מבט קדימה. אם יש נקודת המשך חשובה, שלב אותה בשורה התחתונה."
        )

    if REVIEW_TYPE == "israel":
        main_section_title = "במרכז השוק המקומי"
        background_section_title = "ברקע המקומי"
        if forward_section_title == "לקראת יום המסחר הבא":
            forward_section_title = "לקראת המסחר בתל אביב"
        elif forward_section_title == "לקראת השבוע הקרוב":
            forward_section_title = "לקראת השבוע בבורסה בתל אביב"
        market_lens_block = """
כללי עריכה ייחודיים לערוץ ישראל, חובה:
1. מהות הסקירה היא השוק הישראלי, הבורסה בתל אביב, שקל, אג״ח מקומי, בנקים, נדל״ן, ביטחוניות, אנרגיה, ביטוח ופיננסים.
2. אל תכתוב סקירת וול סטריט בעברית. אם מופיעה כותרת גלובלית או אמריקאית, הכנס אותה רק אם יש לה השלכה ברורה על תל אביב, שקל, אג״ח, סקטור ישראלי או מניות מקומיות.
3. הפתיח חייב לענות על שאלת שוק ישראלית, למשל: האם השוק המקומי מתמחר סיכון ביטחוני, ריבית, שקל, בנקים, נדל״ן או זרימות לתל אביב.
4. בכל נושא ציין את החיבור המקומי. לא מספיק לכתוב אירוע כללי, צריך להסביר איך הוא קשור למדדי ת״א, סקטור מקומי, מט״ח, אג״ח או סנטימנט משקיעים בישראל.
5. העדף שמות בעברית: מדד ת״א 35, מדד ת״א 90, מדד הבנקים, שקל־דולר, אג״ח ממשלתי, אג״ח קונצרני. אל תתחיל שורה באנגלית.
6. אם אין מספיק חומר מקומי איכותי, כתוב סקירה קצרה יותר. אל תמלא חלל בכותרות חוץ שאינן קשורות ישירות לשוק הישראלי.
7. סעיף הקדימה צריך לכלול טריגרים מקומיים: כיוון השקל, תשואות אג״ח, בנקים, נדל״ן, ביטחוניות, מחזורי מסחר בתל אביב, הודעות חברות מקומיות ואירועים ביטחוניים עם קשר שוקי.
"""
    else:
        main_section_title = "במרכז הפתיחה"
        background_section_title = "ברקע"
        market_lens_block = ""

    prompt = f"""
{get_review_prompt_header()}
המטרה:
להפיק סקירת Market Desk קצרה, היררכית וקריאה בעברית. הסקירה אינה רשימת ציוצים ואינה דוח ציות. היא צריכה לתת לקורא מסגרת שוקית ברורה: מה במרכז, מה ברקע, ומה הטריגרים להמשך.

עקרונות סגנון חובה:
1. כתוב בעברית מלאה וביישור טבעי מימין לשמאל. אל תפתח שורה, כותרת משנה או פסקה באנגלית, בטיקר או בסימבול. אם צריך להזכיר שם באנגלית, פתח קודם במילה עברית, למשל "ספייסאיקס", "אלפבית", "מניית", "קרן", "סקטור".
2. הסקירה צריכה להרגיש כמו עמדת Market Desk, לא כמו סיכום מקורות. יש לנסח תזה מרכזית ולהוביל את הקורא דרכה.
3. אל תכתוב "לפי הציוצים שנאספו", "לפי נתוני Finnhub", "לפי הטענות" או כל ניסוח שמסביר את המקורות.
4. אל תשתמש במקפים כפולים כמו -- או ״––״, ואל תשתמש במקף ארוך/Em dash. השתמש בפסיק, נקודה, נקודתיים או נקודה־פסיק.
5. אל תרבה בסימבולים. אם סימבול חיוני, כתוב אותו פעם אחת בלבד בסוגריים אחרי שם החברה או המוצר. אל תכתוב SPCX$ או TSLA$. אל תפתח שורה בסימבול.
6. אם כמה כותרות שייכות לאותו נושא, אחד אותן לפסקה אחת. לדוגמה: ספייסאיקס והמוצרים הממונפים סביבה הם סעיף אחד, לא שני סעיפים נפרדים.
7. הימנע מהמבנה החוזר "זה חשוב כי". במקום זאת, כתוב את המשמעות באופן טבעי: "המשמעות לשוק היא...", "עבור הסקטור...", "הנקודה המקצועית היא...".
8. אל תסיים כל סעיף בהסתייגות. אם נדרשת זהירות, שלב אותה בקצרה בתוך המשפט.
9. אל תכניס סעיף שאין לו ערך שוקי ברור. אם אין תובנה, השמט.
10. אל תכתוב סעיף בשם "מה דורש מעקב".
11. אל תשתמש ב-** או בהדגשות בתוך משפטים. ההיררכיה תבוא רק מכותרות Markdown: ## לכותרת אזור ו-### לכותרת נושא. הפסקאות עצמן יהיו טקסט רגיל.

היררכיה וזרימה:
- הפתיח יציג ב-2 משפטים בלבד את שאלת השוק המרכזית.
- לאחר הפתיח חובה לחלק את הסקירה כך:
  1. "{main_section_title}".
  2. "{background_section_title}".
  3. סעיף קדימה רק אם נדרש לפי זמן ההרצה.
  4. "שורה תחתונה".
- "במרכז" יכלול 3 נקודות בלבד, הנושאים החשובים ביותר.
- "ברקע" יכלול 2 עד 3 נקודות קצרות יותר.
- כל נושא בתוך הסעיף יופיע ככותרת משנה נפרדת ברמה ### בשורה משלו, ואחריה פסקה רגילה של עד 2 משפטים. לא להשתמש בהדגשות בתוך הפסקה.
- כל כותרת משנה וכל פסקה חייבות להתחיל בעברית, לא באנגלית ולא בטיקר.

ערך מוסף:
כל נושא חייב לכלול שני רכיבים:
1. מה קרה או מה בלט.
2. למה זה משנה עכשיו לשוק, לסקטור, לזרימות, לתנודתיות או לסנטימנט.
הסקירה צריכה לעזור לקורא להבין סדר עדיפויות, לא רק לדעת מה קרה.
{market_lens_block}
לקראת המסחר / השבוע:
{forward_section_instruction}
אם נדרש סעיף קדימה, הוא חייב להיות בשם המדויק: ## {forward_section_title}
בסעיף הזה אל תכתוב "לעקוב אחרי" בצורה כללית. כתוב טריגרים שימושיים:
- "אם X קורה, זה יחזק את..."
- "אם Y קורה, זה יסמן מעבר ל..."
- "שילוב של X ו-Y יאותת ש..."
כתוב 3 עד 4 טריגרים בלבד.

חברות פרטיות ונושאים סקטוריאליים:
אם חברה פרטית מופיעה בקלט, אל תהפוך אותה לאירוע טיקר ישיר ואל תכתוב "שאינה נסחרת". הצג את המשמעות דרך הסקטור הרלוונטי: רגולציית AI, ענן, שבבים, תשתיות חישוב, ביטחון לאומי או זרימות.

שימוש בנתוני שוק:
נתוני השוק הם שכבת בדיקה ורקע בלבד. אל תכתוב מחיר או שינוי יומי רק כי הנתון קיים. השתמש במספר רק אם הוא הסיפור עצמו או אם הוא מחזק נקודת שוק קיימת.

הקשר פנימי לזמן ההרצה, לשימושך בלבד ולא להדפסה בסקירה:
- מצב סקירה: {review_context_mode}
- כותרת משנה לשימוש בפלט: {review_context_title}
- הנחיית עריכה: {review_editorial_focus}
- הנחיית סעיף קדימה: {forward_section_instruction}
- כותרת סעיף קדימה, אם נדרש: {forward_section_title}

אסור להדפיס בסקירה את המילים "הקשר זמן הסקירה", "הנחיית עריכה", "הנחיית סעיף", "weekly_weekend", "premarket", "intraday", "postmarket", "market_holiday" או כל שם מצב פנימי אחר.

מבנה פלט חובה:

# 🌅 {REVIEW_CONFIG["label"]}

{review_context_title}

פתיח קצר של 2 משפטים בלבד. הפתיח חייב להציג שאלת שוק מרכזית אחת שמארגנת את הסקירה.

## {main_section_title}

כתוב 3 נושאים בלבד.
כל נושא בפורמט הבא, בלי כוכביות ובלי הדגשות פנימיות:
### כותרת עברית קצרה
פסקה רגילה של עד 2 משפטים.
כותרת הסעיף הראשית חייבת להיות בדיוק: ## {main_section_title}

## {background_section_title}

כתוב 2 עד 3 נושאים בלבד.
כל נושא בפורמט של כותרת משנה ### ואחריה פסקה רגילה קצרה יותר מהפסקאות במרכז.

הוסף סעיף קדימה רק לפי ההנחיה הפנימית: {forward_section_instruction}
אם נדרש סעיף כזה, הכותרת שלו חייבת להיות בדיוק: ## {forward_section_title}
כתוב בו 3 עד 4 טריגרים. כל טריגר יופיע ככותרת משנה ### ואחריה משפט אחד עד שניים. לא להשתמש בכוכביות ובהדגשות פנימיות.

## שורה תחתונה

כתוב 2 עד 3 משפטים בלבד. השורה התחתונה צריכה להיות עמדה מסכמת, לא חזרה על כל הסקירה.

בסוף כתוב בדיוק:

⚠️ גילוי נאות: תוכן זה נוצר באמצעות AI לצרכים אינפורמטיביים בלבד. אין באמור ייעוץ השקעות או המלצה לפעולה בניירות ערך.

פותח ע"י דורון שרייבמן

בדיקת איכות פנימית לפני החזרה:
- ודא שהסקירה קצרה יותר, היררכית יותר וחדה יותר.
- ודא שאין יותר מ-6 נושאים לפני סעיף הקדימה.
- אם זו סקירת ישראל, ודא שהסקירה היא על השוק הישראלי ולא על וול סטריט; כל כותרת חוץ חייבת להיות מחוברת להשפעה מקומית.
- ודא שכל כותרת משנה ופסקה מתחילות בעברית ולא באנגלית או בטיקר.
- ודא שנושאים קשורים אוחדו לפסקה אחת ולא פוצלו מלאכותית.
- ודא שאין "זה חשוב כי" שחוזר על עצמו.
- ודא שסעיף הקדימה כולל טריגרים ולא ניסוח כללי של "לעקוב".
- ודא שהשורה התחתונה היא 2 עד 3 משפטים בלבד.
- ודא שאין מקפים כפולים, אין מקף ארוך ואין הדגשות פנימיות עם **.
- ודא שאין תקלות RTL כמו SPCX$ או P&S.
- ודא שלא הודפסו הוראות פנימיות או שמות מצב.

ציוצים:
{tweets_text}

נתוני שוק, לשימוש פנימי בלבד:
{market_context_text}
"""

    payload = {
        "model": OPENAI_MODEL,
        "input": prompt,
        "max_output_tokens": 3600,
    }

    response = requests.post(
        OPENAI_BASE,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=220,
    )

    try:
        data = response.json()
    except Exception:
        raise SystemExit(f"OpenAI returned non-JSON response: {response.text[:2000]}")

    if response.status_code >= 400:
        raise SystemExit(
            "OpenAI API error:\n"
            + json.dumps(data, ensure_ascii=False, indent=2)[:4000]
        )

    text = extract_openai_text(data)
    if not text:
        raise SystemExit("OpenAI returned empty text.")

    return text.strip()



TICKER_NAME_MAP = {
    "SPCX": "SpaceX",
    "TSLA": "Tesla",
    "PYPL": "PayPal",
    "AMZN": "Amazon",
    "IBIT": "IBIT",
    "SOXX": "קרן השבבים",
    "SPXL": "קרן ה-S&P הממונפת",
    "VXN": "מדד התנודתיות",
    "SPCH": "מוצר הלונג",
    "SSPC": "מוצר השורט",
}

# Only these symbols may remain visually in the text, and only on first meaningful mention.
# After the first mention, the script converts the symbol to a plain company/instrument name.
IMPORTANT_KEEP_SYMBOLS = {"SPCX", "SPCH", "SSPC", "IBIT", "SOXX", "SPXL", "VXN", "TSLA", "PYPL"}


def normalize_cashtag_direction(text: str) -> str:
    # Fix RTL artifacts such as SPCX$ or VXN,$ back to normal ticker form.
    text = re.sub(r"\b([A-Z]{1,6}),\$", r"$\1", text)
    text = re.sub(r"\b([A-Z]{1,6})\$", r"$\1", text)
    text = re.sub(r"\$([A-Z]{1,6})\s*,", r"$\1,", text)
    text = re.sub(r"\$([A-Z]{1,6})\s*\.", r"$\1.", text)
    return text


def reduce_ticker_noise(text: str) -> str:
    """Reduce unnecessary ticker noise after the model writes the review.

    Policy:
    - A symbol may appear only once in the whole review.
    - The first appearance becomes Name ($SYMBOL).
    - Later appearances become the plain company/instrument name.
    - Symbols not in IMPORTANT_KEEP_SYMBOLS are removed and replaced by a name when known.
    - This is deliberately strict because the review is for reading, not a trading terminal.
    """
    text = normalize_cashtag_direction(text)
    seen_global = set()

    def repl(match: re.Match) -> str:
        symbol = match.group(1).upper()
        name = TICKER_NAME_MAP.get(symbol, symbol)

        if symbol not in IMPORTANT_KEEP_SYMBOLS:
            return name

        if symbol in seen_global:
            return name

        seen_global.add(symbol)
        return f"{name} (${symbol})"

    text = re.sub(r"\$([A-Z]{1,6})(?![A-Z0-9])", repl, text)

    # Clean duplicated name/ticker patterns, for example SpaceX SpaceX ($SPCX).
    for symbol, name in TICKER_NAME_MAP.items():
        text = text.replace(f"{name} {name} (${symbol})", f"{name} (${symbol})")
        text = text.replace(f"{name} ({name} (${symbol}))", f"{name} (${symbol})")

    return text


def normalize_review_text(text: str) -> str:
    """Final cleanup before writing the review.

    Keeps Hebrew financial text clean and prevents punctuation artifacts
    such as double hyphens from leaking into the website/PDF/WhatsApp.
    """
    replacements = {
        "---": ",",
        "--": ",",
        "––": ",",
        "—": ",",
        "–": ",",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)

    # Remove inline bold markers. Headings provide hierarchy; body text stays clean.
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text)

    # Clean repeated commas/spaces created by replacements, without harming newlines.
    text = re.sub(r"[ \t]+,", ",", text)
    text = re.sub(r",[ \t]*,", ",", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

def main() -> None:
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    accounts = read_accounts()

    all_tweets = []
    for account in accounts:
        print(f"Fetching @{account}...")
        all_tweets.extend(fetch_tweets_for_account(account))

    selected = select_tweets(all_tweets)
    symbols = collect_symbols(selected)
    market_context = fetch_finnhub_context(symbols)

    print(f"Total raw tweets: {len(all_tweets)}")
    print(f"Selected tweets: {len(selected)}")
    print(f"Detected symbols for Finnhub: {', '.join(symbols) or 'None'}")
    print(f"Finnhub enabled: {bool(FINNHUB_API_KEY)}")

    review = normalize_review_text(reduce_ticker_noise(call_openai(selected, market_context)))

    input_json = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "generated_israel": datetime.now(ZoneInfo("Asia/Jerusalem")).isoformat(),
        "model": OPENAI_MODEL,
        "total_raw_tweets": len(all_tweets),
        "selected_tweets": selected,
        "detected_cashtags": sorted(
            set(tag for tweet in selected for tag in tweet.get("cashtags", []))
        ),
        "finnhub_enabled": bool(FINNHUB_API_KEY),
        "finnhub_symbols_checked": symbols,
        "finnhub_context": market_context,
        "review_type": REVIEW_TYPE,
        "review_context": get_active_review_context(datetime.now(ZoneInfo("Asia/Jerusalem"))),
    }

    timestamped_input_path = OUTPUT_DIR / f"review_input_{run_ts}.json"
    timestamped_review_path = OUTPUT_DIR / f"{REVIEW_TYPE}_review_{run_ts}.md"

    timestamped_input_path.write_text(
        json.dumps(input_json, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    timestamped_review_path.write_text(
        review,
        encoding="utf-8",
    )

    (OUTPUT_DIR / "latest.json").write_text(
        json.dumps(input_json, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    (OUTPUT_DIR / "latest.md").write_text(
        review,
        encoding="utf-8",
    )

    # Backward compatibility for the existing website path.
    if REVIEW_CONFIG.get("legacy_latest"):
        Path("output/latest.json").write_text(
            json.dumps(input_json, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        Path("output/latest.md").write_text(
            review,
            encoding="utf-8",
        )

    print("Review created successfully.")
    print(f"Wrote {timestamped_review_path}")
    print(f"Wrote {OUTPUT_DIR / 'latest.md'} for the website.")
    print(review[:1200])


if __name__ == "__main__":
    main()
