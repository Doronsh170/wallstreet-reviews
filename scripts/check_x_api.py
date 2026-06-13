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

MAX_TWEETS_PER_ACCOUNT = int(os.environ.get("MAX_TWEETS_PER_ACCOUNT", "20"))
MAX_TWEETS_FOR_REVIEW = int(os.environ.get("MAX_TWEETS_FOR_REVIEW", "70"))

CASHTAG_RE = re.compile(r"(?<![A-Za-z0-9_])\$[A-Z]{1,6}(?![A-Za-z0-9_])")


if not TWITTER_API_KEY:
    raise SystemExit("Missing GitHub secret: TWITTER_API_KEY")

if not OPENAI_API_KEY:
    raise SystemExit("Missing GitHub secret: OPENAI_API_KEY")


def read_accounts() -> List[str]:
    if not ACCOUNTS_FILE.exists():
        raise SystemExit("accounts.txt not found")

    accounts = []
    for line in ACCOUNTS_FILE.read_text(encoding="utf-8").splitlines():
        account = line.strip().lstrip("@")
        if account and not account.startswith("#"):
            accounts.append(account)
    return accounts


def get_twitter_json(path: str, params: Dict[str, Any]) -> Dict[str, Any]:
    url = f"{TWITTER_BASE}{path}"
    response = requests.get(
        url,
        headers={"X-API-Key": TWITTER_API_KEY},
        params=params,
        timeout=40,
    )

    try:
        payload = response.json()
    except Exception:
        payload = {"raw_text": response.text[:2000]}

    payload["_http_status"] = response.status_code
    payload["_requested_url"] = response.url
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
        for key in ("tweets", "data", "items", "results", "timeline", "list"):
            if key in obj:
                found.extend(find_tweets(obj[key]))

        if not found:
            for value in obj.values():
                found.extend(find_tweets(value))

    dedup = {}
    for tweet in found:
        key = str(
            tweet.get("id")
            or tweet.get("tweetId")
            or tweet.get("url")
            or tweet.get("text")
            or tweet.get("fullText")
            or tweet.get("content")
        )
        dedup[key] = tweet

    return list(dedup.values())


def normalize_tweet(tweet: Dict[str, Any], account: str) -> Dict[str, Any]:
    author = tweet.get("author") if isinstance(tweet.get("author"), dict) else {}

    text = tweet.get("text") or tweet.get("fullText") or tweet.get("content") or ""
    text = text.replace("\n", " ").strip()

    return {
        "account": account,
        "author": author.get("userName") or account,
        "id": tweet.get("id") or tweet.get("tweetId"),
        "url": tweet.get("url") or tweet.get("twitterUrl"),
        "createdAt": tweet.get("createdAt") or tweet.get("created_at"),
        "text": text,
        "likeCount": tweet.get("likeCount") or 0,
        "retweetCount": tweet.get("retweetCount") or 0,
        "replyCount": tweet.get("replyCount") or 0,
        "quoteCount": tweet.get("quoteCount") or 0,
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
    score = 0.0

    cashtags = tweet.get("cashtags", [])
    if cashtags:
        score += 25

    text = (tweet.get("text") or "").lower()

    important_words = [
        "breaking",
        "just in",
        "earnings",
        "guidance",
        "downgrade",
        "upgrade",
        "price target",
        "pt",
        "merger",
        "acquisition",
        "doj",
        "sec",
        "contract",
        "export",
        "tariff",
        "fed",
        "cpi",
        "pce",
        "payrolls",
        "yield",
        "iran",
        "oil",
        "space",
        "ai",
        "semiconductor",
    ]

    for word in important_words:
        if word in text:
            score += 6

    views = tweet.get("viewCount") or 0
    likes = tweet.get("likeCount") or 0
    retweets = tweet.get("retweetCount") or 0

    if views >= 100000:
        score += 20
    elif views >= 25000:
        score += 10

    if likes >= 500:
        score += 10
    elif likes >= 100:
        score += 5

    if retweets >= 50:
        score += 10
    elif retweets >= 10:
        score += 5

    if tweet.get("is_retweet"):
        score -= 20

    return score


def select_tweets_for_review(tweets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    dedup = {}
    for tweet in tweets:
        key = str(tweet.get("id") or tweet.get("url") or tweet.get("text"))
        dedup[key] = tweet

    unique = list(dedup.values())

    scored = []
    for tweet in unique:
        scored.append((tweet_score(tweet), tweet))

    scored.sort(key=lambda x: x[0], reverse=True)

    selected = [tweet for _, tweet in scored[:MAX_TWEETS_FOR_REVIEW]]
    return selected


def build_tweets_text(tweets: List[Dict[str, Any]]) -> str:
    lines = []

    for i, tweet in enumerate(tweets, start=1):
        lines.append(f"Tweet {i}")
        lines.append(f"Source: @{tweet.get('author')}")
        lines.append(f"Time: {tweet.get('createdAt')}")
        lines.append(f"URL: {tweet.get('url')}")
        lines.append(f"Cashtags: {', '.join(tweet.get('cashtags', [])) or 'None'}")
        lines.append(
            f"Engagement: views={tweet.get('viewCount')}, "
            f"likes={tweet.get('likeCount')}, retweets={tweet.get('retweetCount')}"
        )
        lines.append(f"Text: {tweet.get('text')}")
        lines.append("")

    return "\n".join(lines)


def extract_openai_text(payload: Dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]

    parts = []

    for item in payload.get("output", []):
        for content in item.get("content", []):
            if isinstance(content, dict):
                if content.get("type") in ("output_text", "text"):
                    text = content.get("text")
                    if isinstance(text, str):
                        parts.append(text)

    if parts:
        return "\n".join(parts)

    return json.dumps(payload, ensure_ascii=False, indent=2)[:6000]


def call_openai(system_prompt: str, user_prompt: str) -> str:
    payload = {
        "model": OPENAI_MODEL,
        "input": [
            {
                "role": "system",
                "content": system_prompt,
            },
            {
                "role": "user",
                "content": user_prompt,
            },
        ],
    }

    response = requests.post(
        OPENAI_BASE,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=600,
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

    return extract_openai_text(data)


def write_review_with_openai(tweets: List[Dict[str, Any]]) -> str:
    tweets_text = build_tweets_text(tweets)

    system_prompt = """
אתה עורך סקירת וול סטריט מקצועית בעברית עבור איש שוק הון מנוסה.

המטרה: לנתח ציוצים ממקורות X אמינים, בלי לסלף, בלי להמציא, ובלי לייחס סיבתיות לא מוכחת.

כללי ברזל:
1. אסור להוסיף מידע שלא מופיע בציוצים שסופקו.
2. אסור להפוך דעה לעובדה.
3. אסור להפוך שמועה לעובדה.
4. אסור לכתוב "בגלל" אם אין קשר סיבתי ברור.
5. אסור לתת המלצת השקעה.
6. חובה להפריד בין עובדה, פרשנות, שמועה, סנטימנט ונרטיב.
7. אם משהו לא ודאי, כתוב שהוא לא ודאי.
8. הסקירה צריכה להיות 30% מאקרו ו-70% מיקרו.
9. המיקרו הוא העיקר: חברות, סקטורים, אירועים נקודתיים וטיקרים שעלו בציוצים.
10. הטיקרים נקבעים רק לפי הציוצים שסופקו, לא לפי רשימת מעקב קבועה.

סגנון:
עברית מקצועית, חדה, לא שיווקית, לא דרמטית מדי.
פנה לקורא מקצועי, לא לקהל כללי.
אל תמרח. תן ערך אנליטי.
"""

    user_prompt = f"""
כתוב סקירת וול סטריט לפי דרישה על בסיס הציוצים הבאים.

מבנה חובה:

# טעימת וול סטריט, סקירה לפי דרישה

## 1. תמונת מצב קצרה
סכם בקצרה את הנושאים המרכזיים שעולים מהציוצים.

## 2. מאקרו, כ-30%
רק מה שרלוונטי למניות, סקטורים או סנטימנט שוק.
הפרד בין עובדה לבין פרשנות.
אל תרחיב במאקרו אם אין לו השפעה ברורה על חברות או סקטורים.

## 3. סקטורים במוקד
זהה 3 עד 6 סקטורים שעלו מהציוצים.
לכל סקטור כתוב:
- מה נאמר בפועל
- מה המשמעות הזהירה
- מה לא ידוע עדיין

## 4. מניות במוקד
בחר את הטיקרים החשובים ביותר מתוך הציוצים.
לכל טיקר כתוב:
- מה נאמר בפועל
- האם זו עובדה, פרשנות, שמועה או סנטימנט
- למה זה חשוב
- ניסוח זהיר של המשמעות

## 5. נרטיב שעולה מהמקורות ב-X
רק נרטיבים שחוזרים בכמה ציוצים או נראים מהותיים.
אל תהפוך ציוץ בודד לנרטיב שוק רחב.

## 6. נקודות שדורשות זהירות
ציין במפורש שמועות, טענות לא מאומתות, טיקרים עם הייפ, או סיבתיות לא מוכחת.

## 7. שורה תחתונה
סיכום מקצועי קצר.
לא המלצת השקעה.

להלן הציוצים:

{tweets_text}
"""

    draft = call_openai(system_prompt, user_prompt)

    quality_system_prompt = """
אתה עורך בקרת איכות לסקירת שוק הון.
התפקיד שלך הוא לזהות סילופים, סיבתיות לא מוכחת, ניסוחים נחרצים מדי, והוספת מידע שלא הופיע במקורות.

אם משפט אינו נתמך בציוצים, תקן או הסר אותו.
אם נכתב "בגלל" בלי בסיס ברור, החלף ל"ברקע", "לפי המקורות", "הציוצים מצביעים על", או "ייתכן".
אם דעה מוצגת כעובדה, תקן.
אם שמועה מוצגת כעובדה, תקן.
שמור על עברית מקצועית.
"""

    quality_user_prompt = f"""
בדוק את הסקירה הבאה מול הציוצים המקוריים.

החזר גרסה מתוקנת בלבד, מוכנה לקריאה.

ציוצים מקוריים:
{tweets_text}

סקירה לבדיקה:
{draft}
"""

    checked = call_openai(quality_system_prompt, quality_user_prompt)
    return checked


def main() -> None:
    run_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    generated_utc = datetime.now(timezone.utc).isoformat()

    accounts = read_accounts()

    all_tweets = []
    status_by_account = {}

    for account in accounts:
        print(f"Fetching @{account}...")
        tweets = fetch_tweets_for_account(account)
        status_by_account[account] = {
            "tweet_count_received": len(tweets),
            "cashtags": sorted(set(tag for t in tweets for tag in t.get("cashtags", []))),
        }
        all_tweets.extend(tweets)

    selected_tweets = select_tweets_for_review(all_tweets)

    all_cashtags = sorted(set(tag for t in selected_tweets for tag in t.get("cashtags", [])))

    print(f"Total raw tweets: {len(all_tweets)}")
    print(f"Selected tweets for review: {len(selected_tweets)}")
    print(f"Cashtags: {', '.join(all_cashtags)}")

    review_md = write_review_with_openai(selected_tweets)

    json_result = {
        "generated_utc": generated_utc,
        "source_api": "twitterapi.io",
        "openai_model": OPENAI_MODEL,
        "accounts": accounts,
        "total_raw_tweets": len(all_tweets),
        "selected_tweets_for_review": len(selected_tweets),
        "detected_cashtags": all_cashtags,
        "status_by_account": status_by_account,
        "selected_tweets": selected_tweets,
    }

    json_path = OUTPUT_DIR / f"wallstreet_review_input_{run_ts}.json"
    review_path = OUTPUT_DIR / f"wallstreet_review_{run_ts}.md"

    json_path.write_text(
        json.dumps(json_result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    review_path.write_text(review_md, encoding="utf-8")

    print(f"Saved input JSON: {json_path}")
    print(f"Saved review Markdown: {review_path}")
    print("Done.")


if __name__ == "__main__":
    main()
