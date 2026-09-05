import os
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException
from app.db import SessionLocal

def send_template_sms(to_phone: str, template_keyword: str) -> dict:
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    from_phone = os.getenv("TWILIO_PHONE_NUMBER")

    if not account_sid or not auth_token or not from_phone:
        print("Twilio credentials missing. Skipping SMS.")
        return {"status": "skipped_no_key", "error": "Missing Twilio credentials"}

    mock_sms = os.getenv("MOCK_SMS_FIXED", "true").lower() == "true"
    if mock_sms:
        print(f"[MOCK] Twilio SMS to {to_phone} with keyword {template_keyword}")
        return {"status": "initiated", "sid": "mock_twilio_sms_sid"}

    try:
        client = Client(account_sid, auth_token)
        sms = client.messages.create(
            body=template_keyword,
            to=to_phone,
            from_=from_phone
        )
        print(f"Twilio SMS initiated: SID {sms.sid}")
        return {"status": "initiated", "sid": sms.sid}
    except TwilioRestException as e:
        print(f"Twilio API Error during SMS: {e}")
        return {"status": "failed", "error": str(e)}
    except Exception as e:
        print(f"Unexpected error during Twilio SMS: {e}")
        return {"status": "failed", "error": str(e)}

def send_recovery_sms(to_phone: str, message: str) -> dict:
    from app.db_models import CheckoutSession, RecoveryOutcomeRecord
    from app.agent.recovery_actions import is_discount_allowed
    
    db = SessionLocal()
    session = db.query(CheckoutSession).filter(CheckoutSession.customer_phone == to_phone).order_by(CheckoutSession.id.desc()).first()
    keyword = "sms_unavailable"
    
    if session:
        outcome = db.query(RecoveryOutcomeRecord).filter(RecoveryOutcomeRecord.session_id == session.id).first()
        if outcome and is_discount_allowed(outcome.predicted_reason):
            keyword = "sms_marketing_promotions"
        else:
            # Fallback for restricted sessions since none of the Twilio templates match an order recovery without discount
            keyword = "sms_unavailable"
    db.close()
    
    if keyword == "sms_unavailable":
        return {"status": "failed", "error": "SMS unavailable for this reason — use Email or Call instead"}
        
    return send_template_sms(to_phone, keyword)
