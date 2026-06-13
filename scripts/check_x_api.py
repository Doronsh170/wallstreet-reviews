import os
import re
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Any, Dict, List

import requests

TWITTER_BASE = "https://api.twitterapi.io"
OPENAI_BASE = "https://api.openai.com/v1/responses"

ACCOUNTS_FILE = Path("accounts.txt")
OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

TWITTER_API_KEY = os.environ.get("TWITTER_API_KEY", "").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.5").strip()

MAX_TWEETS_PER_ACCOUNT = int(os.environ.get("MAX_TWEETS_PER_ACCOUNT", "4"))
MAX_TWEETS_FOR_REVIEW = int(os.environ.get("MAX_TWEETS_FOR_REVIEW", "10"))

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
        "breaking",
        "just in",
        "earnings",
        "guidance",
        "upgrade",
        "downgrade",
        "price target",
        "merger",
        "acquisition",
        "contract",
        "export",
        "doj",
        "fed",
        "cpi",
        "pce",
        "yield",
        "oil",
        "iran",
        "ai",
        "space",
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


def extract_openai_text(data: Dict[str, Any]) -> str:
    if isinstance(data.get("output_text"), str):
        return data["output_text"]

    parts = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if isinstance(content, dict) and "text" in content:
                parts.append(content["text"])

    return "\n".join(parts)


def call_openai(tweets: List[Dict[str, Any]]) -> str:
    tweets_text = build_tweets_text(tweets)

    now_il = datetime.now(ZoneInfo("Asia/Jerusalem"))
    generated_date = now_il.strftime("%Y-%m-%d")

    prompt = f"""
אתה כותב סקירת וול סטריט מקצועית בעברית עבור איש שוק הון מנוסה.

הסקירה מבוססת אך ורק על הציוצים שסופקו לך.
אסור להוסיף מידע חיצוני, להשלים פערים מהידע הכללי שלך, או להסיק מסקנות שאינן נתמכות בציוצים.

המטרה:
להפוך זרם ציוצים לסקירה קצרה, קריאה ומקצועית, במבנה דומה לדף "הכנה ליום מסחר":
כותרת נקייה, תת כותרת, כרטיס נקודות מרכזיות, בולטים קצרים וברורים, גילוי נאות וקרדיט.

סגנון מבוקש:
- עברית מקצועית וזורמת.
- RTL.
- משפטים קצרים עד בינוניים.
- דף קריא של נקודות מרכזיות, לא מחקר ארוך.
- לא טבלה.
- לא דוח ציות.
- לא כותרות משנה רבות.
- לא לחזור בכל סעיף על "לפי הציוצים שנאספו".
- להשתמש ב"לפי הציוצים שנאספו" רק פעם אחת בפתיחה, או כאשר ממש נדרש להזכיר מגבלת מקור.
- כל נקודה צריכה לתת ערך מיידי לקורא.
- כתיבה חדה, לא שיווקית ולא דרמטית.

כללי אמינות:
- אל תוסיף מידע שלא מופיע בציוצים.
- אל תהפוך דעה לעובדה.
- אל תהפוך שמועה לעובדה.
- אל תכתוב "בגלל" אם אין קשר סיבתי ברור.
- אל תיתן המלצת השקעה.
- אל תציג ציוצים בודדים כתמונת שוק מלאה.
- אם מידע חריג, ספקולטיבי או לא מאומת, ציין זאת באופן טבעי בתוך הסעיף.
- אל תשתמש בטיקרים שלא הופיעו בציוצים.
- לעולם אל תחליף טיקרים ל-$1. שמור טיקרים בדיוק כפי שהם מופיעים, למשל $TSLA, $IBIT, $SPCX.
- אם אינך בטוח מה הטיקר, כתוב את שם החברה או הנכס בלי להמציא טיקר.

מבנה חובה לפלט:

# 🌅 טעימת וול סטריט

נקודות חשובות לקראת/אחרי המסחר בוול סטריט, {generated_date}

## נקודות מרכזיות

כתוב 7 עד 10 נקודות בולטות בלבד.
כל נקודה תהיה Bullet שמתחיל בכותרת מודגשת, ואז הסבר של 2 עד 3 משפטים.

מבנה כל נקודה:
• **כותרת קצרה:** הסבר קצר, מקצועי וזורם. אם יש טיקרים, שלב אותם בתוך המשפט. אם יש מגבלת ודאות, ציין אותה טבעית במשפט האחרון.

סדר מומלץ לנקודות:
1. תמונת מצב כללית שעלתה מהציוצים.
2. מאקרו / סנטימנט, רק אם באמת הופיע ורלוונטי.
3. סקטור מרכזי או נושא מוביל.
4. 3 עד 5 טיקרים מרכזיים, כל אחד רק אם יש סביבו אירוע ברור.
5. אירוע חריג או הייפ חריג, אם קיים.
6. נקודת זהירות מרכזית.
7. מה לעקוב בהמשך.

הנחיות לעריכה:
- אל תכתוב פרקים נפרדים כמו "מאקרו", "סקטורים", "מניות", "נרטיב".
- הכל חייב להופיע תחת "נקודות מרכזיות".
- אל תכתוב "רמת ודאות: גבוהה/בינונית/נמוכה". במקום זאת שלב את רמת הוודאות בניסוח טבעי.
- אל תשתמש ביותר מדי הסתייגויות.
- אם יש שמועה, כתוב: "מדובר באינדיקציה/שמועה ולא באישור רשמי".
- אם יש נתון חריג, כתוב: "דורש אימות נוסף".
- אם אין מספיק מידע בנושא מסוים, אל תנפח.
- אל תכניס כל טיקר שהופיע. בחר רק את החשובים ביותר.
- העדף נקודות מיקרו: חברות, טיקרים, סקטורים ואירועים נקודתיים.
- המאקרו יופיע רק כרקע אם הוא עלה בציוצים.

דוגמאות לכותרות טובות:
• **סנטימנט מעורב בטכנולוגיה:**
• **קריפטו תחת לחץ:**
• **$TSLA במוקד:**
• **$IBIT וזרימות הקריפטו:**
• **SpaceX ממשיכה לרכז עניין חריג:**
• **רגולציית AI חוזרת לכותרות:**
• **מה לעקוב בהמשך:**

בסוף הסקירה כתוב בדיוק:

⚠️ גילוי נאות: תוכן זה נוצר באמצעות AI לצרכים אינפורמטיביים בלבד. אין באמור ייעוץ השקעות או המלצה לפעולה בניירות ערך.

פותח ע"י דורון שרייבמן

הציוצים:
{tweets_text}
"""

    payload = {
        "model": OPENAI_MODEL,
        "input": prompt,
        "max_output_tokens": 1600,
    }

    response = requests.post(
        OPENAI_BASE,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=180,
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

    return text


def main() -> None:
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    accounts = read_accounts()

    all_tweets = []
    for account in accounts:
        print(f"Fetching @{account}...")
        all_tweets.extend(fetch_tweets_for_account(account))

    selected = select_tweets(all_tweets)

    print(f"Total raw tweets: {len(all_tweets)}")
    print(f"Selected tweets: {len(selected)}")

    review = call_openai(selected)

    input_json = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "model": OPENAI_MODEL,
        "total_raw_tweets": len(all_tweets),
        "selected_tweets": selected,
        "detected_cashtags": sorted(
            set(tag for tweet in selected for tag in tweet.get("cashtags", []))
        ),
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

    # Files used by GitHub Pages. The website fetches these automatically.
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
    print(review[:1000])


if __name__ == "__main__":
    main()
