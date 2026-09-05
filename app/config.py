"""
Loads environment variables from a local .env file (if present).

Imported by app.main so uvicorn/pytest both see RAZORPAY_*, RESEND_*,
and ANTHROPIC_* without each module calling load_dotenv() itself.
Missing keys are allowed — each integration degrades at call time.
"""

import os
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

# Centralized default LLM models across all classifiers
GEMINI_DEFAULT_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")
CLAUDE_DEFAULT_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")


def get_base_url(request=None) -> str:
    """
    Returns the application base URL (e.g. 'https://xxx.ngrok-free.dev' or 'http://localhost:8001').
    Priority:
      1. PUBLIC_APP_URL environment variable (from .env, e.g. ngrok/public tunnel)
      2. APP_BASE_URL environment variable (from .env)
      3. request.base_url (derived dynamically from incoming HTTP request host header)
      4. Fallback: http://localhost:{PORT} (defaulting to port 8001)
    """
    public_url = os.getenv("PUBLIC_APP_URL")
    if public_url and public_url.strip():
        return public_url.strip().rstrip("/")

    env_url = os.getenv("APP_BASE_URL")
    if env_url and env_url.strip():
        return env_url.strip().rstrip("/")
    
    if request is not None:
        try:
            return str(request.base_url).rstrip("/")
        except Exception:
            pass

    port = os.getenv("PORT", "8001")
    return f"http://localhost:{port}"
