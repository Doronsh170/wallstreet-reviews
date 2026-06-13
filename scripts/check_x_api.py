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


def call_openai(tweets: List[Dict[str, Any]], market_context: Dict[str, Any]) -> str:
    tweets_text = build_tweets_text(tweets)
    market_context_text = build_market_context_text(market_context)

    now_il = datetime.now(ZoneInfo("Asia/Jerusalem"))
    review_context = get_review_context(now_il)
    review_context_title = review_context["title"]
    review_context_mode = review_context["mode"]
    review_editorial_focus = review_context["editorial_focus"]

    prompt = f"""
אתה עורך סקירת וול סטריט בעברית עבור איש שוק הון מנוסה.

מקורות הקלט שלך הם ציוצים שנאספו ונתוני שוק שנשלפו עבור חלק מהטיקרים. השתמש בהם בלבד.
אל תוסיף ידע חיצוני, אל תשלים פערים, אל תיתן המלצת השקעה, ואל תיצור קשר סיבתי שלא מופיע בקלט.

המטרה:
להפיק דף קריא, חד ומקצועי שמתאים לזמן ההרצה. לא סיכום ציוצים, לא דוח ציות, ולא רשימת הסתייגויות.

כללי עריכה חשובים:
1. אל תכתוב "לפי הציוצים שנאספו".
2. אל תכתוב "לפי נתוני Finnhub" או כל אזכור ל-Finnhub. נתוני השוק הם חומר עזר פנימי בלבד.
3. אל תכתוב "לפי הטענות". השתמש ב"דווח", "עלה", "בלט", "נרשם", "הוזכר".
4. אל תסיים כל סעיף בהסתייגות.
5. אל תכניס סעיף שאין לו ערך שוקי ברור.
6. אל תכניס כותרת גיאופוליטית כללית אם אין לה חיבור מפורש בקלט לנפט, תשואות, חוזים, מדדים, סחורות, מטבעות, טיקר או סקטור.
7. אל תשתמש במחיר נקוב או שינוי יומי רק כדי למלא סעיף. מחיר ושינוי יומי ייכנסו רק אם התנועה עצמה היא הסיפור: ירידה חדה, עלייה חדה, תגובה לדוח, אירוע חברה, שבירה טכנית משמעותית, או אם הציוץ עצמו עוסק בתנועת המחיר.
8. אם טיקר מופיע בהקשר של השוואת שווי, שמועה, עסקה או נרטיב, אל תוסיף לו מחיר ושינוי יומי אם הם לא מסבירים את הסיפור. לדוגמה: אל תכתוב מחיר ושינוי יומי של $TSLA רק כי היא הוזכרה בהשוואה ל-$SPCX.
9. שמור טיקרים בדיוק כפי שהם מופיעים, למשל $TSLA, $IBIT, $SPCX. לעולם אל תחליף טיקר ל-$1.
10. אם הנתון נשמע טכני מדי, למשל מדד תנודתיות, הסבר במשפט אחד למה זה חשוב. אם אין הסבר ברור, השמט אותו.
11. אם יש יותר מדי נקודות, תעדיף מיקרו: חברות, ETFים, סקטורים, זרימות ואירועי חברה.
12. התאם את השפה לזמן ההרצה. אם זו סקירה בזמן מסחר, אל תכתוב "לקראת פתיחה" או "אחרי נעילה". אם זו שבת, אל תכתוב כאילו יש מסחר פתוח. אם זו סקירת סיום יום, אל תכתוב כאילו היום עוד לפנינו.

איך להשתמש בנתוני השוק:
- השתמש בהם רק כשכבת בדיקה ורקע, לא כתוכן עצמאי.
- אל תכתוב מחיר מניה או שינוי יומי אלא אם התנועה במחיר היא מרכז הסיפור.
- אל תכניס מחיר או שינוי יומי לטיקר רק מפני שהנתון קיים.
- אם הטיקר מוזכר בגלל שווי, מיזוג, ETF, רגולציה, זרימות או שמועה, בדרך כלל אין צורך במחיר יומי.
- אם אין נתון שוק שימושי שמחזק נקודה קיימת, התעלם ממנו לגמרי.

הסתייגויות:
הסתייגות תופיע רק כשיש סיבה אמיתית:
- שמועה.
- שוק תחזיות כמו Kalshi.
- נתון חריג במיוחד.
- פעולה עתידית לא רשמית.
- סתירה בין מקורות.
- קשר סיבתי שהקורא עלול להבין בטעות.

גם כשנדרשת הסתייגות, היא צריכה להיות קצרה וטבעית. אל תהפוך כל סעיף למשפט הגנה.
אל תרכז הסתייגויות בסעיף נפרד. אם נדרשת הסתייגות, שלב אותה בקצרה ובטבעיות בתוך הסעיף הרלוונטי בלבד.

מבנה פלט חובה:

# 🌅 טעימת וול סטריט

{review_context_title}

הקשר זמן הסקירה: {review_context_mode}
הנחיית עריכה לזמן ההרצה: {review_editorial_focus}

פתיח קצר של 2 משפטים בלבד. הפתיח יציג את מוקדי העניין המרכזיים בהתאם לזמן ההרצה. בלי הביטוי "לפי הציוצים", בלי הסבר על מקורות, ובלי משפטי הגנה.

## נקודות מרכזיות

כתוב 6 עד 7 נקודות בלבד.
כל נקודה תהיה בוליט אחד:
• **כותרת קצרה:** 2 עד 3 משפטים.

כל בוליט חייב לכלול:
- מה קרה או מה בלט.
- למה זה חשוב לשוק / לסקטור / לטיקר.

אסור שכל בוליט יסתיים בהסתייגות.
אם צריך זהירות, שלב אותה בקצרה בתוך המשפט ואל תהפוך אותה למשפט הגנה.

סדר עדיפות:
1. האירוע המיקרו המרכזי ביותר.
2. טיקרים עם אירוע ברור.
3. ETFים, זרימות, מוצרים ממונפים, סקטורים.
4. תנודתיות/מדדים רק אם יש משמעות ברורה.
5. מאקרו רק אם הוא קשור ישירות לשוק.

## שורה תחתונה

כתוב 3 עד 4 משפטים קצרים ומקצועיים.
סכם את הנושא המרכזי, את הסקטור או הטיקרים שבמוקד, ואת מה שחשוב למשקיע לעקוב אחריו בהמשך.
אל תכתוב תחזית נחרצת ואל תיתן המלצת השקעה.
אל תיצור סעיף בשם "מה דורש מעקב".

בסוף כתוב בדיוק:

⚠️ גילוי נאות: תוכן זה נוצר באמצעות AI לצרכים אינפורמטיביים בלבד. אין באמור ייעוץ השקעות או המלצה לפעולה בניירות ערך.

פותח ע"י דורון שרייבמן

בדיקת איכות פנימית לפני שאתה מחזיר תשובה:
- מחק כל סעיף שנשמע כמו כותרת חדשותית בלי משמעות שוקית.
- מחק אזכור ל-Finnhub או למקור הנתונים הטכני.
- ודא שאין הסתייגות בסוף כל סעיף, אלא רק היכן שבאמת צריך.
- ודא שאין יותר מ-7 נקודות מרכזיות.
- ודא שכל סעיף זורם כסקירת שוק ולא כסיכום ציוץ.
- ודא שלא נוצר סעיף בשם "מה דורש מעקב".
- ודא שכותרת המשנה והפתיח תואמים לזמן ההרצה: טרום מסחר, זמן מסחר, אחרי נעילה, שבת, ראשון או חג/יום ללא מסחר.

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
        "review_context": get_review_context(datetime.now(ZoneInfo("Asia/Jerusalem"))),
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
