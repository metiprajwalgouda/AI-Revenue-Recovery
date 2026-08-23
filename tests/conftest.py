"""
Pytest automatically discovers and runs conftest.py before any tests.
This ensures .env is loaded into os.environ for every test run --
without this, ANTHROPIC_API_KEY / RAZORPAY_KEY_ID etc. from your .env
file are invisible to pytest even though your app can see them fine.
"""

from dotenv import load_dotenv

load_dotenv()