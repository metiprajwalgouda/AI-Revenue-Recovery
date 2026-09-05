import os
import requests
from twilio.rest import Client
from twilio.base.exceptions import TwilioRestException

def make_recovery_call(to_phone: str, customer_name: str, cart_total: float, resume_url: str, coupon_code: str = None) -> dict:
    provider = os.getenv("RECOVERY_CALL_PROVIDER", "twilio").lower()
    
    if coupon_code:
        call_script = f"Hi {customer_name}, you have an order of {cart_total} waiting. Continue your order using coupon code {coupon_code} to get a discount. Thank you."
    else:
        call_script = f"Hi {customer_name}, you have an order of {cart_total} waiting. Please return to our store to complete your order. Thank you."

    if provider == "exotel":
        exotel_sid = os.getenv("EXOTEL_SID")
        exotel_key = os.getenv("EXOTEL_API_KEY")
        exotel_token = os.getenv("EXOTEL_API_TOKEN")
        from_phone = os.getenv("EXOTEL_PHONE_NUMBER")
        domain = os.getenv("EXOTEL_API_DOMAIN", "https://api.exotel.com")
        
        if not all([exotel_sid, exotel_key, exotel_token, from_phone]):
            print("Exotel credentials missing.")
            return {"status": "skipped_no_key", "error": "Missing Exotel credentials"}
            
        mock_calls = os.getenv("MOCK_CALLS", "true").lower() == "true"
        
        if mock_calls:
            print(f"[MOCK] Exotel call would be initiated to {to_phone} from {from_phone} via {domain}")
            print(f"[MOCK] Script: {call_script}")
            return {"status": "initiated", "sid": "mock_exotel_sid"}
            
        # Real Exotel API Execution
        try:
            url = f"{domain}/v1/Accounts/{exotel_sid}/Calls/connect.json"
            auth = (exotel_key, exotel_token)
            
            # This payload assumes you have an Exotel AppId configured for the automated voice flow.
            # You will need to set EXOTEL_APP_ID in your .env
            app_id = os.getenv("EXOTEL_APP_ID", "")
            
            payload = {
                "From": to_phone,         # The customer's number
                "CallerId": from_phone,   # Your Exotel virtual number
                "CallType": "trans",
                "Url": f"http://my.app.url/exotel_twiml", # Replace with your dynamic TwiML endpoint if not using AppId
                "CustomField": f"{cart_total},{coupon_code or ''}" 
            }
            
            print(f"Initiating REAL Exotel call to {to_phone}...")
            response = requests.post(url, auth=auth, data=payload, timeout=10)
            response.raise_for_status()
            
            call_sid = response.json().get("Call", {}).get("Sid", "unknown_sid")
            print(f"Exotel call initiated successfully: SID {call_sid}")
            return {"status": "initiated", "sid": call_sid}
            
        except requests.exceptions.RequestException as e:
            print(f"Exotel API Error during call: {e}")
            return {"status": "failed", "error": str(e)}

    # Fallback to Twilio
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    from_phone = os.getenv("TWILIO_PHONE_NUMBER")

    if not account_sid or not auth_token or not from_phone:
        print("Twilio credentials missing. Skipping voice call.")
        return {"status": "skipped_no_key", "error": "Missing Twilio credentials"}

    mock_calls = os.getenv("MOCK_CALLS", "true").lower() == "true"
    if mock_calls:
        print(f"[MOCK] Twilio call would be initiated to {to_phone} from {from_phone}")
        print(f"[MOCK] Script: {call_script}")
        return {"status": "initiated", "sid": "mock_twilio_sid"}

    try:
        from app.db import SessionLocal
        from app.db_models import CheckoutSession
        db = SessionLocal()
        session_row = db.query(CheckoutSession).filter(
            CheckoutSession.customer_phone == to_phone,
            CheckoutSession.cart_value == cart_total
        ).order_by(CheckoutSession.id.desc()).first()
        event_id = session_row.event_id if session_row else ""
        db.close()

        client = Client(account_sid, auth_token)
        
        from app.config import get_base_url
        public_url = (os.getenv("PUBLIC_APP_URL") or get_base_url()).rstrip("/")
        twiml_url = f"{public_url}/api/twilio-twiml?event_id={event_id}"
        if coupon_code:
            twiml_url += f"&coupon_code={coupon_code}"
        
        call = client.calls.create(
            url=twiml_url,
            to=to_phone,
            from_=from_phone
        )
        print(f"Twilio call initiated: SID {call.sid}")
        return {"status": "initiated", "sid": call.sid}
    except TwilioRestException as e:
        print(f"Twilio API Error during call: {e}")
        return {"status": "failed", "error": str(e)}
    except Exception as e:
        print(f"Unexpected error during Twilio call: {e}")
        return {"status": "failed", "error": str(e)}


def make_invoice_overdue_call(
    to_phone: str,
    customer_name: str,
    invoice_number: str,
    amount: float,
    days_overdue: int,
    payment_link_url: str = None
) -> dict:
    """Initiates a voice call reminder for an overdue invoice (manual human action on Priority queue)."""
    provider = os.getenv("RECOVERY_CALL_PROVIDER", "twilio").lower()
    
    call_script = (
        f"Hi {customer_name}, this is a reminder regarding Invoice {invoice_number} for {amount:.2f} rupees, "
        f"which is currently {days_overdue} days overdue. Please check your email or SMS for your payment link to complete payment. Thank you."
    )

    if provider == "exotel":
        exotel_sid = os.getenv("EXOTEL_SID")
        exotel_key = os.getenv("EXOTEL_API_KEY")
        exotel_token = os.getenv("EXOTEL_API_TOKEN")
        from_phone = os.getenv("EXOTEL_PHONE_NUMBER")
        domain = os.getenv("EXOTEL_API_DOMAIN", "https://api.exotel.com")
        
        if not all([exotel_sid, exotel_key, exotel_token, from_phone]):
            print("Exotel credentials missing.")
            return {"status": "skipped_no_key", "error": "Missing Exotel credentials"}
            
        mock_calls = os.getenv("MOCK_CALLS", "true").lower() == "true"
        if mock_calls:
            print(f"[MOCK] Exotel call would be initiated to {to_phone} from {from_phone} via {domain}")
            print(f"[MOCK] Script: {call_script}")
            return {"status": "initiated", "sid": "mock_exotel_invoice_sid"}
            
        try:
            url = f"{domain}/v1/Accounts/{exotel_sid}/Calls/connect.json"
            auth = (exotel_key, exotel_token)
            payload = {
                "From": to_phone,
                "CallerId": from_phone,
                "CallType": "trans",
                "Url": f"http://my.app.url/exotel_twiml",
                "CustomField": f"inv_{invoice_number},{amount},{days_overdue}"
            }
            response = requests.post(url, auth=auth, data=payload, timeout=10)
            response.raise_for_status()
            call_sid = response.json().get("Call", {}).get("Sid", "unknown_sid")
            return {"status": "initiated", "sid": call_sid}
        except requests.exceptions.RequestException as e:
            print(f"Exotel API Error during invoice call: {e}")
            return {"status": "failed", "error": str(e)}

    # Fallback to Twilio
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    from_phone = os.getenv("TWILIO_PHONE_NUMBER")

    if not account_sid or not auth_token or not from_phone:
        print("Twilio credentials missing. Skipping invoice voice call.")
        return {"status": "skipped_no_key", "error": "Missing Twilio credentials"}

    mock_calls = os.getenv("MOCK_CALLS", "true").lower() == "true"
    if mock_calls:
        print(f"[MOCK] Twilio invoice call would be initiated to {to_phone} from {from_phone}")
        print(f"[MOCK] Script: {call_script}")
        return {"status": "initiated", "sid": "mock_twilio_invoice_sid"}

    try:
        client = Client(account_sid, auth_token)
        from app.config import get_base_url
        import urllib.parse
        public_url = (os.getenv("PUBLIC_APP_URL") or get_base_url()).rstrip("/")
        enc_name = urllib.parse.quote(customer_name)
        enc_inv = urllib.parse.quote(invoice_number)
        twiml_url = f"{public_url}/api/twilio-twiml?invoice_number={enc_inv}&days_overdue={days_overdue}&amount={amount:.2f}&customer_name={enc_name}"
        
        call = client.calls.create(
            url=twiml_url,
            to=to_phone,
            from_=from_phone
        )
        print(f"Twilio invoice call initiated: SID {call.sid}")
        return {"status": "initiated", "sid": call.sid}
    except TwilioRestException as e:
        print(f"Twilio API Error during invoice call: {e}")
        return {"status": "failed", "error": str(e)}
    except Exception as e:
        print(f"Unexpected error during Twilio invoice call: {e}")
        return {"status": "failed", "error": str(e)}

