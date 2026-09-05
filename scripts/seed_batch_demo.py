import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import uuid
import random
from datetime import datetime, timedelta, timezone
from sqlalchemy.orm import Session
from app.db import SessionLocal
from app.db_models import CheckoutSession, RecoveryOutcomeRecord, SessionStatus

def seed_demo_data(db: Session = None):
    should_close = False
    if db is None:
        db = SessionLocal()
        should_close = True
    
    try:
        from sqlalchemy import text
        db.execute(text("ALTER TABLE checkout_sessions ADD COLUMN is_control_group BOOLEAN DEFAULT 0"))
        db.commit()
    except Exception as e:
        pass # Column already exists
    
    from app.db_models import CustomerUser
    c = db.query(CustomerUser).first()
    if not c:
        c = CustomerUser(name="Seed User", email="seed@example.com")
        c.set_password("pass")
        db.add(c)
        db.commit()
    cid = c.id
    
    print("Generating batch demo data...")
    
    reasons = [
        "hesitation_price", 
        "hesitation_shipping", 
        "bank_gateway_failure", 
        "otp_timeout", 
        "card_declined", 
        "unknown"
    ]
    
    now = datetime.now(timezone.utc)
    
    # Generate 30 sessions
    for i in range(30):
        is_control = random.choice([True, False])
        amount = float(random.choice([500, 750, 1200, 1500, 2400, 3200, 4500, 5000]))
        reason = random.choice(reasons)
        
        # Outcome probabilities: 
        # If control group: 10% natural recovery.
        # If agent-assisted: 
        #   - technical failures -> 15% recovery (retry link only)
        #   - hesitation -> 40% recovery (discount offer)
        
        recovered = False
        if is_control:
            recovered = random.random() < 0.10
        else:
            if "hesitation" in reason:
                recovered = random.random() < 0.40
            elif "failure" in reason or "timeout" in reason or "declined" in reason:
                recovered = random.random() < 0.15
            else:
                recovered = random.random() < 0.20
        
        status = SessionStatus.COMPLETED if recovered else SessionStatus.ABANDONED
        
        session = CheckoutSession(
            event_id="demo_evt_" + uuid.uuid4().hex[:12],
            customer_user_id=cid,  # Hardcode to first customer
            customer_name=f"Demo User {i}",
            customer_email=f"demo{i}@example.com",
            customer_phone="+919876543210",
            cart_value=amount,
            status=status,
            started_at=now - timedelta(days=random.randint(1, 5)),
            is_control_group=is_control
        )
        db.add(session)
        db.commit()
        db.refresh(session)
        
        # Add a RecoveryOutcomeRecord for ALL of them to represent the diagnosis,
        # but for the control group, action_taken will just be "control_group_no_action"
        if not is_control:
            action = "sent_email_discount" if "hesitation" in reason else "sent_email_retry"
        else:
            action = "control_group_no_action"
            
        outcome = RecoveryOutcomeRecord(
            session_id=session.id,
            predicted_reason=reason,
            confidence=0.85,
            classification_method="llm",
            reasoning="Seeded demo data",
            action_taken=action,
            action_success=True,
            amount_offered=amount * 0.9 if ("hesitation" in reason and not is_control) else amount,
            delivery_status="delivered"
        )
        db.add(outcome)
        db.commit()
        
        if recovered:
            # Create a completed session linked to this
            rec_session = CheckoutSession(
                event_id="demo_evt_" + uuid.uuid4().hex[:12],
                customer_user_id=cid,
                customer_name=f"Demo User {i}",
                customer_email=f"demo{i}@example.com",
                customer_phone="+919876543210",
                cart_value=outcome.amount_offered or amount,
                status=SessionStatus.COMPLETED,
                started_at=now - timedelta(hours=random.randint(1, 10)),
                recovered_from_session_id=session.id,
                is_control_group=is_control
            )
            db.add(rec_session)
            db.commit()

    print("Seeded 30 demo sessions.")

if __name__ == '__main__':
    seed_demo_data()
