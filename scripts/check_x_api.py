import json
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import requests

TWITTER_BASE = "https://api.twitterapi.io"
OPENAI_BASE = "https://api.openai.com/v1/responses"
FINNHUB_BASE = "https://finnhub.io/api/v1"

REVIEW_TYPE = os.environ.get("REVIEW_TYPE", "wallstreet").strip().lower()
if REVIEW_TYPE != "wallstreet":
    raise SystemExit("Core rebuild v1 supports only REVIEW_TYPE=wallstreet. Israel/Trump will be rebuilt later.")

TWITTER_API_KEY = os.environ.get("TWITTER_API_KEY", "").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.5").strip()
MAX_TWEETS_PER_ACCOUNT = int(os.environ.get("MAX_TWEETS_PER_ACCOUNT", "4"))
MAX_TWEETS_FOR_REVIEW = int(os.environ.get("MAX_TWEETS_FOR_REVIEW", "12"))
MAX_OUTPUT_TOKENS = int(os.environ.get("OPENAI_MAX_OUTPUT_TOKENS", "5500"))

if not TWITTER_API_KEY:
    raise SystemExit("Missing GitHub secret: TWITTER_API_KEY")
if not OPENAI_API_KEY:
    raise SystemExit("Missing GitHub secret: OPENAI_API_KEY")

ROOT = Path(".")
SOURCES_FILE = Path("sources/wallstreet.txt")
OUTPUT_DIR = Path("output/wallstreet")
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_ROOT = Path("output")
OUTPUT_ROOT.mkdir(exist_ok=True)

CASHTAG_RE = re.compile(r"(?<![A-Za-z0-9_])\$([A-Z]{1,6})(?![A-Za-z0-9_])")
HEBREW_RE = re.compile(r"[א-ת]")

FORBIDDEN_PHRASES = [
    "לפי הציוץ", "ציוץ נוסף", "הציוץ", "ציוץ", "לפי פוסט", "הפוסט",
    "הוזכר", "דווח כי", "לפי דיווח", "לפי המקור", "נאמר כי", "מקור",
    "###", "##", "**", "נקודה 1", "נקודה 2", "נקודה 3", "נקודה 4", "נקודה 5",
    "יום המסחר הבא", "שכבת ערך", "מסגרת חדשה", "שיח ביטחוני", "עדשת ביטחון לאומי",
    "נכסי צמיחה עתירי נרטיב", "תמחור נרטיבי", "מוקד נרטיבי", "אס אנד פי"
]

BANNED_PREFIX_RE = re.compile(r"^\s*([A-Za-z$]|\([A-Za-z$])")
BAD_NUMERIC_PATTERNS = [
    re.compile(r"\d+\.\s+\d+"),
    re.compile(r"סביב\s+0\."),
    re.compile(r"\b0\.\s*(?:$|[א-ת])"),
]
FUTURES_PERCENT_RE = re.compile(r"חוז(?:ה|ים|י)[^\.\n]{0,70}\d+(?:\.\d+)?\s*%")

# Phrases that sound like generic market commentary rather than real market mechanism.
# These are intentionally narrow: they block the bad style we saw without rejecting every normal sentence.
SHALLOW_MARKET_PHRASES = [
    "השוק עשוי להמשיך להעדיף מניות על פני נפט",
    "השוק מעדיף מניות על פני נפט",
    "המשקיעים יעדיפו מניות",
    "השוק יאהב את זה",
    "זה חיובי לשוק",
    "זה שלילי לשוק",
    "זה רע לסנטימנט",
    "זה טוב לסנטימנט",
    "השווקים מחכים לראות",
]

GEOPOLITICAL_TERMS = ["איראן", "הורמוז", "מיצרי הורמוז", "לבנון", "ביירות", "הפסקת אש", "מלחמה", "תקיפה", "הסכם"]
MARKET_MECHANISM_TERMS = ["נפט", "אינפלציה", "תשואות", "ריבית", "דולר", "חוזים", "מדדים", "סנטימנט", "פרמיית סיכון"]


def has_geo_context(text: str) -> bool:
    return any(t in text for t in GEOPOLITICAL_TERMS)


def has_market_mechanism(text: str) -> bool:
    hits = sum(1 for t in MARKET_MECHANISM_TERMS if t in text)
    return hits >= 2


def read_accounts() -> List[str]:
    if not SOURCES_FILE.exists():
        raise SystemExit(f"Missing sources file: {SOURCES_FILE}")
    accounts: List[str] = []
    for line in SOURCES_FILE.read_text(encoding="utf-8").splitlines():
        x = line.strip().lstrip("@")
        if x and not x.startswith("#"):
            accounts.append(x)
    if not accounts:
        raise SystemExit("No Wall Street sources configured")
    return accounts


def http_json(url: str, *, headers=None, params=None, timeout=35) -> Dict[str, Any]:
    r = requests.get(url, headers=headers or {}, params=params or {}, timeout=timeout)
    try:
        data = r.json()
    except Exception:
        data = {"raw_text": r.text[:1000]}
    data["_http_status"] = r.status_code
    return data


def looks_like_tweet(x: Any) -> bool:
    return isinstance(x, dict) and isinstance(x.get("text") or x.get("fullText") or x.get("content"), str)


def find_tweets(obj: Any) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    if looks_like_tweet(obj):
        out.append(obj)
    elif isinstance(obj, list):
        for item in obj:
            out.extend(find_tweets(item))
    elif isinstance(obj, dict):
        for key in ("tweets", "data", "items", "results"):
            if key in obj:
                out.extend(find_tweets(obj[key]))
        if not out:
            for v in obj.values():
                out.extend(find_tweets(v))
    dedup: Dict[str, Dict[str, Any]] = {}
    for t in out:
        key = str(t.get("id") or t.get("url") or t.get("text") or "")
        if key:
            dedup[key] = t
    return list(dedup.values())


def normalize_tweet(tweet: Dict[str, Any], account: str) -> Dict[str, Any]:
    author = tweet.get("author") if isinstance(tweet.get("author"), dict) else {}
    text = tweet.get("text") or tweet.get("fullText") or tweet.get("content") or ""
    text = re.sub(r"\s+", " ", str(text)).strip()
    cashtags = sorted(set(m.group(1).upper() for m in CASHTAG_RE.finditer(text)))
    return {
        "account": account,
        "author": author.get("userName") or account,
        "id": tweet.get("id"),
        "url": tweet.get("url") or tweet.get("twitterUrl"),
        "createdAt": tweet.get("createdAt") or tweet.get("created_at") or tweet.get("date"),
        "text": text,
        "likeCount": int(tweet.get("likeCount") or 0),
        "retweetCount": int(tweet.get("retweetCount") or 0),
        "viewCount": int(tweet.get("viewCount") or 0),
        "cashtags": cashtags,
        "is_retweet": text.startswith("RT @"),
    }


def fetch_tweets_for_account(account: str) -> List[Dict[str, Any]]:
    data = http_json(
        f"{TWITTER_BASE}/twitter/user/last_tweets",
        headers={"X-API-Key": TWITTER_API_KEY},
        params={"userName": account, "cursor": "", "includeReplies": "false"},
    )
    print(f"@{account}: status={data.get('_http_status')}")
    tweets = [normalize_tweet(t, account) for t in find_tweets(data)]
    print(f"  found={len(tweets)}")
    return tweets[:MAX_TWEETS_PER_ACCOUNT]


def tweet_score(tweet: Dict[str, Any]) -> float:
    text = (tweet.get("text") or "").lower()
    score = 0.0
    if tweet.get("cashtags"):
        score += 30
    keywords = [
        "breaking", "just in", "fed", "fomc", "cpi", "ppi", "pce", "jobs", "payrolls", "claims",
        "earnings", "guidance", "upgrade", "downgrade", "price target", "contract", "ipo", "spacex",
        "ai", "anthropic", "nvidia", "apple", "google", "tesla", "semiconductor", "chips",
        "oil", "gold", "yield", "treasury", "dollar", "iran", "tariff", "futures", "nasdaq", "s&p",
    ]
    for k in keywords:
        if k in text:
            score += 7
    views = int(tweet.get("viewCount") or 0)
    likes = int(tweet.get("likeCount") or 0)
    if views >= 100000:
        score += 15
    elif views >= 25000:
        score += 8
    if likes >= 500:
        score += 10
    elif likes >= 100:
        score += 5
    if tweet.get("is_retweet"):
        score -= 25
    return score


def select_tweets(tweets: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    dedup: Dict[str, Dict[str, Any]] = {}
    for t in tweets:
        key = str(t.get("id") or t.get("url") or t.get("text"))
        dedup[key] = t
    scored = sorted(((tweet_score(t), t) for t in dedup.values()), key=lambda x: x[0], reverse=True)
    return [t for _, t in scored[:MAX_TWEETS_FOR_REVIEW]]


def build_source_digest(tweets: List[Dict[str, Any]]) -> str:
    lines: List[str] = []
    for i, t in enumerate(tweets, start=1):
        lines.append(f"Source item {i}")
        lines.append(f"Time: {t.get('createdAt') or 'unknown'}")
        lines.append(f"Cashtags: {', '.join(t.get('cashtags') or []) or 'None'}")
        lines.append(f"Text: {t.get('text')}")
        lines.append("")
    return "\n".join(lines)


def collect_symbols(tweets: List[Dict[str, Any]]) -> List[str]:
    counts: Dict[str, int] = {}
    exclude = {"SPX", "NDX", "DJI", "VIX", "DXY", "USD", "AI", "IPO", "ETF", "CEO", "CFO", "USA", "GDP", "CPI", "PPI"}
    for t in tweets:
        for s in t.get("cashtags") or []:
            if s not in exclude:
                counts[s] = counts.get(s, 0) + 1
    return [s for s, _ in sorted(counts.items(), key=lambda x: x[1], reverse=True)[:10]]


def safe_float(x: Any) -> Optional[float]:
    try:
        if x is None:
            return None
        return float(x)
    except Exception:
        return None


def finnhub_get(path: str, params: Dict[str, Any]) -> Optional[Any]:
    if not FINNHUB_API_KEY:
        return None
    p = dict(params)
    p["token"] = FINNHUB_API_KEY
    try:
        r = requests.get(f"{FINNHUB_BASE}{path}", params=p, timeout=15)
        if not r.ok:
            return None
        return r.json()
    except Exception:
        return None


def fetch_market_facts(symbols: List[str]) -> Dict[str, Any]:
    facts: Dict[str, Any] = {
        "note": "Only these numbers are verified by the code. Do not invent other percentages. Futures percentages are not verified in v1.",
        "futures_percent_verified": False,
        "symbols": {},
    }
    if not FINNHUB_API_KEY:
        facts["finnhub_enabled"] = False
        return facts
    facts["finnhub_enabled"] = True
    for s in symbols:
        q = finnhub_get("/quote", {"symbol": s})
        if isinstance(q, dict):
            price = safe_float(q.get("c"))
            pct = safe_float(q.get("dp"))
            prev = safe_float(q.get("pc"))
            if price and price > 0:
                facts["symbols"][s] = {"price": round(price, 4), "change_pct": pct, "previous_close": prev}
    # Broad ETFs are validation context only. They are not futures.
    for s in ["SPY", "QQQ", "DIA", "IWM", "USO", "GLD", "TLT", "UUP"]:
        if s not in facts["symbols"]:
            q = finnhub_get("/quote", {"symbol": s})
            if isinstance(q, dict):
                price = safe_float(q.get("c"))
                pct = safe_float(q.get("dp"))
                if price and price > 0:
                    facts["symbols"][s] = {"price": round(price, 4), "change_pct": pct, "validation_only": True}
    return facts


def facts_text(facts: Dict[str, Any]) -> str:
    lines = [
        "MARKET FACTS, verified by code:",
        "- Futures percentages are NOT verified in this rebuild version. Do not write any futures percentage.",
        "- If discussing futures, use direction only, e.g. החוזים עולים בחדות / החוזים יורדים / החוזים יציבים.",
        "- Do not copy ETF percentages as futures percentages.",
    ]
    if not facts.get("finnhub_enabled"):
        lines.append("- Finnhub is not enabled. Avoid exact prices and exact daily changes unless they appear in source text and are central.")
        return "\n".join(lines)
    for s, d in (facts.get("symbols") or {}).items():
        pct = d.get("change_pct")
        price = d.get("price")
        suffix = " (validation only, not futures)" if d.get("validation_only") else ""
        if pct is not None:
            lines.append(f"- {s}: price {price}, daily change {pct:.2f}%{suffix}")
        else:
            lines.append(f"- {s}: price {price}{suffix}")
    return "\n".join(lines)


def hebrew_weekday(dt: datetime) -> str:
    names = {0: "יום שני", 1: "יום שלישי", 2: "יום רביעי", 3: "יום חמישי", 4: "יום שישי", 5: "שבת", 6: "יום ראשון"}
    return names.get(dt.weekday(), "")


def get_review_context(now_il: datetime) -> Dict[str, str]:
    now_ny = now_il.astimezone(ZoneInfo("America/New_York"))
    date_str = now_il.strftime("%Y-%m-%d")
    day_il = hebrew_weekday(now_il)
    if now_il.weekday() == 5:
        return {"mode": "weekend", "subtitle": f"סיכום שבוע בוול סטריט והיערכות לפתיחת השבוע, {day_il} {date_str}", "forward_title": "לקראת השבוע הקרוב"}
    if now_il.weekday() == 6:
        return {"mode": "week_start", "subtitle": f"היערכות לפתיחת שבוע המסחר בוול סטריט, {day_il} {date_str}", "forward_title": "לקראת המסחר הקרוב"}
    market_open = now_ny.replace(hour=9, minute=30, second=0, microsecond=0)
    market_close = now_ny.replace(hour=16, minute=0, second=0, microsecond=0)
    if now_ny < market_open:
        return {"mode": "premarket", "subtitle": f"נקודות חשובות לקראת פתיחת המסחר בוול סטריט, {day_il} {date_str}", "forward_title": "לקראת המסחר הקרוב"}
    if market_open <= now_ny <= market_close:
        return {"mode": "intraday", "subtitle": f"עדכון בזמן מסחר בוול סטריט, {day_il} {date_str}", "forward_title": ""}
    return {"mode": "postmarket", "subtitle": f"סיכום יום המסחר בוול סטריט, {day_il} {date_str}", "forward_title": ""}


def openai_text(payload: Dict[str, Any]) -> str:
    if isinstance(payload.get("output_text"), str):
        return payload["output_text"]
    parts: List[str] = []
    for item in payload.get("output", []) or []:
        for c in item.get("content", []) or []:
            if isinstance(c, dict):
                if isinstance(c.get("text"), str):
                    parts.append(c["text"])
                elif isinstance(c.get("output_text"), str):
                    parts.append(c["output_text"])
    return "\n".join(parts)


def call_openai(prompt: str) -> str:
    body = {
        "model": OPENAI_MODEL,
        "input": [{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
        "max_output_tokens": MAX_OUTPUT_TOKENS,
    }
    r = requests.post(
        OPENAI_BASE,
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"},
        json=body,
        timeout=120,
    )
    try:
        data = r.json()
    except Exception:
        raise RuntimeError(f"OpenAI returned non-JSON status={r.status_code}: {r.text[:500]}")
    if not r.ok:
        raise RuntimeError(f"OpenAI error {r.status_code}: {json.dumps(data, ensure_ascii=False)[:1000]}")
    text = openai_text(data).strip()
    if not text:
        raise RuntimeError("OpenAI returned empty text")
    return text


def extract_json_object(text: str) -> Dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    if start < 0:
        raise ValueError("No JSON object found")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return json.loads(text[start:i + 1])
    raise ValueError("JSON object was not closed")


def fix_numeric_spacing(text: str) -> str:
    text = re.sub(r"(\d)\.\s+(\d)", r"\1.\2", text)
    text = re.sub(r"(\d)\s+%", r"\1%", text)
    text = re.sub(r"(\d+(?:\.\d+)?)%\s*[-–—]\s*(\d+(?:\.\d+)?)%", r"בין \1% ל-\2%", text)
    text = re.sub(r"(\d+(?:\.\d+)?)\s*[-–—]\s*(\d+(?:\.\d+)?)%", r"בין \1% ל-\2%", text)
    return text


def clean_text(value: Any) -> str:
    text = str(value or "")
    text = re.sub(r"[\u200E\u200F\u202A-\u202E\u2066-\u2069\u200B-\u200D\uFEFF\u00AD]", "", text)
    text = text.replace("**", "").replace("```", "").replace("###", "").replace("##", "").replace("#", "")
    text = text.replace("---", ",").replace("--", ",").replace("—", ",").replace("–", ",")
    text = text.replace("אס אנד פי 500", "S&P 500").replace("אס אנד פי", "S&P")
    text = re.sub(r"\bבאסי\b", "בחברת ASI", text)
    text = re.sub(r"\bאסי\b", "חברת ASI", text)
    text = re.sub(r"^\s*נקודה\s*\d+\s*[:.：־\-–—]?\s*", "", text, flags=re.I)
    text = re.sub(r"\$([A-Z]{1,6})", r"\1", text)
    text = fix_numeric_spacing(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def ends_complete(text: str) -> bool:
    t = text.strip()
    return bool(t) and t[-1] in ".!?؟"


def normalize_structured(obj: Dict[str, Any], ctx: Dict[str, str]) -> Dict[str, Any]:
    review = {
        "title": clean_text(obj.get("title") or "טעימת וול סטריט"),
        "subtitle": clean_text(obj.get("subtitle") or ctx["subtitle"]),
        "intro": clean_text(obj.get("intro")),
        "main_title": clean_text(obj.get("main_title") or "במרכז הפתיחה"),
        "main": [],
        "background_title": clean_text(obj.get("background_title") or "ברקע"),
        "background": [],
        "forward_title": clean_text(obj.get("forward_title") or ctx.get("forward_title") or ""),
        "forward": [],
        "bottom_line": clean_text(obj.get("bottom_line")),
        "summary_points": [],
    }
    for key, limit in (("main", 3), ("background", 2), ("forward", 3)):
        items = obj.get(key) or []
        if not isinstance(items, list):
            items = []
        for item in items[:limit]:
            if not isinstance(item, dict):
                continue
            heading = clean_text(item.get("heading"))
            body = clean_text(item.get("body"))
            if heading and body:
                review[key].append({"heading": heading, "body": body})
    points = obj.get("summary_points") or []
    if not isinstance(points, list):
        points = []
    review["summary_points"] = [clean_text(p).rstrip(".") for p in points if clean_text(p)][:5]
    if len(review["summary_points"]) < 3:
        fallback = [review["intro"]] + [f'{x["heading"]}: {x["body"]}' for x in review["main"]]
        review["summary_points"] = [clean_text(x).rstrip(".") for x in fallback if clean_text(x)][:5]
    return review


def validate_review(review: Dict[str, Any], facts: Dict[str, Any]) -> None:
    blob = json.dumps(review, ensure_ascii=False)
    hits = [p for p in FORBIDDEN_PHRASES if p and p in blob]
    if hits:
        raise ValueError(f"forbidden phrases: {hits}")
    shallow_hits = [p for p in SHALLOW_MARKET_PHRASES if p and p in blob]
    if shallow_hits:
        raise ValueError(f"shallow market phrasing: {shallow_hits}")
    if "$" in blob:
        raise ValueError("dollar sign ticker format is forbidden")
    for pat in BAD_NUMERIC_PATTERNS:
        if pat.search(blob):
            raise ValueError(f"bad numeric artifact: {pat.pattern}")
    if FUTURES_PERCENT_RE.search(blob):
        raise ValueError("futures percentage is forbidden without verified futures feed")
    if not review.get("intro") or not ends_complete(review["intro"]):
        raise ValueError("intro missing or incomplete")
    if len(review.get("main") or []) < 2:
        raise ValueError("need at least 2 main items")
    if not review.get("bottom_line") or not ends_complete(review["bottom_line"]):
        raise ValueError("bottom_line missing or incomplete")
    for section in ("main", "background", "forward"):
        for item in review.get(section) or []:
            if not HEBREW_RE.search(item.get("heading", "")[:4]):
                raise ValueError(f"{section} heading does not start with Hebrew")
            if BANNED_PREFIX_RE.search(item.get("body", "")):
                raise ValueError(f"{section} body starts with English/ticker")
            if not ends_complete(item.get("body", "")):
                raise ValueError(f"{section} body incomplete")
            body_text = item.get("body", "")
            if len(body_text) > 520:
                raise ValueError(f"{section} body too long")
            # If geopolitics is used, the paragraph must explain a market transmission channel.
            # This blocks vague conclusions such as "the market prefers stocks over oil".
            if has_geo_context(body_text) and not has_market_mechanism(body_text):
                raise ValueError(f"{section} geopolitical paragraph lacks market mechanism")
    if has_geo_context(review.get("bottom_line", "")) and not has_market_mechanism(review.get("bottom_line", "")):
        raise ValueError("bottom_line geopolitical sentence lacks market mechanism")
    if review.get("forward_title") and review["forward_title"] not in {"לקראת המסחר הקרוב", "לקראת השבוע הקרוב"}:
        raise ValueError("bad forward title")


def build_prompt(tweets: List[Dict[str, Any]], facts: Dict[str, Any], ctx: Dict[str, str], previous_error: str = "") -> str:
    return f"""
שם המשימה: הפקת סקירת Market Desk מקצועית, קצרה וברורה על וול סטריט.
שפת הפלט: עברית בלבד, ימין לשמאל, ניסוח פשוט, פיננסי וישיר.

תפקידך:
לנתח את נתוני המקור ולכתוב סקירת שוק קצרה וברורה למשקיע מקצועי.
הסקירה אינה סיכום ציוצים ואינה רשימת כותרות. אסור להזכיר בגוף הסקירה את המילים: ציוץ, פוסט, מקור, הוזכר, דווח כי, לפי דיווח, נאמר כי.

סגנון:
- כתיבה מקצועית, ישירה ופשוטה.
- משפטים קצרים. בלי התחכמות ובלי מילים מנופחות.
- להסביר ברור: מה קרה, למה זה משנה, מה לבדוק במסחר.
- אסור להשתמש בביטויים: שכבת ערך, מסגרת חדשה, שיח ביטחוני, עדשת ביטחון לאומי, נכסי צמיחה עתירי נרטיב, תמחור נרטיבי.
- אסור לכתוב משפטי שוק כלליים כמו: השוק מעדיף מניות על פני נפט, השוק יאהב את זה, זה חיובי לשוק, זה רע לסנטימנט, המשקיעים יעדיפו מניות.

חובה להסביר מנגנון שוקי:
- כל פסקה חייבת להסביר למה הנושא משנה לשוק, לא רק לתאר מה קרה.
- אם אתה כותב על גיאופוליטיקה, איראן, הורמוז או נפט, חובה להסביר את שרשרת ההשפעה: גיאופוליטיקה → נפט → אינפלציה → תשואות/ריבית → סנטימנט במניות.
- דוגמה לא טובה: השוק עשוי להמשיך להעדיף מניות על פני נפט.
- דוגמה טובה: ירידה בסיכון סביב הורמוז עשויה להפחית לחץ על מחירי הנפט, להקל על חששות אינפלציה, ולתמוך בסנטימנט במניות.

מבנה חובה:
1. פתיח של שני משפטים: מה שאלת השוק המרכזית.
2. במרכז הפתיחה: 2 עד 3 נושאים מרכזיים.
3. ברקע: 1 עד 2 נושאים משניים.
4. {ctx['forward_title'] or 'אין סעיף קדימה'}: {('3 טריגרים ברורים בלבד' if ctx['forward_title'] else 'להחזיר מערך ריק')}.
5. שורה תחתונה: 2 עד 3 משפטים בלבד.

כל סעיף:
- כותרת קצרה בשורה נפרדת.
- פסקה אחת מתחת, 2 משפטים קצרים לכל היותר.
- בלי Markdown. אסור ###, ##, #, **, נקודה 1.
- כל כותרת וכל פסקה מתחילות בעברית, לא באנגלית ולא בטיקר.
- סעיפי "לקראת המסחר הקרוב" חייבים להיות טריגרים ברורים: אם X קורה, מה זה יסמן במסחר. לא לכתוב "לעקוב אחרי" בלי הסבר.

טיקרים:
- אין להשתמש בסימן דולר.
- אם צריך טיקר, לכתוב אותו בסוגריים אחרי שם החברה: אפל (AAPL), פלנטיר (PLTR), ספייסאיקס (SPCX).
- שם חברה לא מוכר לא לתרגם לבד: חברת ASI, תאלס (Thales).

נתונים ואחוזים:
- השתמש רק במספרים שמופיעים ב-MARKET FACTS או בנתוני המקור.
- אחוזים חייבים להיכתב תקין: 0.7%, 1.2%, בין 0.7% ל-0.8%.
- אסור לכתוב 0. 7% או סביב 0.
- אין לכתוב אחוז על חוזים עתידיים. בגרסה זו אין מקור חוזים מאומת. אם צריך לדבר על חוזים, כתוב כיוון בלבד.
- אסור להעתיק אחוזי ETF כאילו הם חוזים.
- אם אין נתון מספרי אמין, כתוב כיוון בלבד או השמט את המספר.

הקשר סקירה:
- כותרת משנה: {ctx['subtitle']}
- מצב: {ctx['mode']}

{facts_text(facts)}

נתוני מקור לעיבוד. אין להזכיר בגוף הסקירה שהם הגיעו מציוצים או ממקורות:
{build_source_digest(tweets)}

{('שגיאה בניסיון קודם: ' + previous_error + ' תקן והחזר JSON תקין בלבד.') if previous_error else ''}

החזר JSON תקין בלבד, בדיוק במבנה הזה:
{{
  "title": "טעימת וול סטריט",
  "subtitle": "{ctx['subtitle']}",
  "intro": "שני משפטים בלבד.",
  "main_title": "במרכז הפתיחה",
  "main": [
    {{"heading": "כותרת עברית קצרה", "body": "פסקה קצרה עד שני משפטים."}}
  ],
  "background_title": "ברקע",
  "background": [
    {{"heading": "כותרת עברית קצרה", "body": "פסקה קצרה עד שני משפטים."}}
  ],
  "forward_title": "{ctx['forward_title']}",
  "forward": [
    {{"heading": "אם משהו קורה", "body": "מה זה יסמן במסחר."}}
  ],
  "bottom_line": "שניים עד שלושה משפטים.",
  "summary_points": ["משפט תקציר נקי אחד"]
}}
"""


def generate_review(tweets: List[Dict[str, Any]], facts: Dict[str, Any], ctx: Dict[str, str]) -> Dict[str, Any]:
    last_error = ""
    for attempt in range(1, 4):
        print(f"OpenAI attempt {attempt}/3")
        text = call_openai(build_prompt(tweets, facts, ctx, previous_error=last_error))
        try:
            obj = extract_json_object(text)
            review = normalize_structured(obj, ctx)
            validate_review(review, facts)
            return review
        except Exception as e:
            last_error = str(e)
            print(f"  validation failed: {last_error}")
    raise RuntimeError(f"Review generation failed after retries: {last_error}")


def markdown_from_review(review: Dict[str, Any]) -> str:
    lines: List[str] = []
    lines.append(f"# 🌅 {review['title']}")
    lines.append("")
    lines.append(review["subtitle"])
    lines.append("")
    lines.append(review["intro"])
    lines.append("")
    lines.append(f"## {review['main_title']}")
    lines.append("")
    for item in review["main"]:
        lines.append(item["heading"])
        lines.append("")
        lines.append(item["body"])
        lines.append("")
    if review["background"]:
        lines.append(f"## {review['background_title']}")
        lines.append("")
        for item in review["background"]:
            lines.append(item["heading"])
            lines.append("")
            lines.append(item["body"])
            lines.append("")
    if review.get("forward_title") and review.get("forward"):
        lines.append(f"## {review['forward_title']}")
        lines.append("")
        for item in review["forward"]:
            lines.append(item["heading"])
            lines.append("")
            lines.append(item["body"])
            lines.append("")
    lines.append("## שורה תחתונה")
    lines.append("")
    lines.append(review["bottom_line"])
    lines.append("")
    lines.append("⚠️ גילוי נאות: תוכן זה נוצר באמצעות AI לצרכים אינפורמטיביים בלבד. אין באמור ייעוץ השקעות או המלצה לפעולה בניירות ערך.")
    lines.append("")
    lines.append("פותח ע\"י דורון שרייבמן")
    return "\n".join(lines).strip() + "\n"


def write_outputs(review: Dict[str, Any], tweets: List[Dict[str, Any]], facts: Dict[str, Any], ctx: Dict[str, str]) -> None:
    now_utc = datetime.now(timezone.utc)
    now_il = now_utc.astimezone(ZoneInfo("Asia/Jerusalem"))
    md = markdown_from_review(review)
    payload = {
        "channel": "wallstreet",
        "generated_utc": now_utc.isoformat(),
        "generated_israel": now_il.isoformat(),
        "mode": ctx["mode"],
        "title": review["title"],
        "subtitle": review["subtitle"],
        "structured_review": review,
        "market_facts": facts,
        "source_items": tweets,
    }
    (OUTPUT_DIR / "latest.md").write_text(md, encoding="utf-8")
    (OUTPUT_DIR / "latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    # Legacy paths for the existing site if it still expects them.
    (OUTPUT_ROOT / "latest.md").write_text(md, encoding="utf-8")
    (OUTPUT_ROOT / "latest.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {OUTPUT_DIR / 'latest.md'} and {OUTPUT_DIR / 'latest.json'}")


def main() -> None:
    accounts = read_accounts()
    all_tweets: List[Dict[str, Any]] = []
    for acc in accounts:
        try:
            all_tweets.extend(fetch_tweets_for_account(acc))
        except Exception as e:
            print(f"Error fetching @{acc}: {e}")
    selected = select_tweets(all_tweets)
    if not selected:
        raise SystemExit("No tweets/source items fetched. Refusing to create empty review.")
    symbols = collect_symbols(selected)
    facts = fetch_market_facts(symbols)
    ctx = get_review_context(datetime.now(ZoneInfo("Asia/Jerusalem")))
    review = generate_review(selected, facts, ctx)
    write_outputs(review, selected, facts, ctx)


if __name__ == "__main__":
    main()
