import os
import secrets
import qrcode


ASSETS = os.path.join("assets", "qr")
os.makedirs(ASSETS, exist_ok=True)

def issue_coupon_qr(session_id: int):
    token = secrets.token_urlsafe(16)
    payload = f"coupon:{session_id}:{token}"
    img = qrcode.make(payload)
    path = os.path.join(ASSETS, f"{token}.png")
    img.save(path)
    return token, path