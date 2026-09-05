"""
Seed real e-commerce products with high-resolution images, rich descriptions,
realistic pricing in INR, and stock counts across active merchants.
Removes placeholder/test products.
"""
import sys
import os

# Ensure repo root is on sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.db import SessionLocal
from app.db_models import Product, MerchantUser

REAL_PRODUCTS = [
    {
        "merchant_id": 1,
        "name": "Sony WH-1000XM5 Wireless Noise Cancelling Headphones",
        "description": "Industry-leading noise cancellation with two processors and 8 microphones. Up to 30-hour battery life with quick charging. Crystal-clear hands-free calling with 4 beamforming microphones.",
        "price": 26990.0,
        "stock": 25,
        "image_url": "https://images.unsplash.com/photo-1505740420928-5e560c06d30e?auto=format&fit=crop&w=800&q=80",
        "is_active": True
    },
    {
        "merchant_id": 1,
        "name": "Apple MacBook Air 13.6\" (M2 Chip, 8GB RAM, 256GB SSD)",
        "description": "Strikingly thin design with 13.6-inch Liquid Retina display, 1080p FaceTime HD camera, MagSafe 3 charging port, and up to 18 hours of all-day battery life.",
        "price": 94990.0,
        "stock": 12,
        "image_url": "https://images.unsplash.com/photo-1517336714731-489689fd1ca8?auto=format&fit=crop&w=800&q=80",
        "is_active": True
    },
    {
        "merchant_id": 1,
        "name": "Samsung Galaxy S24 Ultra 5G (Titanium Gray, 256GB)",
        "description": "200MP camera with Galaxy AI assistance, built-in S Pen, Snapdragon 8 Gen 3 processor, and Corning Gorilla Armor titanium frame for ultimate durability.",
        "price": 129999.0,
        "stock": 8,
        "image_url": "https://images.unsplash.com/photo-1598327105666-5b89351aff97?auto=format&fit=crop&w=800&q=80",
        "is_active": True
    },
    {
        "merchant_id": 1,
        "name": "Logitech MX Master 3S Wireless Performance Mouse",
        "description": "Quiet Clicks and 8,000 DPI any-surface tracking, including glass. MagSpeed electromagnetic scrolling delivers remarkable speed, precision, and near-silence.",
        "price": 8995.0,
        "stock": 30,
        "image_url": "https://images.unsplash.com/photo-1527864550417-7fd91fc51a46?auto=format&fit=crop&w=800&q=80",
        "is_active": True
    },
    {
        "merchant_id": 2,
        "name": "Noise ColorFit Pulse Grand Smartwatch (1.69\" HD Display)",
        "description": "1.69-inch LCD touch screen, 60 sports modes, 150+ customizable cloud-based watch faces, 24/7 heart rate monitor, stress tracker, and IP68 waterproof rating.",
        "price": 1499.0,
        "stock": 45,
        "image_url": "https://images.unsplash.com/photo-1523275335684-37898b6baf30?auto=format&fit=crop&w=800&q=80",
        "is_active": True
    },
    {
        "merchant_id": 2,
        "name": "Apple AirPods Pro (2nd Generation with USB-C)",
        "description": "Up to 2x more Active Noise Cancellation, Adaptive Audio, Transparency mode, and Personalized Spatial Audio with dynamic head tracking. MagSafe Charging Case with speaker and lanyard loop.",
        "price": 24900.0,
        "stock": 18,
        "image_url": "https://images.unsplash.com/photo-1590658268037-6bf12165a8df?auto=format&fit=crop&w=800&q=80",
        "is_active": True
    },
    {
        "merchant_id": 2,
        "name": "Marshall Emberton II Portable Bluetooth Speaker",
        "description": "Rich, clear, and loud 360-degree True Stereophonic sound. 30+ hours of portable playtime on a single charge. Tough IP67 dust and water-resistance design.",
        "price": 14999.0,
        "stock": 15,
        "image_url": "https://images.unsplash.com/photo-1545454675-3531b543be5d?auto=format&fit=crop&w=800&q=80",
        "is_active": True
    },
    {
        "merchant_id": 2,
        "name": "Anker PowerCore 20,000mAh 20W Fast Power Bank",
        "description": "Ultra-high capacity external battery pack with PowerIQ high-speed USB-C charging. Provides over 4 full charges for smartphones and tablets simultaneously.",
        "price": 2999.0,
        "stock": 50,
        "image_url": "https://images.unsplash.com/photo-1609592426505-184cfd2925b4?auto=format&fit=crop&w=800&q=80",
        "is_active": True
    },
    {
        "merchant_id": 3,
        "name": "Nike Air Zoom Pegasus 40 Road Running Shoes",
        "description": "Responsive cushioning with Nike React foam technology and two Zoom Air units. Engineered mesh upper provides lightweight breathability for everyday runners.",
        "price": 6495.0,
        "stock": 20,
        "image_url": "https://images.unsplash.com/photo-1542291026-7eec264c27ff?auto=format&fit=crop&w=800&q=80",
        "is_active": True
    },
    {
        "merchant_id": 3,
        "name": "Wildcraft 35L Water-Resistant Laptop Backpack",
        "description": "Ergonomic multi-compartment backpack with padded 15.6-inch laptop sleeve, breathable back padding, reinforced haul loop, and water-repellent polyester fabric.",
        "price": 1899.0,
        "stock": 40,
        "image_url": "https://images.unsplash.com/photo-1553062407-98eeb64c6a62?auto=format&fit=crop&w=800&q=80",
        "is_active": True
    },
    {
        "merchant_id": 3,
        "name": "Milton Thermosteel 1000ml Double Wall Flask",
        "description": "100% rust-proof stainless steel vacuum insulated flask. Keeps beverages hot or cold for 24 hours. Leak-proof flip lid with convenient carrying strap.",
        "price": 899.0,
        "stock": 60,
        "image_url": "https://images.unsplash.com/photo-1602143407151-7111542de6e8?auto=format&fit=crop&w=800&q=80",
        "is_active": True
    },
    {
        "merchant_id": 3,
        "name": "Minimalist Warm LED Desk Lamp with Touch Dimmer",
        "description": "Modern Scandinavian style desk lamp featuring 3 brightness modes, flicker-free eye-care illumination, adjustable gooseneck arm, and USB powered operation.",
        "price": 1199.0,
        "stock": 35,
        "image_url": "https://images.unsplash.com/photo-1507473885765-e6ed057f782c?auto=format&fit=crop&w=800&q=80",
        "is_active": True
    },
    {
        "merchant_id": 3,
        "name": "Handcrafted Artisan Ceramic Coffee Mug (Set of 2)",
        "description": "350ml microwave and dishwasher safe stoneware ceramic coffee mugs with comfortable grip handles, matte glazed exterior, and artisan speckled finish.",
        "price": 649.0,
        "stock": 50,
        "image_url": "https://images.unsplash.com/photo-1514432324607-a09d9b4aefdd?auto=format&fit=crop&w=800&q=80",
        "is_active": True
    },
    {
        "merchant_id": 3,
        "name": "Premium High-Density Non-Slip Yoga & Fitness Mat",
        "description": "6mm extra thick eco-friendly TPE yoga mat with dual-sided non-slip texture, alignment grid lines, and free carrying strap for home workouts.",
        "price": 1299.0,
        "stock": 30,
        "image_url": "https://images.unsplash.com/photo-1601925260368-ae2f83cf8b7f?auto=format&fit=crop&w=800&q=80",
        "is_active": True
    },
    {
        "merchant_id": 3,
        "name": "Men's RFID Blocking Genuine Vintage Leather Wallet",
        "description": "Handcrafted full-grain bifold leather wallet featuring 8 credit card slots, 2 currency compartments, quick access ID window, and built-in RFID security shield.",
        "price": 1399.0,
        "stock": 45,
        "image_url": "https://images.unsplash.com/photo-1627123424574-724758594e93?auto=format&fit=crop&w=800&q=80",
        "is_active": True
    },
    {
        "merchant_id": 3,
        "name": "Classic 100% Organic Ring-Spun Cotton Crewneck T-Shirt",
        "description": "180 GSM premium combed cotton t-shirt with bio-wash finish for superior softness, breathable all-day comfort, and reinforced ribbed collar.",
        "price": 549.0,
        "stock": 100,
        "image_url": "https://images.unsplash.com/photo-1521572267360-ee0c2909d518?auto=format&fit=crop&w=800&q=80",
        "is_active": True
    }
]

def main():
    db = SessionLocal()
    try:
        # Check available merchants
        merchants = {m.id: m for m in db.query(MerchantUser).all()}
        if not merchants:
            print("No merchants found in database.")
            return

        fallback_merchant_id = list(merchants.keys())[0]

        # Reset product catalog
        deleted_count = db.query(Product).delete()
        db.commit()
        print(f"Cleared {deleted_count} old products.")

        # Seed real products
        added_count = 0
        for pdata in REAL_PRODUCTS:
            m_id = pdata["merchant_id"] if pdata["merchant_id"] in merchants else fallback_merchant_id
            product = Product(
                merchant_id=m_id,
                name=pdata["name"],
                description=pdata["description"],
                price=pdata["price"],
                stock=pdata["stock"],
                image_url=pdata["image_url"],
                is_active=pdata["is_active"]
            )
            db.add(product)
            added_count += 1

        db.commit()
        print(f"Successfully seeded {added_count} real products with images and full details!")

        # Print summary
        prods = db.query(Product).all()
        print("\n--- Current Storefront Catalog ---")
        for p in prods:
            m_name = merchants.get(p.merchant_id).store_name if p.merchant_id in merchants else "Store"
            print(f"[{p.id}] {p.name} — Rs.{p.price:.2f} (Stock: {p.stock}) | Merchant: {m_name}")

    finally:
        db.close()

if __name__ == "__main__":
    main()
