import os
import json
from datetime import datetime, timezone
from pathlib import Path

import requests

OUTPUT_DIR = Path("output")
OUTPUT_DIR.mkdir(exist_ok=True)

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.5").strip()

if not OPENAI_API_KEY:
    raise SystemExit("Missing GitHub secret: OPENAI_API_KEY")

payload = {
    "model": OPENAI_MODEL,
    "input": "ענה בעברית במשפט אחד בלבד: בדיקת OpenAI הצליחה.",
    "max_output_tokens": 100
}

response = requests.post(
    "https://api.openai.com/v1/responses",
    headers={
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    },
    json=payload,
    timeout=120,
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

text = data.get("output_text", "")

if not text:
    parts = []
    for item in data.get("output", []):
        for content in item.get("content", []):
            if isinstance(content, dict) and "text" in content:
                parts.append(content["text"])
    text = "\n".join(parts)

result = {
    "generated_utc": datetime.now(timezone.utc).isoformat(),
    "model": OPENAI_MODEL,
    "status_code": response.status_code,
    "text": text,
}

Path("output/openai_test.json").write_text(
    json.dumps(result, ensure_ascii=False, indent=2),
    encoding="utf-8",
)

Path("output/openai_test.md").write_text(
    f"# OpenAI Test\n\nModel: {OPENAI_MODEL}\n\nResult:\n\n{text}\n",
    encoding="utf-8",
)

print(text)
print("OpenAI test completed.")
