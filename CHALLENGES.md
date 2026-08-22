Bug: test isolation failure in Razorpay client tests. The test_missing_keys_raises_error test set key_id=None expecting a ValueError, but it passed even with broken logic because a previous test's os.environ.setdefault() had already set fallback env vars, which the client's constructor was silently picking up via os.getenv(). Fixed by explicitly clearing the env vars with monkeypatch.delenv() inside the test. Lesson: passing None as an arg isn't the same as the environment truly being empty — global test state can hide real bugs.


Bug 1 — Data model too strict for its own edge case: I built CheckoutEvent with cart_value: float = Field(..., gt=0) to guard against bad data, but then the dataset generator deliberately creates a ₹0 cart-value edge case to test that exact guardrail. The validation rejects the event before it even becomes an object — so the guardrail we built to handle zero-value carts never actually gets to run. The check is in the wrong place.

Bug 2 — Missing data silently misclassified: The "accidental close" rule checks time_on_checkout_page_sec <= 5, but when that field is genuinely missing (None), my code did (value or 0) <= 5, which treats "no data" the same as "0 seconds" — a real customer with unknown dwell time gets wrongly labeled as "accidentally closed the page in under 5 seconds." Missing data and short duration are different things and got conflated.

Bug: real_llm integration test always skipped despite valid API key in .env.
The test file set a placeholder ANTHROPIC_API_KEY via os.environ.setdefault()
but never called load_dotenv(), so the real key from .env was never loaded
into the environment -- the skip condition always saw the placeholder value
and never ran the real integration test. Fixed by adding load_dotenv() before
the setdefault call. Lesson: setdefault() silently masks missing configuration
if nothing populates the environment first.