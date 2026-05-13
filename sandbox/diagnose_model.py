"""Quick check: is the OpenRouter model reachable and answering?"""
import os
import sys

# Load backend/.env so OPENROUTER_API_KEY etc. are available.
from pathlib import Path
env_path = Path(__file__).resolve().parent.parent / "backend" / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())

from langchain_openai import ChatOpenAI
from langchain_core.messages import HumanMessage

key = os.getenv("OPENROUTER_API_KEY") or ""
print("OPENROUTER_API_KEY:", "present" if key else "MISSING")
model_name = os.getenv("CHAT_MODEL", "meta-llama/llama-3.3-70b-instruct:free")
print("model:", model_name)

try:
    client = ChatOpenAI(
        model=model_name,
        openai_api_key=key,
        openai_api_base="https://openrouter.ai/api/v1",
        max_tokens=50,
        temperature=0.3,
        default_headers={"HTTP-Referer": "http://localhost", "X-Title": "diagnostic"},
    )
    r = client.invoke([HumanMessage(content="Say 'pong' and nothing else.")])
    print("OK reply:", repr(r.content[:200]))
except Exception as e:
    print(f"ERROR ({type(e).__name__}): {str(e)[:500]}")
    sys.exit(1)
