from dotenv import load_dotenv
load_dotenv()

from app.razorpay_client import RazorpayRecoveryClient

client = RazorpayRecoveryClient()
result = client.create_recovery_payment_link(
    amount_rupees=499,
    customer_name="Test Customer",
    customer_email="test@example.com",
    customer_phone="+919876543210",
    description="Test recovery link",
    reference_id="manual_test_1",
)
print(result)