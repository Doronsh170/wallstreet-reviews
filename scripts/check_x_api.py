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

ACCOUNTS_FILE = Path("accounts.txt")
OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

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
    accounts = []
    for line in ACCOUNTS_FILE.read_text(encoding="utf-8").splitlines():
        account = line.strip().lstrip("@")
        if account and not account.startswith("#"):
            accounts.append(account)
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
        return "Finnhub not configured. Use only the tweets."

    symbols = context.get("symbols") or {}
    if not symbols:
        return "Finnhub configured, but no usable quote/news data was returned for the selected symbols."

    lines = ["Finnhub market data for selected cashtags:"]
    for symbol, data in symbols.items():
        lines.append(f"${symbol}")
        quote = data.get("quote")
        if quote:
            dp = quote.get("change_pct")
            dp_text = f", change {dp:.2f}%" if isinstance(dp, (int, float)) else ""
            lines.append(f"Quote: price {quote.get('price')}{dp_text}")
        news = data.get("news") or []
        for item in news:
            source = f" ({item.get('source')})" if item.get("source") else ""
            lines.append(f"News: {item.get('headline')}{source}")
        lines.append("")
    return "\n".join(lines)


def extract_openai_text(data: Dict[str, Any]) -> str:
    if isinstance(data.get("output_text"), str):
        return data["output_text"]

    parts = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if isinstance(content, dict) and "text" in content:
                parts.append(content["text"])

    return "\n".join(parts)


def call_openai(tweets: List[Dict[str, Any]], market_context: Dict[str, Any]) -> str:
    tweets_text = build_tweets_text(tweets)
    market_context_text = build_market_context_text(market_context)

    now_il = datetime.now(ZoneInfo("Asia/Jerusalem"))
    generated_date = now_il.strftime("%Y-%m-%d")

    prompt = f"""
אתה כותב סקירת וול סטריט מקצועית בעברית עבור איש שוק הון מנוסה.

מקורות המידע שלך:
1. ציוצים שסופקו לך.
2. נתוני Finnhub שסופקו לך, אם קיימים.

אסור להוסיף מידע חיצוני מעבר לשני מקורות אלה.
אסור להמציא קשרים, אסור להשלים פערים מהידע הכללי שלך, ואסור לתת המלצת השקעה.

המטרה:
לכתוב דף קצר, קריא ומקצועי בסגנון “הכנה ליום מסחר”, דומה למסמך נקודות מרכזיות.
הטקסט צריך להישמע כמו סקירת שוק של עורך מקצועי, לא כמו דוח ציות ולא כמו רשימת הסתייגויות.

כלל חשוב מאוד לגבי הסתייגויות:
אל תסיים כל סעיף בהסתייגות.
אל תכתוב בכל נקודה "דורש אימות", "לא הודעה רשמית", "אין קשר סיבתי".
זה לא מקצועי.

הוסף הסתייגות רק באחד מהמקרים הבאים:
- שמועה מפורשת.
- שוק תחזיות, למשל Kalshi.
- נתון חריג מאוד שאין לו חיזוק מנתוני Finnhub.
- טענה על פעולה עתידית לא רשמית.
- סתירה ברורה בין מקורות.
- קשר סיבתי לא מוכח שהקורא עלול להבין בטעות.

כאשר מדובר בנתון שוק רגיל, זרימות כספים, תנועת מדד, דיווח עסקי, חדשות חברה או טיקר שעלה בציוצים, כתוב אותו באופן ישיר וענייני, ללא הסתייגות מיותרת.

אם יש כמה נקודות שדורשות זהירות, רכז אותן בסוף תחת כותרת קצרה:
## מה דורש מעקב
ולא בתוך כל סעיף.

סינון חשיבות:
אל תשתמש בכל ציוץ שנאסף. בחר רק פריטים שנותנים ערך שוקי ברור.
ציוץ ייכנס לסקירה רק אם הוא עומד לפחות באחד מהתנאים:
- כולל טיקר רלוונטי.
- קשור לסקטור ציבורי.
- קשור למדד, סחורה, תשואה, מטבע, קריפטו או חוזים עתידיים.
- מצביע על אירוע חברה מהותי.
- חוזר בכמה מקורות.
- משנה סנטימנט סביב נושא מרכזי.

כותרת גיאופוליטית או חדשותית כללית ללא חיבור שוקי ברור לא תיכנס לנקודות המרכזיות.
אם היא חשובה אך ללא חיבור ברור, השמט אותה.

כללים מחייבים לטיקרים ולנתונים:
- שמור טיקרים בדיוק כפי שהם מופיעים, למשל $TSLA, $IBIT, $SPCX.
- לעולם אל תחליף טיקר ל-$1.
- אל תכניס טיקר רק כי הופיע פעם אחת בלי ערך שוקי ברור.
- אם Finnhub מספק מחיר או שינוי יומי, אפשר להשתמש בזה כתוספת קצרה, אבל רק אם זה באמת מחזק את הסעיף.
- אם Finnhub לא מחזיר נתון לטיקר מסוים, אל תכתוב על כך בסקירה. פשוט התעלם.

סגנון כתיבה:
- עברית מקצועית וזורמת.
- בלי הביטוי "לפי הציוצים שנאספו".
- בלי הביטוי "לפי הטענות".
- בלי ניסוחים משפטיים חוזרים.
- בלי טבלאות.
- בלי פרקים רבים.
- משפטים קצרים עד בינוניים.
- כל נקודה צריכה לתת ערך מיידי לקורא.

ניסוחים מועדפים:
- "במוקד עמד..."
- "עוד בלט..."
- "בצד הטכנולוגיה..."
- "בקריפטו..."
- "במניות המדיה..."
- "הנתון המרכזי..."
- "המסר למשקיעים..."
- "נקודת המעקב..."

ניסוחים אסורים:
- "לפי הציוצים שנאספו"
- "לפי הטענות"
- "אין ספק ש"
- "ברור ש"
- "המניה צפויה"
- "השוק ירד בגלל"
- "זו הוכחה לכך"

מבנה פלט חובה:

# 🌅 טעימת וול סטריט

נקודות חשובות לקראת/אחרי המסחר בוול סטריט, {generated_date}

פתיח קצר של 2 עד 3 משפטים. הפתיח יציג את מוקדי העניין המרכזיים בצורה טבעית, בלי הסתייגות משפטית ובלי להציג זאת כתמונת שוק מלאה.

## נקודות מרכזיות

כתוב 6 עד 8 נקודות בלבד.
כל נקודה תהיה בוליט אחד במבנה:
• **כותרת קצרה:** 2 עד 3 משפטים רציפים.

אין להוסיף בסוף כל בוליט משפט הסתייגות.
אם סעיף מחייב זהירות, כתוב אותה במשפט קצר וטבעי בתוך הסעיף, ורק אם באמת צריך.

סדר עדיפות לנקודות:
1. הנושא השוקי המרכזי ביותר.
2. SpaceX / $SPCX אם הופיע.
3. ETFים, מוצרים ממונפים או אירועי מסחר אם הופיעו.
4. טיקרים מרכזיים עם אירוע ברור.
5. קריפטו / Bitcoin ETF אם הופיע.
6. AI / רגולציה אם יש חיבור שוקי.
7. M&A / מדיה אם הופיע.
8. תנודתיות / מדדים רק אם זה מוסיף ערך ברור.

## מה דורש מעקב

כתוב 2 עד 4 נקודות קצרות בלבד.
כאן מרכזים הסתייגויות אמיתיות בלבד, למשל:
• אימות נתוני שווי/מחזור חריגים.
• האם מוצר ממונף אכן מתחיל להיסחר.
• האם זרימות שליליות בקרנות נמשכות.
• האם דיווח רגולטורי מקבל אישור רשמי נוסף.

אם אין נקודות מעקב חשובות, כתוב 2 נקודות בלבד.

בסוף הסקירה כתוב בדיוק:

⚠️ גילוי נאות: תוכן זה נוצר באמצעות AI לצרכים אינפורמטיביים בלבד. אין באמור ייעוץ השקעות או המלצה לפעולה בניירות ערך.

פותח ע"י דורון שרייבמן

ציוצים:
{tweets_text}

נתוני שוק מ-Finnhub, אם קיימים:
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

    review = call_openai(selected, market_context)

    input_json = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "model": OPENAI_MODEL,
        "total_raw_tweets": len(all_tweets),
        "selected_tweets": selected,
        "detected_cashtags": sorted(
            set(tag for tweet in selected for tag in tweet.get("cashtags", []))
        ),
        "finnhub_enabled": bool(FINNHUB_API_KEY),
        "finnhub_symbols_checked": symbols,
        "finnhub_context": market_context,
    }

    timestamped_input_path = Path(f"output/review_input_{run_ts}.json")
    timestamped_review_path = Path(f"output/wallstreet_review_{run_ts}.md")

    timestamped_input_path.write_text(
        json.dumps(input_json, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    timestamped_review_path.write_text(
        review,
        encoding="utf-8",
    )

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
    print("Wrote output/latest.md for the website.")
    print(review[:1200])


if __name__ == "__main__":
    main()
