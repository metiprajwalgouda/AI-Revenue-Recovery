import os
import logging
from typing import Optional

logger = logging.getLogger("whatsapp_service")

def send_whatsapp_message(to_phone: str, message: str) -> dict:
    """
    Sends a WhatsApp message using Twilio's WhatsApp API.
    Reuses TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN.
    """
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    
    if not account_sid or not auth_token:
        logger.warning("WhatsApp sending skipped: Twilio credentials not configured.")
        return {"status": "failed", "error": "Twilio credentials not configured"}
        
    try:
        from twilio.rest import Client
        client = Client(account_sid, auth_token)
        
        # Twilio WhatsApp sandbox number
        from_number = os.getenv("TWILIO_WHATSAPP_NUMBER", "whatsapp:+14155238886")
        
        # Format destination phone for WhatsApp
        if not to_phone.startswith("whatsapp:"):
            # Assume to_phone already has country code (e.g., +91...)
            to_phone = f"whatsapp:{to_phone}"
            
        message = client.messages.create(
            from_=from_number,
            body=message,
            to=to_phone
        )
        
        logger.info(f"WhatsApp sent successfully, SID: {message.sid}")
        return {"status": "initiated", "sid": message.sid}
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Failed to send WhatsApp message: {error_msg}")
        return {"status": "failed", "error": error_msg}
