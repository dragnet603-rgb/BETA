import os
import requests
import json
import time

API_KEY = os.environ.get("MART_API_KEY", "")

URL = "https://api.apimart.ai/v1/chat/completions"

MODELS = [
    "gpt-5",
    "claude-sonnet-4-6",
    "gemini-2.5-pro",
    "deepseek-v3.2",
]

PROMPT = """
Write a Python function that takes a list of numbers and returns the median, mean, and standard deviation.

Return valid JSON containing:
- code
- explanation
- complexity
"""

headers = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}

for model in MODELS:

    print("\n" + "=" * 60)
    print(f"MODEL: {model}")
    print("=" * 60)

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": PROMPT
            }
        ],
        "stream": False
    }

    start = time.time()

    response = requests.post(
        URL,
        headers=headers,
        json=payload
    )

    elapsed = time.time() - start

    print("STATUS:", response.status_code)
    print("TIME:", round(elapsed, 2), "seconds")

    # PRINT THE ACTUAL RESPONSE
    print("\nRAW RESPONSE:")
    print(json.dumps(response.json(), indent=2))