import os
import uuid
import random
import logging
import secrets
import io
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import List, Optional, Any
from zoneinfo import ZoneInfo

import jwt
import bcrypt
import requests
from dotenv import load_dotenv
from fastapi import FastAPI, APIRouter, Depends, HTTPException, Header, UploadFile, File, Query, Response
from fastapi.responses import StreamingResponse
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
from pydantic import BaseModel, Field
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]
JWT_SECRET = os.environ.get("JWT_SECRET", "dev_secret_change_me")
ADMIN_MOBILE = os.environ.get("ADMIN_MOBILE", "")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
EMERGENT_KEY = os.environ.get("EMERGENT_LLM_KEY")
STORAGE_BASE = (os.environ.get("INTEGRATION_PROXY_URL") or "").strip() or "https://integrations.emergentagent.com"
STORAGE_URL = STORAGE_BASE.rstrip("/") + "/objstore/api/v1/storage"
APP_NAME = "silver-agency"

client = AsyncIOMotorClient(MONGO_URL)
db = client[DB_NAME]

app = FastAPI(title="silver agency Lottery API")
api = APIRouter(prefix="/api")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ---- Storage ----
_storage_key: Optional[str] = None

def init_storage(force: bool = False) -> Optional[str]:
    global _storage_key
    if _storage_key and not force:
        return _storage_key
    if not EMERGENT_KEY:
        return None
    try:
        r = requests.post(f"{STORAGE_URL}/init", json={"emergent_key": EMERGENT_KEY}, timeout=30)
        r.raise_for_status()
        _storage_key = r.json()["storage_key"]
    except Exception as e:
        logger.error(f"Storage init failed: {e}")
        _storage_key = None
    return _storage_key


def put_object(path: str, data: bytes, content_type: str) -> dict:
    key = init_storage()
    if not key:
        raise HTTPException(500, "Storage not initialized")
    r = requests.put(f"{STORAGE_URL}/objects/{path}",
                     headers={"X-Storage-Key": key, "Content-Type": content_type},
                     data=data, timeout=120)
    if r.status_code == 404:
        key = init_storage(force=True)
        r = requests.put(f"{STORAGE_URL}/objects/{path}",
                         headers={"X-Storage-Key": key, "Content-Type": content_type},
                         data=data, timeout=120)
    r.raise_for_status()
    return r.json()


def get_object(path: str):
    key = init_storage()
    if not key:
        raise HTTPException(500, "Storage not initialized")
    r = requests.get(f"{STORAGE_URL}/objects/{path}",
                     headers={"X-Storage-Key": key}, timeout=60)
    if r.status_code == 404:
        key = init_storage(force=True)
        r = requests.get(f"{STORAGE_URL}/objects/{path}",
                         headers={"X-Storage-Key": key}, timeout=60)
    r.raise_for_status()
    return r.content, r.headers.get("Content-Type", "application/octet-stream")


# ---- Helpers ----
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_id() -> str:
    return str(uuid.uuid4())


def make_ref_code(mobile: str) -> str:
    return f"PA{mobile[-4:]}{secrets.token_hex(2).upper()}"


def create_jwt(user_id: str) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {"sub": user_id, "iat": now.timestamp(), "exp": (now + timedelta(minutes=10)).timestamp()},
        JWT_SECRET, algorithm="HS256"
    )


async def current_user(authorization: str = Header(None)) -> dict:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing token")
    token = authorization.split(" ", 1)[1]
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except Exception:
        raise HTTPException(401, "Invalid token")
    user = await db.profiles.find_one({"id": payload["sub"]}, {"_id": 0, "password_hash": 0, "recovery_hash": 0})
    if not user:
        raise HTTPException(401, "User not found")
    if user.get("is_blocked"):
        raise HTTPException(403, "Account is blocked")
    if not user.get("is_admin") and user.get("is_approved") is False:
        raise HTTPException(403, "Account awaiting admin approval")
    return user


async def admin_user(user: dict = Depends(current_user)) -> dict:
    if not user.get("is_admin"):
        raise HTTPException(403, "Admin only")
    return user


# ---- Constants: Pricing ----
LOTTERY_CONFIG = {
    "dear": {"name": "Dear Lottery", "timings": ["01:00 PM", "06:00 PM", "08:00 PM"], "close_minutes": 10},
    "kerala": {"name": "Kerala Lottery", "timings": ["03:00 PM"], "close_minutes": 5},
}

PRICING = {
    "single": {
        "positions": ["A", "B", "C"],
        "digits": 1,
        "max_qty": 500,
        "bundles": {
            "base": {"price": 10.70, "prizes": {"match": 100}, "label": "₹10.70"}
        },
    },
    "double": {
        "positions": ["AB", "BC", "AC"],
        "digits": 2,
        "max_qty": 100,
        "bundles": {
            "base": {"price": 10.70, "prizes": {"match": 1000}, "label": "₹10.70"}
        },
    },
    "triple": {
        "positions": ["ABC"],
        "digits": 3,
        "max_qty": 40,
        "bundles": {
            "base": {"price": 11.70, "prizes": {"abc": 6250, "bc": 250, "c": 25}, "label": "₹11.70"},
            "b28": {"price": 28.0, "prizes": {"abc": 15000, "bc": 500, "c": 50}, "label": "₹28"},
            "b30": {"price": 30.0, "prizes": {"abc": 17500, "bc": 500, "c": 50}, "label": "₹30"},
            "b33": {"price": 33.0, "prizes": {"abc": 20000, "bc": 500, "c": 50}, "label": "₹33", "kerala_only": True},
            "b55": {"price": 55.0, "prizes": {"abc": 30000, "bc": 1000, "c": 100}, "label": "₹55"},
            "b60": {"price": 60.0, "prizes": {"abc": 35000, "bc": 1000, "c": 100}, "label": "₹60"},
            "b65": {"price": 65.0, "prizes": {"abc": 40000, "bc": 1000, "c": 100}, "label": "₹65", "kerala_only": True},
        },
    },
    "xabc": {
        "positions": ["XABC"],
        "digits": 4,
        "max_qty": 40,
        "kerala_only": True,
        "bundles": {
            "base": {"price": 20.0, "prizes": {"xabc": 100000}, "label": "₹20"},
            "b50": {"price": 50.0, "prizes": {"xabc": 250000, "abc": 5000, "bc": 500, "c": 50}, "label": "₹50"},
            "b100": {"price": 100.0, "prizes": {"xabc": 500000, "abc": 10000, "bc": 1000, "c": 100}, "label": "₹100"},
        },
    },
}

# ---- Models ----
class SignupReq(BaseModel):
    mobile: str
    username: str
    password: str
    referral_code: Optional[str] = None
    recovery_code: Optional[str] = None

class LoginReq(BaseModel):
    mobile: str
    password: str

class ForgotPasswordReq(BaseModel):
    mobile: str
    recovery_code: str
    new_password: str

class AdminLoginReq(BaseModel):
    mobile: str
    password: str

class TicketNumber(BaseModel):
    position: str  # e.g. "A", "AB", "ABC", "XABC"
    digits: str    # e.g. "5", "63", "635", "5635"

class BookTicketsReq(BaseModel):
    lottery_type: str
    lottery_time: str
    lottery_date: str  # YYYY-MM-DD
    items: List[dict]  # each: {digit_type, numbers:[{position,digits}], quantity}

class RechargeReq(BaseModel):
    amount: float
    screenshot_path: str

class WithdrawReq(BaseModel):
    amount: float

class AccountDetailsReq(BaseModel):
    payout_type: str  # upi or bank
    upi_id: Optional[str] = None
    account_number: Optional[str] = None
    ifsc_code: Optional[str] = None
    account_name: Optional[str] = None

class NotifyReq(BaseModel):
    title: str
    message: str
    is_popup: bool = False

class SettingsReq(BaseModel):
    agency_name: Optional[str] = None
    upi_id: Optional[str] = None
    qr_code_url: Optional[str] = None

class ResultReq(BaseModel):
    lottery_type: str
    lottery_time: str
    lottery_date: str
    winning_number: str  # e.g. "0623" for 4-digit or "062" for 3-digit


class StatementReq(BaseModel):
    lottery_type: str
    lottery_time: str
    lottery_date: str


def _hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _check_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode("utf-8"), hashed.encode("utf-8"))
    except Exception:
        return False


def _draw_past_cutoff(lottery_type: str, lottery_time: str, lottery_date: str) -> bool:
    """Return True if lottery_date is in the past OR (today and past cutoff). Uses IST."""
    ist = ZoneInfo("Asia/Kolkata")
    now_ist = datetime.now(ist)
    today = now_ist.date().isoformat()
    if lottery_date < today:
        return True
    if lottery_date > today:
        return False
    close_minutes = LOTTERY_CONFIG.get(lottery_type, {}).get("close_minutes", 10)
    try:
        parts = lottery_time.split(" ")
        hm = parts[0]; ap = parts[1]
        h, m = [int(x) for x in hm.split(":")]
        if ap == "PM" and h != 12: h += 12
        if ap == "AM" and h == 12: h = 0
    except Exception:
        return False
    draw_dt = now_ist.replace(hour=h, minute=m, second=0, microsecond=0)
    cutoff = draw_dt - timedelta(minutes=close_minutes)
    return now_ist >= cutoff


@api.post("/auth/signup")
async def signup(req: SignupReq):
    mobile = req.mobile.strip()
    if not (mobile.isdigit() and len(mobile) == 10):
        raise HTTPException(400, "Invalid mobile")
    if not req.username.strip():
        raise HTTPException(400, "Username required")
    if len(req.password) < 4:
        raise HTTPException(400, "Password too short")
    existing = await db.profiles.find_one({"mobile": mobile})
    if existing:
        raise HTTPException(400, "Mobile already registered")
    ref_code = make_ref_code(mobile)
    while await db.profiles.find_one({"referral_code": ref_code}):
        ref_code = make_ref_code(mobile)
    referrer_id = None
    if req.referral_code:
        ref = await db.profiles.find_one({"referral_code": req.referral_code.upper()}, {"_id": 0})
        if ref:
            referrer_id = ref["id"]
    is_admin = mobile == ADMIN_MOBILE
    profile = {
        "id": make_id(),
        "mobile": mobile,
        "username": req.username.strip(),
        "password_hash": _hash_password(req.password),
        "recovery_hash": _hash_password(req.recovery_code.strip()) if req.recovery_code and req.recovery_code.strip() else None,
        "wallet_balance": 0.0,
        "bonus_cash": 10.0 if referrer_id else 0.0,  # Referral code pottu vandha new user-ku ₹10
        "is_blocked": False,
        "is_admin": is_admin,
        "is_approved": is_admin,  # admins auto-approved; regular users need admin approval
        "referral_code": ref_code,
        "referred_by": referrer_id,
        "created_at": now_iso(),
    }
    await db.profiles.insert_one(profile)
    if referrer_id:
        await db.profiles.update_one({"id": referrer_id}, {"$inc": {"bonus_cash": 10.0}}) # Refer pannavangaluku ₹10
        await db.referrals.insert_one({
            "id": make_id(), "referrer_id": referrer_id,
            "referred_mobile": mobile, "referred_id": profile["id"],
            "bonus_awarded": 10.0, "created_at": now_iso() # History-la ₹10 record aagum
        })
        await db.notifications.insert_one({
            "id": make_id(), "user_id": referrer_id,
            "title": "Referral Bonus!",
            "message": f"You earned ₹50 bonus cash for referring {mobile}",
            "is_read": False, "is_popup": False, "created_at": now_iso()
        })
    profile.pop("_id", None); profile.pop("password_hash", None); profile.pop("recovery_hash", None)
    # Regular users: no token issued until admin approves. Notify admin(s).
    if not profile["is_admin"]:
        admin_ids = await db.profiles.find({"is_admin": True}, {"id": 1, "_id": 0}).to_list(50)
        for a in admin_ids:
            await db.notifications.insert_one({
                "id": make_id(), "user_id": a["id"],
                "title": "New Signup Awaiting Approval",
                "message": f"{profile['username']} (+91 {profile['mobile']}) has signed up.",
                "is_read": False, "is_popup": True, "created_at": now_iso()
            })
        return {"pending_approval": True, "user": profile}
    return {"token": create_jwt(profile["id"]), "user": profile}


@api.post("/auth/login")
async def login(req: LoginReq):
    mobile = req.mobile.strip()
    user = await db.profiles.find_one({"mobile": mobile})
    if not user or not user.get("password_hash"):
        raise HTTPException(401, "Invalid credentials")
    if user.get("is_blocked"):
        raise HTTPException(403, "Account is blocked")
    if not _check_password(req.password, user["password_hash"]):
        raise HTTPException(401, "Invalid credentials")
    if not user.get("is_admin") and not user.get("is_approved"):
        raise HTTPException(403, "Account awaiting admin approval")
    user.pop("_id", None); user.pop("password_hash", None); user.pop("recovery_hash", None)
    return {"token": create_jwt(user["id"]), "user": user}


@api.post("/auth/forgot-password")
async def forgot_password(req: ForgotPasswordReq):
    mobile = req.mobile.strip()
    if len(req.new_password) < 4:
        raise HTTPException(400, "Password too short")
    user = await db.profiles.find_one({"mobile": mobile})
    if not user:
        raise HTTPException(404, "Mobile not registered")
    if user.get("is_blocked"):
        raise HTTPException(403, "Account is blocked")
    if not user.get("recovery_hash"):
        raise HTTPException(400, "No recovery code set - contact admin")
    if not _check_password(req.recovery_code.strip(), user["recovery_hash"]):
        raise HTTPException(401, "Invalid recovery code")
    await db.profiles.update_one({"id": user["id"]}, {"$set": {"password_hash": _hash_password(req.new_password)}})
    await db.password_resets.insert_one({
        "id": make_id(),
        "user_id": user["id"],
        "mobile": mobile,
        "username": user.get("username"),
        "method": "recovery_code",
        "created_at": now_iso(),
    })
    return {"success": True}


@api.post("/auth/admin-login")
async def admin_login(req: AdminLoginReq):
    if not ADMIN_MOBILE or not ADMIN_PASSWORD:
        raise HTTPException(500, "Admin not configured")
    if req.mobile.strip() != ADMIN_MOBILE or req.password != ADMIN_PASSWORD:
        raise HTTPException(401, "Invalid admin credentials")
    user = await db.profiles.find_one({"mobile": ADMIN_MOBILE}, {"_id": 0})
    if not user:
        ref_code = make_ref_code(ADMIN_MOBILE)
        while await db.profiles.find_one({"referral_code": ref_code}):
            ref_code = make_ref_code(ADMIN_MOBILE)
        user = {
            "id": make_id(),
            "mobile": ADMIN_MOBILE,
            "username": "Admin",
            "wallet_balance": 0.0,
            "bonus_cash": 0.0,
            "is_blocked": False,
            "is_admin": True,
            "referral_code": ref_code,
            "referred_by": None,
            "created_at": now_iso(),
        }
        await db.profiles.insert_one(user)
        user.pop("_id", None)
    elif not user.get("is_admin"):
        await db.profiles.update_one({"id": user["id"]}, {"$set": {"is_admin": True}})
        user["is_admin"] = True
    return {"token": create_jwt(user["id"]), "user": user}


@api.get("/auth/me")
async def me(user: dict = Depends(current_user)):
    return user


@api.post("/auth/refresh")
async def refresh_token(user: dict = Depends(current_user)):
    return {"token": create_jwt(user["id"]), "user": user}


# ---- Lottery Config ----
@api.get("/lottery/config")
async def lottery_config():
    return {"lotteries": LOTTERY_CONFIG, "pricing": PRICING}


@api.get("/lottery/locked-draws")
async def locked_draws():
    docs = await db.draw_statements.find({}, {"_id": 0, "lottery_type": 1, "lottery_time": 1, "lottery_date": 1}).to_list(500)
    return docs


# ---- Tickets ----
def _get_bundle(digit_type: str, bundle: str):
    p = PRICING.get(digit_type)
    if not p:
        raise HTTPException(400, "Invalid digit type")
    b = p["bundles"].get(bundle)
    if not b:
        raise HTTPException(400, f"Invalid bundle for {digit_type}")
    return p, b


@api.post("/tickets/book")
async def book_tickets(req: BookTicketsReq, user: dict = Depends(current_user)):
    if req.lottery_type not in LOTTERY_CONFIG:
        raise HTTPException(400, "Invalid lottery")
    if req.lottery_time not in LOTTERY_CONFIG[req.lottery_type]["timings"]:
        raise HTTPException(400, "Invalid time")
    locked = await db.draw_statements.find_one({
        "lottery_type": req.lottery_type,
        "lottery_time": req.lottery_time,
        "lottery_date": req.lottery_date,
    })
    if locked:
        raise HTTPException(400, "Bookings closed - draw statement generated")
    if _draw_past_cutoff(req.lottery_type, req.lottery_time, req.lottery_date):
        raise HTTPException(400, "Draw cutoff has passed - bookings closed")
    total_amount = 0.0
    tickets_to_insert = []
    for item in req.items:
        dt = item.get("digit_type")
        bundle_id = item.get("bundle", "base")
        p, b = _get_bundle(dt, bundle_id)
        if p.get("kerala_only") and req.lottery_type != "kerala":
            raise HTTPException(400, f"{dt} is Kerala only")
        if b.get("kerala_only") and req.lottery_type != "kerala":
            raise HTTPException(400, f"{b['label']} bundle is Kerala only")
        qty = int(item.get("quantity", 1))
        if qty < 1 or qty > p["max_qty"]:
            raise HTTPException(400, f"Invalid quantity for {dt}")
        numbers = item.get("numbers", [])
        if not numbers:
            raise HTTPException(400, "Numbers required")
        for n in numbers:
            digits = str(n.get("digits", ""))
            if len(digits) != p["digits"] or not digits.isdigit():
                raise HTTPException(400, f"Invalid digits for {dt}")
            if n.get("position") not in p["positions"]:
                raise HTTPException(400, f"Invalid position for {dt}")
        amount = round(b["price"] * qty * len(numbers), 2)
        total_amount = round(total_amount + amount, 2)
        tickets_to_insert.append({
            "id": make_id(),
            "user_id": user["id"],
            "lottery_type": req.lottery_type,
            "lottery_time": req.lottery_time,
            "lottery_date": req.lottery_date,
            "digit_type": dt,
            "bundle": bundle_id,
            "numbers": numbers,
            "quantity": qty,
            "amount": amount,
            "status": "booked",
            "won_amount": 0.0,
            "created_at": now_iso(),
        })
    # deduct wallet_balance then bonus_cash
    wb = float(user.get("wallet_balance", 0))
    bc = float(user.get("bonus_cash", 0))
    if wb + bc < total_amount:
        raise HTTPException(400, "Insufficient balance")
    from_wallet = min(wb, total_amount)
    from_bonus = round(total_amount - from_wallet, 2)
    await db.profiles.update_one(
        {"id": user["id"]},
        {"$inc": {"wallet_balance": -from_wallet, "bonus_cash": -from_bonus}}
    )
    if tickets_to_insert:
        await db.tickets.insert_many(tickets_to_insert)
    return {"success": True, "total_amount": total_amount, "count": len(tickets_to_insert)}


@api.get("/tickets/my")
async def my_tickets(user: dict = Depends(current_user),
                     lottery_type: Optional[str] = None,
                     lottery_time: Optional[str] = None,
                     lottery_date: Optional[str] = None):
    q = {"user_id": user["id"]}
    if lottery_type: q["lottery_type"] = lottery_type
    if lottery_time: q["lottery_time"] = lottery_time
    if lottery_date: q["lottery_date"] = lottery_date
    tickets = await db.tickets.find(q, {"_id": 0}).sort("created_at", -1).to_list(500)
    return tickets


# ---- Recharge ----
@api.post("/recharge/request")
async def create_recharge(req: RechargeReq, user: dict = Depends(current_user)):
    if req.amount < 10:
        raise HTTPException(400, "Minimum ₹10")
    doc = {
        "id": make_id(),
        "user_id": user["id"],
        "amount": float(req.amount),
        "screenshot_path": req.screenshot_path,
        "status": "pending",
        "admin_note": "",
        "created_at": now_iso(),
        "reviewed_at": None,
    }
    await db.recharge_requests.insert_one(doc)
    doc.pop("_id", None)
    return doc


@api.get("/recharge/my")
async def my_recharges(user: dict = Depends(current_user)):
    return await db.recharge_requests.find({"user_id": user["id"]}, {"_id": 0}).sort("created_at", -1).to_list(200)


# ---- Withdrawal ----
@api.post("/withdrawal/request")
async def create_withdrawal(req: WithdrawReq, user: dict = Depends(current_user)):
    if req.amount < 100:
        raise HTTPException(400, "Minimum withdrawal ₹100")
    if float(user.get("wallet_balance", 0)) < req.amount:
        raise HTTPException(400, "Insufficient wallet balance")
    acc = await db.account_details.find_one({"user_id": user["id"]}, {"_id": 0})
    if not acc:
        raise HTTPException(400, "Add account details first")
    doc = {
        "id": make_id(),
        "user_id": user["id"],
        "amount": float(req.amount),
        "payout_type": acc.get("payout_type", "upi"),
        "upi_id": acc.get("upi_id"),
        "account_number": acc.get("account_number"),
        "ifsc_code": acc.get("ifsc_code"),
        "account_name": acc.get("account_name"),
        "status": "pending",
        "admin_note": "",
        "created_at": now_iso(),
        "reviewed_at": None,
    }
    await db.profiles.update_one({"id": user["id"]}, {"$inc": {"wallet_balance": -float(req.amount)}})
    await db.withdrawal_requests.insert_one(doc)
    doc.pop("_id", None)
    return doc


@api.get("/withdrawal/my")
async def my_withdrawals(user: dict = Depends(current_user)):
    return await db.withdrawal_requests.find({"user_id": user["id"]}, {"_id": 0}).sort("created_at", -1).to_list(200)


# ---- Account Details ----
@api.get("/account")
async def get_account(user: dict = Depends(current_user)):
    acc = await db.account_details.find_one({"user_id": user["id"]}, {"_id": 0})
    pending = await db.account_change_requests.find_one({"user_id": user["id"], "status": "pending"}, {"_id": 0})
    return {"account": acc, "pending_change": pending}


@api.post("/account")
async def set_or_change_account(req: AccountDetailsReq, user: dict = Depends(current_user)):
    existing = await db.account_details.find_one({"user_id": user["id"]})
    if not existing:
        doc = {
            "id": make_id(),
            "user_id": user["id"],
            "payout_type": req.payout_type,
            "upi_id": req.upi_id,
            "account_number": req.account_number,
            "ifsc_code": req.ifsc_code,
            "account_name": req.account_name,
            "created_at": now_iso(),
        }
        await db.account_details.insert_one(doc)
        doc.pop("_id", None)
        return {"account": doc, "pending_change": None}
    pending = await db.account_change_requests.find_one({"user_id": user["id"], "status": "pending"})
    if pending:
        raise HTTPException(400, "Change request already pending")
    doc = {
        "id": make_id(),
        "user_id": user["id"],
        "new_payout_type": req.payout_type,
        "new_upi_id": req.upi_id,
        "new_account_number": req.account_number,
        "new_ifsc_code": req.ifsc_code,
        "new_account_name": req.account_name,
        "status": "pending",
        "created_at": now_iso(),
        "reviewed_at": None,
    }
    await db.account_change_requests.insert_one(doc)
    doc.pop("_id", None)
    return {"account": None, "pending_change": doc}


# ---- Notifications ----
@api.get("/notifications")
async def list_notifications(user: dict = Depends(current_user)):
    return await db.notifications.find({"user_id": user["id"]}, {"_id": 0}).sort("created_at", -1).to_list(200)


@api.get("/notifications/popups")
async def list_popup_notifications(user: dict = Depends(current_user)):
    return await db.notifications.find(
        {"user_id": user["id"], "is_popup": True, "is_read": False}, {"_id": 0}
    ).sort("created_at", -1).to_list(50)


@api.post("/notifications/{nid}/read")
async def mark_read(nid: str, user: dict = Depends(current_user)):
    await db.notifications.update_one({"id": nid, "user_id": user["id"]}, {"$set": {"is_read": True}})
    return {"success": True}


@api.post("/notifications/read-all")
async def mark_all_read(user: dict = Depends(current_user)):
    await db.notifications.update_many({"user_id": user["id"]}, {"$set": {"is_read": True}})
    return {"success": True}


# ---- Referrals ----
@api.get("/referrals/my")
async def my_referrals(user: dict = Depends(current_user)):
    refs = await db.referrals.find({"referrer_id": user["id"]}, {"_id": 0}).sort("created_at", -1).to_list(200)
    return refs


# ---- Settings (public) ----
@api.get("/settings")
async def get_settings():
    docs = await db.admin_settings.find({}, {"_id": 0}).to_list(50)
    settings = {d["key"]: d["value"] for d in docs}
    settings.setdefault("agency_name", "silver agency")
    settings.setdefault("upi_id", "silver@upi")
    settings.setdefault("qr_code_url", "")
    settings.setdefault("marquee", "Welcome to silver agency! Book Kerala & Dear Lottery tickets. Play responsibly.admin number 7305803270")
    return settings


# ---- Uploads ----
@api.post("/upload")
async def upload(file: UploadFile = File(...), user: dict = Depends(current_user)):
    ext = (file.filename or "bin").split(".")[-1].lower() if "." in (file.filename or "") else "bin"
    path = f"{APP_NAME}/uploads/{user['id']}/{uuid.uuid4()}.{ext}"
    data = await file.read()
    result = put_object(path, data, file.content_type or "application/octet-stream")
    await db.files.insert_one({
        "id": make_id(),
        "user_id": user["id"],
        "storage_path": result["path"],
        "content_type": file.content_type,
        "size": result.get("size", len(data)),
        "created_at": now_iso(),
    })
    return {"path": result["path"]}


@api.get("/files/{path:path}")
async def download(path: str, auth: str = Query(None), authorization: str = Header(None)):
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ", 1)[1]
    elif auth:
        token = auth
    if not token:
        raise HTTPException(401, "No token")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except Exception:
        raise HTTPException(401, "Invalid token")
    if not payload.get("sub"):
        raise HTTPException(401, "Invalid token")
    data, ct = get_object(path)
    return Response(content=data, media_type=ct)


# ---- Results ----
def _digit_positions(winning: str) -> dict:
    """Given full winning number (3 or 4 digits), return positional single-digit map."""
    w = winning
    if len(w) == 3:
        return {"A": w[0], "B": w[1], "C": w[2],
                "AB": w[0:2], "BC": w[1:3], "AC": w[0] + w[2], "ABC": w}
    elif len(w) == 4:
        return {"X": w[0], "A": w[1], "B": w[2], "C": w[3],
                "AB": w[1:3], "BC": w[2:4], "AC": w[1] + w[3],
                "ABC": w[1:4], "XABC": w}
    return {}


def _compute_win(ticket: dict, positions: dict) -> float:
    total = 0.0
    dt = ticket["digit_type"]
    bundle_id = ticket.get("bundle", "base")
    p = PRICING.get(dt)
    if not p:
        return 0.0
    b = p["bundles"].get(bundle_id) or p["bundles"]["base"]
    prizes = b["prizes"]
    for n in ticket["numbers"]:
        pos = n["position"]
        digits = str(n["digits"])
        qty = ticket["quantity"]
        if dt == "single":
            if positions.get(pos) == digits:
                total += prizes.get("match", 0) * qty
        elif dt == "double":
            if positions.get(pos) == digits:
                total += prizes.get("match", 0) * qty
        elif dt == "triple":
            if positions.get("ABC") == digits:
                total += prizes.get("abc", 0) * qty
            elif positions.get("BC") == digits[-2:]:
                total += prizes.get("bc", 0) * qty
            elif positions.get("C") == digits[-1:]:
                total += prizes.get("c", 0) * qty
        elif dt == "xabc":
            if positions.get("XABC") == digits:
                total += prizes.get("xabc", 0) * qty
            elif positions.get("ABC") == digits[-3:]:
                total += prizes.get("abc", 0) * qty
            elif positions.get("BC") == digits[-2:]:
                total += prizes.get("bc", 0) * qty
            elif positions.get("C") == digits[-1:]:
                total += prizes.get("c", 0) * qty
    return round(total, 2)


@api.get("/results")
async def public_results():
    return await db.results.find({}, {"_id": 0}).sort("created_at", -1).to_list(200)


# ---- Admin ----
@api.get("/admin/clients")
async def admin_clients(q: Optional[str] = None, _: dict = Depends(admin_user)):
    query: dict = {}
    if q:
        query = {"$or": [{"username": {"$regex": q, "$options": "i"}}, {"mobile": {"$regex": q}}]}
    return await db.profiles.find(query, {"_id": 0, "password_hash": 0, "recovery_hash": 0}).sort("created_at", -1).to_list(500)


@api.get("/admin/password-resets")
async def admin_password_resets(_: dict = Depends(admin_user), days: int = 30, limit: int = 200):
    from datetime import timedelta
    since = (datetime.now(timezone.utc) - timedelta(days=int(days))).isoformat()
    items = await db.password_resets.find(
        {"created_at": {"$gte": since}}, {"_id": 0}
    ).sort("created_at", -1).to_list(int(limit))
    total = await db.password_resets.count_documents({"created_at": {"$gte": since}})
    # buckets by date (YYYY-MM-DD)
    buckets: dict = {}
    for it in items:
        d = it["created_at"][:10]
        buckets[d] = buckets.get(d, 0) + 1
    return {"total": total, "items": items, "by_date": [{"date": k, "count": v} for k, v in sorted(buckets.items(), reverse=True)]}


@api.get("/admin/winners")
async def admin_winners(_: dict = Depends(admin_user),
                        lottery_type: Optional[str] = None,
                        lottery_time: Optional[str] = None,
                        lottery_date: Optional[str] = None,
                        limit: int = 200):
    q = {"status": "won", "won_amount": {"$gt": 0}}
    if lottery_type: q["lottery_type"] = lottery_type
    if lottery_time: q["lottery_time"] = lottery_time
    if lottery_date: q["lottery_date"] = lottery_date
    tickets = await db.tickets.find(q, {"_id": 0}).sort("created_at", -1).to_list(int(limit))
    for t in tickets:
        u = await db.profiles.find_one({"id": t["user_id"]}, {"_id": 0, "username": 1, "mobile": 1})
        t["user"] = u
    return tickets


@api.post("/admin/clients/{uid}/block")
async def admin_block(uid: str, block: bool = True, _: dict = Depends(admin_user)):
    await db.profiles.update_one({"id": uid}, {"$set": {"is_blocked": block}})
    return {"success": True}


@api.post("/admin/clients/{uid}/approve")
async def admin_approve(uid: str, approve: bool = True, _: dict = Depends(admin_user)):
    user = await db.profiles.find_one({"id": uid})
    if not user:
        raise HTTPException(404, "User not found")
    await db.profiles.update_one({"id": uid}, {"$set": {"is_approved": bool(approve)}})
    await db.notifications.insert_one({
        "id": make_id(), "user_id": uid,
        "title": "Account Approved" if approve else "Account Approval Revoked",
        "message": "Your account is approved. You can now login." if approve else "Your account approval has been revoked. Contact admin.",
        "is_read": False, "is_popup": True, "created_at": now_iso()
    })
    return {"success": True}


class AdminPasswordResetReq(BaseModel):
    new_password: str


@api.post("/admin/clients/{uid}/reset-password")
async def admin_reset_password(uid: str, req: AdminPasswordResetReq, admin: dict = Depends(admin_user)):
    if len(req.new_password) < 4:
        raise HTTPException(400, "Password too short")
    user = await db.profiles.find_one({"id": uid})
    if not user:
        raise HTTPException(404, "User not found")
    await db.profiles.update_one({"id": uid}, {"$set": {"password_hash": _hash_password(req.new_password)}})
    await db.password_resets.insert_one({
        "id": make_id(),
        "user_id": uid,
        "mobile": user.get("mobile"),
        "username": user.get("username"),
        "method": "admin_reset",
        "reset_by": admin.get("id"),
        "created_at": now_iso(),
    })
    await db.notifications.insert_one({
        "id": make_id(), "user_id": uid,
        "title": "Password Reset by Admin",
        "message": "Your password was reset by admin. Please login with the new password.",
        "is_read": False, "is_popup": True, "created_at": now_iso()
    })
    return {"success": True}


@api.get("/admin/recharges")
async def admin_recharges(_: dict = Depends(admin_user)):
    docs = await db.recharge_requests.find({}, {"_id": 0}).sort("created_at", -1).to_list(500)
    for d in docs:
        u = await db.profiles.find_one({"id": d["user_id"]}, {"_id": 0, "username": 1, "mobile": 1})
        d["user"] = u
    return docs


@api.post("/admin/recharges/{rid}/review")
async def admin_review_recharge(rid: str, approve: bool, note: str = "", _: dict = Depends(admin_user)):
    r = await db.recharge_requests.find_one({"id": rid})
    if not r or r["status"] != "pending":
        raise HTTPException(400, "Not pending")
    status = "approved" if approve else "rejected"
    await db.recharge_requests.update_one({"id": rid}, {"$set": {"status": status, "admin_note": note, "reviewed_at": now_iso()}})
    if approve:
        await db.profiles.update_one({"id": r["user_id"]}, {"$inc": {"wallet_balance": float(r["amount"])}})
        await db.notifications.insert_one({
            "id": make_id(), "user_id": r["user_id"],
            "title": "Recharge Approved",
            "message": f"₹{r['amount']} credited to your wallet",
            "is_read": False, "is_popup": True, "created_at": now_iso()
        })
    else:
        await db.notifications.insert_one({
            "id": make_id(), "user_id": r["user_id"],
            "title": "Recharge Rejected",
            "message": note or "Your recharge was rejected",
            "is_read": False, "is_popup": True, "created_at": now_iso()
        })
    return {"success": True}


@api.get("/admin/withdrawals")
async def admin_withdrawals(_: dict = Depends(admin_user)):
    docs = await db.withdrawal_requests.find({}, {"_id": 0}).sort("created_at", -1).to_list(500)
    for d in docs:
        u = await db.profiles.find_one({"id": d["user_id"]}, {"_id": 0, "username": 1, "mobile": 1})
        d["user"] = u
    return docs


@api.post("/admin/withdrawals/{wid}/review")
async def admin_review_withdraw(wid: str, approve: bool, note: str = "", _: dict = Depends(admin_user)):
    w = await db.withdrawal_requests.find_one({"id": wid})
    if not w or w["status"] != "pending":
        raise HTTPException(400, "Not pending")
    status = "approved" if approve else "rejected"
    await db.withdrawal_requests.update_one({"id": wid}, {"$set": {"status": status, "admin_note": note, "reviewed_at": now_iso()}})
    if not approve:
        await db.profiles.update_one({"id": w["user_id"]}, {"$inc": {"wallet_balance": float(w["amount"])}})
    await db.notifications.insert_one({
        "id": make_id(), "user_id": w["user_id"],
        "title": f"Withdrawal {status.title()}",
        "message": note or (f"₹{w['amount']} withdrawal processed" if approve else f"₹{w['amount']} refunded to wallet"),
        "is_read": False, "is_popup": True, "created_at": now_iso()
    })
    return {"success": True}


@api.get("/admin/tickets")
async def admin_tickets(lottery_type: Optional[str] = None, lottery_time: Optional[str] = None,
                        lottery_date: Optional[str] = None, _: dict = Depends(admin_user)):
    q = {}
    if lottery_type: q["lottery_type"] = lottery_type
    if lottery_time: q["lottery_time"] = lottery_time
    if lottery_date: q["lottery_date"] = lottery_date
    docs = await db.tickets.find(q, {"_id": 0}).sort("created_at", -1).to_list(1000)
    for d in docs:
        u = await db.profiles.find_one({"id": d["user_id"]}, {"_id": 0, "username": 1, "mobile": 1})
        d["user"] = u
    return docs


@api.get("/admin/ac-changes")
async def admin_ac_changes(_: dict = Depends(admin_user)):
    docs = await db.account_change_requests.find({}, {"_id": 0}).sort("created_at", -1).to_list(500)
    for d in docs:
        u = await db.profiles.find_one({"id": d["user_id"]}, {"_id": 0, "username": 1, "mobile": 1})
        d["user"] = u
    return docs


@api.post("/admin/ac-changes/{cid}/review")
async def admin_review_ac(cid: str, approve: bool, _: dict = Depends(admin_user)):
    c = await db.account_change_requests.find_one({"id": cid})
    if not c or c["status"] != "pending":
        raise HTTPException(400, "Not pending")
    status = "approved" if approve else "rejected"
    await db.account_change_requests.update_one({"id": cid}, {"$set": {"status": status, "reviewed_at": now_iso()}})
    if approve:
        await db.account_details.update_one(
            {"user_id": c["user_id"]},
            {"$set": {
                "payout_type": c.get("new_payout_type"),
                "upi_id": c.get("new_upi_id"),
                "account_number": c.get("new_account_number"),
                "ifsc_code": c.get("new_ifsc_code"),
                "account_name": c.get("new_account_name"),
            }}
        )
    await db.notifications.insert_one({
        "id": make_id(), "user_id": c["user_id"],
        "title": f"Account Change {status.title()}",
        "message": f"Your account change request was {status}",
        "is_read": False, "is_popup": True, "created_at": now_iso()
    })
    return {"success": True}


@api.post("/admin/notify")
async def admin_notify(req: NotifyReq, _: dict = Depends(admin_user)):
    users = await db.profiles.find({"is_admin": {"$ne": True}}, {"id": 1, "_id": 0}).to_list(10000)
    docs = [{
        "id": make_id(), "user_id": u["id"],
        "title": req.title, "message": req.message,
        "is_read": False, "is_popup": req.is_popup, "created_at": now_iso()
    } for u in users]
    if docs:
        await db.notifications.insert_many(docs)
    return {"success": True, "count": len(docs)}


@api.post("/admin/settings")
async def update_settings(req: SettingsReq, _: dict = Depends(admin_user)):
    for k, v in req.model_dump(exclude_none=True).items():
        await db.admin_settings.update_one(
            {"key": k}, {"$set": {"key": k, "value": v, "updated_at": now_iso()}}, upsert=True
        )
    return {"success": True}


@api.post("/admin/results")
async def admin_add_result(req: ResultReq, _: dict = Depends(admin_user)):
    if req.lottery_type not in LOTTERY_CONFIG:
        raise HTTPException(400, "Invalid lottery")
    w = req.winning_number.strip()
    expected_len = 4 if req.lottery_type == "kerala" else 3
    if len(w) != expected_len or not w.isdigit():
        raise HTTPException(400, f"Winning number must be {expected_len} digits")
    existing = await db.results.find_one({
        "lottery_type": req.lottery_type, "lottery_time": req.lottery_time, "lottery_date": req.lottery_date
    })
    if existing:
        raise HTTPException(400, "Result already declared for this draw")
    result_doc = {
        "id": make_id(),
        "lottery_type": req.lottery_type,
        "lottery_time": req.lottery_time,
        "lottery_date": req.lottery_date,
        "winning_number": w,
        "created_at": now_iso(),
    }
    positions = _digit_positions(w)
    # Scan matching tickets
    tickets = await db.tickets.find({
        "lottery_type": req.lottery_type,
        "lottery_time": req.lottery_time,
        "lottery_date": req.lottery_date,
        "status": "booked",
    }).to_list(10000)
    total_credited = 0.0
    winners = 0
    for t in tickets:
        win = _compute_win(t, positions)
        new_status = "won" if win > 0 else "lost"
        await db.tickets.update_one({"id": t["id"]}, {"$set": {"status": new_status, "won_amount": win}})
        if win > 0:
            winners += 1
            total_credited += win
            await db.profiles.update_one({"id": t["user_id"]}, {"$inc": {"wallet_balance": win}})
            await db.notifications.insert_one({
                "id": make_id(), "user_id": t["user_id"],
                "title": "You Won!",
                "message": f"₹{win} credited to wallet for winning {t['digit_type']} ticket",
                "is_read": False, "is_popup": True, "created_at": now_iso()
            })
    result_doc["total_credited"] = total_credited
    result_doc["winners_count"] = winners
    await db.results.insert_one(result_doc)
    result_doc.pop("_id", None)
    return result_doc


@api.get("/admin/statements")
async def list_statements(_: dict = Depends(admin_user)):
    return await db.draw_statements.find({}, {"_id": 0}).sort("created_at", -1).to_list(500)


@api.post("/admin/statements/preview")
async def preview_statement(req: StatementReq, _: dict = Depends(admin_user)):
    if req.lottery_type not in LOTTERY_CONFIG:
        raise HTTPException(400, "Invalid lottery")
    tickets = await db.tickets.find({
        "lottery_type": req.lottery_type,
        "lottery_time": req.lottery_time,
        "lottery_date": req.lottery_date,
    }, {"_id": 0}).to_list(10000)
    for t in tickets:
        u = await db.profiles.find_one({"id": t["user_id"]}, {"_id": 0, "username": 1, "mobile": 1})
        t["user"] = u
    locked = await db.draw_statements.find_one(
        {"lottery_type": req.lottery_type, "lottery_time": req.lottery_time, "lottery_date": req.lottery_date},
        {"_id": 0}
    )
    total = sum(float(t["amount"]) for t in tickets)
    return {"tickets": tickets, "total": round(total, 2), "count": len(tickets), "locked": bool(locked), "locked_at": locked.get("created_at") if locked else None}


DIGIT_WORD = {"single": "single", "double": "double", "triple": "three", "xabc": "four"}


def _bundle_prize(digit_type: str, bundle_key: str) -> int:
    p = PRICING.get(digit_type, {}).get("bundles", {}).get(bundle_key, {})
    prizes = p.get("prizes", {})
    if digit_type in ("single", "double"):
        return int(prizes.get("match", 0))
    if digit_type == "triple":
        return int(prizes.get("abc", 0))
    if digit_type == "xabc":
        return int(prizes.get("xabc", 0))
    return 0


def _build_statement_pdf(lottery_type: str, lottery_time: str, lottery_date: str, tickets: List[dict], agency_name: str) -> bytes:
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=12 * mm, rightMargin=12 * mm, topMargin=14 * mm, bottomMargin=14 * mm)
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("t", parent=styles["Title"], textColor=colors.HexColor("#0a1a0f"), fontSize=18, spaceAfter=4)
    sub_style = ParagraphStyle("s", parent=styles["Normal"], textColor=colors.HexColor("#555"), fontSize=10, spaceAfter=6)
    story: List[Any] = []
    story.append(Paragraph(agency_name.upper(), title_style))
    story.append(Paragraph(f"Draw Statement · {lottery_type.upper()} · {lottery_time} · {lottery_date}", sub_style))
    story.append(Paragraph(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}", sub_style))
    story.append(Spacer(1, 6))

    if not tickets:
        story.append(Paragraph("<i>No tickets for this draw.</i>", sub_style))
        doc.build(story)
        return buf.getvalue()

    # Group tickets by (digit_type, bundle_key)
    groups: dict = {}
    for t in tickets:
        key = (t.get("digit_type", "single"), t.get("bundle", "base"))
        groups.setdefault(key, []).append(t)

    # Emit one table per digit_type, iterating all bundles for that digit_type in canonical order
    digit_types_in_draw = sorted({dt for dt, _ in groups.keys()})
    lot_lc = lottery_type.lower()
    lot_cap = lottery_type.capitalize()
    lot_label = f"{lot_cap} Lot"

    header_style = [
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0a1a0f")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#ffd700")),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("ALIGN", (5, 0), (5, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#c33"))
    ]

    grand_total_qty = 0
    grand_total_amt = 0.0

    for dt in digit_types_in_draw:
        bundles = PRICING.get(dt, {}).get("bundles", {})
        # Skip bundles the whole draw can't use (e.g. b33/b65 for dear)
        allowed_bundles = [(bk, bv) for bk, bv in bundles.items() if not (bv.get("kerala_only") and lot_lc != "kerala")]

        digit_word = DIGIT_WORD.get(dt, dt)
        for bundle_key, bundle_val in allowed_bundles:
            prize = _bundle_prize(dt, bundle_key)
            cat_label = f"{lot_lc}{digit_word}lot {prize}"
            group_total_label = f"{lot_cap}{prize} Lot Total"
            rows = groups.get((dt, bundle_key), [])
            data = [["Time", "Lottery", "Category", "Number", "=", "Qty"]]
            for t in rows:
                for n in t.get("numbers", []):
                    data.append([
                        lottery_time,
                        lot_label,
                        cat_label,
                        str(n.get("digits", "")),
                        "=",
                        str(t.get("quantity", 1)),
                    ])
            # Group total row = sum of qty * number-count
            grp_qty = sum(int(t.get("quantity", 1)) * len(t.get("numbers", [])) for t in rows)
            grp_amt = sum(float(t.get("amount", 0) or 0) for t in rows)
            grand_total_qty += grp_qty
            grand_total_amt += grp_amt
            data.append(["", "", group_total_label, "", "", str(grp_qty)])
            tbl = Table(data, colWidths=[22 * mm, 22 * mm, 60 * mm, 50 * mm, 8 * mm, 24 * mm])
            style_cmds = list(header_style)
            n_body = len(rows)  # not exactly rows; actual body rows below
            # Actual body rows count in data: len(data)-2 (header + total)
            body_start = 1
            body_end = len(data) - 2  # last data row is total
            if body_end >= body_start:
                style_cmds.append(("ROWBACKGROUNDS", (0, body_start), (-1, body_end), [colors.white, colors.HexColor("#fff8e1")]))
            # Style total row (last row): green pill look
            total_row = len(data) - 1
            style_cmds += [
                ("BACKGROUND", (0, total_row), (-1, total_row), colors.HexColor("#e8f5e9")),
                ("FONTNAME", (0, total_row), (-1, total_row), "Helvetica-Bold"),
                ("TEXTCOLOR", (2, total_row), (2, total_row), colors.HexColor("#0a1a0f")),
                ("TEXTCOLOR", (5, total_row), (5, total_row), colors.HexColor("#0a1a0f")),
                ("ALIGN", (2, total_row), (2, total_row), "RIGHT"),
            ]
            tbl.setStyle(TableStyle(style_cmds))
            story.append(tbl)
            story.append(Spacer(1, 4))

    story.append(Spacer(1, 6))
    story.append(Paragraph(f"<b>Grand Total Tickets:</b> {grand_total_qty} &nbsp;&nbsp; <b>Grand Total Amount:</b> Rs {grand_total_amt:.2f}", styles["Normal"]))
    story.append(Spacer(1, 4))
    story.append(Paragraph("<i>Bookings for this draw are now closed after statement generation.</i>", sub_style))
    doc.build(story)
    return buf.getvalue()


@api.post("/admin/statements/generate")
async def generate_statement(req: StatementReq, _: dict = Depends(admin_user)):
    if req.lottery_type not in LOTTERY_CONFIG:
        raise HTTPException(400, "Invalid lottery")
    existing = await db.draw_statements.find_one({
        "lottery_type": req.lottery_type, "lottery_time": req.lottery_time, "lottery_date": req.lottery_date
    })
    if existing:
        raise HTTPException(400, "Statement already generated for this draw")
    tickets = await db.tickets.find({
        "lottery_type": req.lottery_type,
        "lottery_time": req.lottery_time,
        "lottery_date": req.lottery_date,
    }, {"_id": 0}).to_list(10000)
    for t in tickets:
        u = await db.profiles.find_one({"id": t["user_id"]}, {"_id": 0, "username": 1, "mobile": 1})
        t["user"] = u
    total = round(sum(float(t["amount"]) for t in tickets), 2)
    setting = await db.admin_settings.find_one({"key": "agency_name"}, {"_id": 0})
    agency = (setting or {}).get("value") or "silver agency"
    doc = {
        "id": make_id(),
        "lottery_type": req.lottery_type,
        "lottery_time": req.lottery_time,
        "lottery_date": req.lottery_date,
        "total_amount": total,
        "tickets_count": len(tickets),
        "created_at": now_iso(),
    }
    await db.draw_statements.insert_one(doc)
    doc.pop("_id", None)
    return doc


@api.get("/admin/statements/download")
async def download_statement(lottery_type: str, lottery_time: str, lottery_date: str, auth: str = Query(None), authorization: str = Header(None)):
    token = None
    if authorization and authorization.startswith("Bearer "):
        token = authorization.split(" ", 1)[1]
    elif auth:
        token = auth
    if not token:
        raise HTTPException(401, "No token")
    try:
        payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except Exception:
        raise HTTPException(401, "Invalid token")
    u = await db.profiles.find_one({"id": payload.get("sub")}, {"_id": 0})
    if not u or not u.get("is_admin"):
        raise HTTPException(403, "Admin only")
    tickets = await db.tickets.find({
        "lottery_type": lottery_type, "lottery_time": lottery_time, "lottery_date": lottery_date,
    }, {"_id": 0}).to_list(10000)
    for t in tickets:
        uu = await db.profiles.find_one({"id": t["user_id"]}, {"_id": 0, "username": 1, "mobile": 1})
        t["user"] = uu
    setting = await db.admin_settings.find_one({"key": "agency_name"}, {"_id": 0})
    agency = (setting or {}).get("value") or "silver agency"
    pdf = _build_statement_pdf(lottery_type, lottery_time, lottery_date, tickets, agency)
    fname = f"statement-{lottery_type}-{lottery_time.replace(':', '').replace(' ', '')}-{lottery_date}.pdf"
    return StreamingResponse(io.BytesIO(pdf), media_type="application/pdf",
                             headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@app.on_event("startup")
async def startup():
    init_storage()
    await db.profiles.create_index("mobile", unique=True)
    await db.profiles.create_index("referral_code", unique=True)
    await db.draw_statements.create_index(
        [("lottery_type", 1), ("lottery_time", 1), ("lottery_date", 1)], unique=True
    )
    logger.info("Startup complete")


app.include_router(api)
app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("shutdown")
async def shutdown():
    client.close()
