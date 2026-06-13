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

הסקירה מבוססת רק על הציוצים שסופקו. אסור להוסיף מידע חיצוני, להשלים פערים מהידע הכללי שלך, או להסיק מסקנות שאינן נתמכות בטקסט.

המטרה:
להפוך זרם ציוצים לדף קריא בסגנון “הכנה ליום מסחר”: כותרת נקייה, פתיח קצר, כרטיס נקודות מרכזיות, גילוי נאות וקרדיט.

עקרונות כתיבה:
- לכתוב כמו איש שוק, לא כמו דוח ציות.
- לא להשתמש בביטוי "לפי הציוצים שנאספו".
- לא להשתמש בביטוי "לפי הטענות".
- במקום זאת השתמש בניסוחים טבעיים: "עלה דיווח", "דווח כי", "במוקד עמד", "הנתון המרכזי", "עוד בלט", "נקודת המעקב".
- אם מדובר בשמועה או שוק תחזיות, כתוב במפורש: "זו אינדיקציה/שמועה ולא הודעה רשמית".
- אם מדובר בנתון שוק שפורסם בציוץ, כתוב אותו ישיר וברור. אל תחליש אותו במילים כמו "טענות".
- אם נתון נראה חריג מאוד, אפשר להוסיף בסוף המשפט: "הנתון דורש אימות מול מקור שוק רשמי".
- לא לחזור שוב ושוב על הסתייגויות.
- לא לייצר פרקים רבים. הכול תחת נקודות מרכזיות.
- לא לכתוב טבלאות.
- לא לתת המלצת השקעה.

כללי איכות מחייבים:
1. אל תהפוך דעה לעובדה.
2. אל תהפוך שמועה לעובדה.
3. אל תכתוב "בגלל" אם אין קשר סיבתי ברור.
4. אל תשתמש בטיקרים שלא הופיעו בציוצים.
5. לעולם אל תחליף טיקרים ל-$1. שמור טיקרים בדיוק כפי שהם מופיעים, למשל $TSLA, $IBIT, $SPCX, $VXN.
6. אם הקשר בין נתון לבין הסקירה לא ברור, אל תכניס אותו. לדוגמה: נתון על $VXN או תנודתיות יופיע רק אם אתה מסביר בקצרה שזה רקע לתנודתיות בטכנולוגיה, ולא קשר סיבתי למניות ספציפיות.
7. אם אין מספיק מידע כדי להסביר נתון, עדיף להשמיט אותו מאשר ליצור משפט לא ברור.
8. אל תכניס כל טיקר שהופיע. בחר רק נושאים שמייצרים ערך לקורא.

סגנון:
- עברית מקצועית, RTL.
- משפטים קצרים עד בינוניים.
- חד, ברור, לא דרמטי, לא שיווקי.
- כל נקודה 2 עד 3 משפטים בלבד.
- כותרת כל נקודה תהיה מודגשת וברורה.

מבנה פלט חובה:

# 🌅 טעימת וול סטריט

נקודות חשובות לקראת/אחרי המסחר בוול סטריט, {generated_date}

פתיח קצר של 2 עד 3 משפטים. בלי הביטוי "לפי הציוצים שנאספו". הפתיח יסביר מה מוקדי העניין העיקריים שעולים מהמקורות, בלי להציג זאת כתמונת שוק מלאה.

## נקודות מרכזיות

כתוב 6 עד 8 נקודות בלבד.
כל נקודה תתחיל בבוליט ובכותרת מודגשת:
• **כותרת קצרה:** טקסט של 2 עד 3 משפטים.

סדר עדיפות לנקודות:
1. תמונת מצב כללית או סנטימנט, רק אם יש ערך.
2. מאקרו/תנודתיות/רגולציה, רק אם ברור למה זה חשוב.
3. SpaceX / $SPCX אם הופיע.
4. ETFים או מוצרים ממונפים אם הופיעו.
5. טיקרים מרכזיים נוספים עם אירוע ברור.
6. קריפטו/Bitcoin ETF אם הופיע.
7. M&A / מדיה אם הופיע.
8. נקודת מעקב להמשך.

הנחיות לניסוח נושאים רגישים:
- לגבי שווי שוק של $SPCX: אם מופיע בציוצים, כתוב "דווח על שווי שוק של מעל..." או "הנתון המרכזי סביב $SPCX היה שווי שוק של...". אל תכתוב "לפי הטענות".
- לגבי $VXN או תנודתיות: הסבר במשפט אחד שזה מדד תנודתיות של Nasdaq 100, ואם הקשר לא מוסיף ערך, השמט את הנתון.
- לגבי Kalshi: ציין שזה שוק תחזיות, לא הודעת חברה.
- לגבי ETF ממונף: ציין שזה מוצר שמגביר עניין וסיכון תנודתיות, ולא המלצה.
- לגבי נתון שלא ברור או מבלבל: השמט.

בסוף הסקירה כתוב בדיוק:

⚠️ גילוי נאות: תוכן זה נוצר באמצעות AI לצרכים אינפורמטיביים בלבד. אין באמור ייעוץ השקעות או המלצה לפעולה בניירות ערך.

פותח ע"י דורון שרייבמן

הציוצים:
{tweets_text}
"""

    payload = {
        "model": OPENAI_MODEL,
        "input": prompt,
        "max_output_tokens": 2600,
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
