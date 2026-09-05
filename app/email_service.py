import os
import logging
import requests

logger = logging.getLogger("recovery_agent.email_service")

from fastapi.templating import Jinja2Templates

templates = Jinja2Templates(directory="app/templates")

def send_recovery_email(to: str, subject: str, template_name: str, context: dict) -> bool:
    """
    Sends an email using the Resend HTTP API.
    Gracefully no-ops and logs a warning if RESEND_API_KEY is missing.
    Returns True if sent (or skipped successfully in dev), False if failed.
    """
    body_html = templates.get_template(template_name).render(context)
    
    api_key = os.getenv("RESEND_API_KEY")
    if not api_key:
        logger.warning(f"RESEND_API_KEY missing. Skipped sending email to {to}: {subject}")
        return True  # Skipped successfully, don't crash
        
    try:
        response = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json={
                "from": "Acme <onboarding@resend.dev>",
                "to": [to],
                "subject": subject,
                "html": body_html
            },
            timeout=10
        )
        response.raise_for_status()
        logger.info(f"Recovery email sent successfully to {to}")
        return True
    except requests.exceptions.RequestException as e:
        logger.error(f"Failed to send email to {to}: {e}")
        return False
