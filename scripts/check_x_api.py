import os
import re
import json
from datetime import datetime, timezone
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

    prompt = f"""
אתה כותב סקירת וול סטריט מקצועית בעברית עבור איש שוק הון מנוסה.

הסקירה מבוססת אך ורק על הציוצים שסופקו לך. אסור להוסיף מידע חיצוני, להשלים פערים מהידע הכללי שלך, או להסיק מסקנות שאינן נתמכות בציוצים.

מטרת הסקירה:
להפוך זרם ציוצים ממקורות X לסקירה מקצועית, קריאה, עניינית וממוקדת שוק.

הדגש האנליטי:
- כ-30% מאקרו
- כ-70% מיקרו: חברות, סקטורים, טיקרים, אירועים נקודתיים
- המיקרו הוא מרכז הסקירה
- המאקרו יופיע רק אם הוא רלוונטי למניות, סקטורים או סנטימנט שוק

כללי אמינות:
1. אל תוסיף מידע שלא מופיע בציוצים.
2. אל תהפוך דעה לעובדה.
3. אל תהפוך שמועה לעובדה.
4. אל תכתוב "בגלל" אם אין קשר סיבתי ברור.
5. אל תכתוב תחזית כיוונית נחרצת.
6. אל תיתן המלצת השקעה.
7. אל תהפוך ציוץ בודד לנרטיב שוק רחב.
8. אם מידע דורש אימות נוסף, ציין זאת.
9. אם יש פער בין ציוצים, ציין זאת.
10. הטיקרים בסקירה ייקבעו רק לפי הציוצים שסופקו.

חשוב:
הסקירה צריכה להיות קריאה וזורמת, לא דוח ציות ולא רשימת הסתייגויות.
השתמש בהסתייגויות רק כאשר יש צורך אמיתי:
- שמועה
- נתון לא מאומת
- פער בין מקורות
- סיבתיות לא ברורה
- הייפ חריג סביב טיקר
- מידע שמבוסס על שוק תחזיות או מקור לא רשמי

סגנון:
- עברית מקצועית
- חד וברור
- לא שיווקי
- לא דרמטי מדי
- מתאים לאיש שוק הון
- לא ארוך מדי
- בלי מריחות
- בלי המלצות השקעה

ניסוחים מועדפים:
- "לפי הציוצים שנאספו"
- "על פי המקורות שסופקו"
- "הדיווח מצביע על"
- "המשמעות הזהירה היא"
- "נדרש אימות נוסף"
- "בשלב זה לא ניתן להסיק ש..."
- "מדובר באינדיקציה, לא באישור רשמי"
- "הנושא עלה כמוקד עניין, אך דורש בדיקה נוספת"

ניסוחים שיש להימנע מהם:
- "השוק ירד בגלל..."
- "ברור ש..."
- "אין ספק ש..."
- "המניה צפויה..."
- "זו הוכחה לכך ש..."
- "המשקיעים יודעים ש..."
- "האירוע מוכיח ש..."

מבנה חובה:

# טעימת וול סטריט, סקירה לפי דרישה

## 1. תמונת מצב
כתוב 4-6 משפטים.
פתח במילים: "לפי הציוצים שנאספו..."
ציין מהם 3-5 הנושאים המרכזיים שעלו.
אל תציג את זה כתמונת השוק המלאה, אלא כתמונה שעולה מהמקורות שסופקו.

## 2. מאקרו רלוונטי
כתוב רק על נושאי מאקרו שעלו בציוצים ורלוונטיים לשוק.
אם אין מספיק מאקרו בציוצים, כתוב זאת בקצרה ואל תמציא מאקרו.
הצג את המאקרו כרקע בלבד, אלא אם הוא הנושא המרכזי בציוצים.

לכל נושא מאקרו מרכזי כתוב בפסקה קצרה:
- מה עלה בציוצים
- למה זה רלוונטי לשוק
- מה דורש אימות או מעקב נוסף, אם יש

## 3. סקטורים במוקד
בחר 2-5 סקטורים שעלו מהציוצים.
לכל סקטור כתוב פסקה קצרה:
- מה עלה בציוצים
- אילו טיקרים קשורים
- למה זה חשוב
- האם מדובר בעובדה, דיווח, סנטימנט או שמועה, רק אם זה רלוונטי

אל תיצור סקטורים שלא עלו בציוצים.

## 4. מניות וטיקרים במוקד
בחר רק את הטיקרים החשובים ביותר מתוך הציוצים.
אל תכניס כל טיקר שהופיע.
תעדיף טיקרים עם:
- אירוע ברור
- חזרתיות בציוצים
- מעורבות גבוהה
- משמעות סקטוריאלית
- קשר לנרטיב מרכזי

לכל טיקר מרכזי השתמש במבנה הבא:

### $TICKER, שם החברה או הנכס אם ידוע
- מה עלה בציוצים:
- למה זה חשוב:
- רמת ודאות: גבוהה / בינונית / נמוכה
- נקודת זהירות: רק אם יש צורך אמיתי

אם מדובר בהייפ חריג, שוק תחזיות, שמועה או דיווח לא מאומת, כתוב זאת במפורש.

## 5. הנרטיב המרכזי שעולה מהמקורות
כתוב 2-4 פסקאות קצרות.
התחל כל פסקה במילים: "לפי הציוצים שנאספו..."
כתוב רק נרטיבים שחוזרים בכמה ציוצים או נראים מהותיים במיוחד.
אל תציג נרטיב כעובדה אם הוא מבוסס על סנטימנט או שמועה.

## 6. נקודות שדורשות זהירות
כתוב 3-5 נקודות בלבד.
התמקד ב:
- שמועות
- מידע לא מאומת
- סיבתיות לא מוכחת
- פערים בין ציוצים
- הייפ סביב טיקר
- נתונים שדורשים אימות חיצוני

אל תעמיס הסתייגויות מיותרות.

## 7. שורה תחתונה
כתוב 4-6 משפטים.
סכם מה באמת חשוב.
אל תיתן המלצת השקעה.
אל תכתוב תחזית.
סיים במשפט שמדגיש מה צריך לבדוק לפני מסקנה רחבה.

הציוצים:
{tweets_text}
"""

    payload = {
        "model": OPENAI_MODEL,
        "input": prompt,
        "max_output_tokens": 1800,
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

    Path(f"output/review_input_{run_ts}.json").write_text(
        json.dumps(input_json, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    Path(f"output/wallstreet_review_{run_ts}.md").write_text(
        review,
        encoding="utf-8",
    )

    print("Review created successfully.")
    print(review[:1000])


if __name__ == "__main__":
    main()
