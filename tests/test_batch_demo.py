import pytest
from app.db_models import CheckoutSession, RecoveryOutcomeRecord
from scripts.seed_batch_demo import seed_demo_data
from tests.test_guardrails import client_with_merchant

def test_batch_demo_script_and_report(client_with_merchant):
    client, db, merchant, customer = client_with_merchant
    
    # 1. Run the seed script with isolated in-memory test db
    seed_demo_data(db)
    
    # 2. Check DB groups
    control_count = db.query(CheckoutSession).filter(CheckoutSession.event_id.like("demo_evt_%"), CheckoutSession.is_control_group == True).count()
    agent_count = db.query(CheckoutSession).filter(CheckoutSession.event_id.like("demo_evt_%"), CheckoutSession.is_control_group == False).count()
    
    assert control_count > 0, "Seed script should create some control group sessions"
    assert agent_count > 0, "Seed script should create some agent-assisted sessions"
    
    # Check control-group sessions have 'control_group_no_action'
    control_outcomes = db.query(RecoveryOutcomeRecord).join(CheckoutSession).filter(CheckoutSession.is_control_group == True).all()
    for o in control_outcomes:
        assert o.action_taken == "control_group_no_action"
        
    # 3. Hit the retired batch-report endpoint — should redirect to revenue-intelligence
    res = client.get("/merchant/batch-report", follow_redirects=False)
    assert res.status_code in (302, 307)
    assert res.headers["location"] == "/merchant/revenue-intelligence"

    # Follow redirect
    res_followed = client.get("/merchant/batch-report", follow_redirects=True)
    assert res_followed.status_code == 200
    assert "AI Revenue Intelligence" in res_followed.text

