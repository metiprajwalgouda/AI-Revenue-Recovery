import pytest
from app.agent.classifier import llm_classify
from app.models import CheckoutEvent
from datetime import datetime
import os

def test_llm_classification_hides_raw_exception(monkeypatch):
    # force failure by having no API key and an invalid client
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    monkeypatch.delenv('ANTHROPIC_API_KEY', raising=False)
    
    event = CheckoutEvent(
        event_id="test_event_1",
        customer_id="cust1",
        customer_email="a@b.com",
        customer_phone="123",
        cart_value=100.0,
        checkout_started_at=datetime.now(),
        abandoned_at=datetime.now()
    )
    
    result = llm_classify(event)
    assert result.predicted_reason == "unknown"
    assert "Diagnosis unavailable" in result.reasoning
    assert "api_key" not in result.reasoning.lower()
    assert "auth" not in result.reasoning.lower()
