import socket
import time
import hashlib
import json

# --- إعدادات الشبكة الشبح والمكسب ---
SECRET_KEY = "GHOST_NET_2026_nbylh5425"
AD_PROFIT_RATE = 0.0035  # المكسب لكل ظهور إعلان
BEACON_PORT = 9999

def generate_signal():
    """توليد نبضة مشفرة لإشارة الاتصال"""
    timestamp = str(int(time.time()))
    signature = hashlib.sha256((timestamp + SECRET_KEY).encode()).hexdigest()
    return signature[:16]

def save_profit(total_ads):
    """تسجيل الأرباح في ملف عشان السيرفر يقرأه"""
    profit_data = {
        "user": "nbylh5425-prog",
        "total_ads_shown": total_ads,
        "total_profit_usd": total_ads * AD_PROFIT_RATE,
        "last_sync": time.ctime()
    }
    with open("profit_ledger.json", "w") as f:
        json.dump(profit_data, f, indent=4)
    return profit_data

def start_engine():
    print("🚀 Ghost-Net Engine Started...")
    print(f"Target Repository: caller-free")
    
    ads_counter = 0
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    
    try:
        while True:
            # 1. إرسال النبضة (إشارة الاتصال)
            pulse = generate_signal()
            print(f"📡 Sending Pulse: {pulse} | Status: Online")
            
            # 2. محاكاة ظهور إعلان (Offline Ad Caching)
            ads_counter += 1
            data = save_profit(ads_counter)
            
            print(f"💰 Ad Shown! Current Profit: ${data['total_profit_usd']:.4f}")
            
            # إرسال نبضة كل 5 ثواني (عشان جيت هاب ميبندش السكريبت)
            time.sleep(5)
            
    except KeyboardInterrupt:
        print("\nStopping Engine... Final Profit Saved.")

if __name__ == "__main__":
    start_engine()

