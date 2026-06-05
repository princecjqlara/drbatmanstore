import asyncio
import json
import logging
import os
import random
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from hashlib import sha1
from html import escape
from pathlib import Path
from threading import RLock
from typing import Any

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from storage import configured_store_db_path, load_store_state, save_store_state, supabase_configured


logger = logging.getLogger(__name__)

ADD_NAME, ADD_DESCRIPTION, ADD_PRICE, ADD_STOCK, ADD_WARRANTY, ADD_SUBSCRIPTION, ADD_UPSELLS = range(7)
FOLLOWUP_ORDER, FOLLOWUP_MESSAGE = range(7, 9)

CUSTOMER_LANGUAGES = {
    "en": "English",
    "tl": "Tagalog",
    "es": "Spanish",
    "id": "Indonesian",
    "ms": "Malay",
    "vi": "Vietnamese",
    "th": "Thai",
    "pt": "Portuguese",
    "fr": "French",
    "de": "German",
    "ja": "Japanese",
    "ko": "Korean",
    "zh": "Chinese",
}

BOT_TEXT = {
    "status_updated": {
        "en": "Order {order_id} update: {status}.\n\nThanks for sticking with the process. Open My Orders if you want to check details or send a follow-up to admin.",
        "tl": "Na-update ang order {order_id}: {status}.\n\nSalamat sa pagsunod sa process. Buksan ang My Orders kung gusto mong tingnan ang details o mag-follow up sa admin.",
        "es": "Actualizacion del pedido {order_id}: {status}.\n\nGracias por seguir el proceso. Abre My Orders si quieres revisar detalles o enviar un seguimiento al admin.",
    }
}

ORDER_STATUSES = {
    "pending_payment": "Pending payment",
    "proof_uploaded": "Proof uploaded",
    "paid": "Paid",
    "processing": "Processing",
    "completed": "Completed",
    "cancelled": "Cancelled",
}

MAIN_MENU = ReplyKeyboardMarkup(
    [["Store", "View Cart"], ["Orders & Follow Up"], ["Support & Community"]],
    resize_keyboard=True,
)
MENU_COMMANDS = {
    "store",
    "view cart",
    "orders & follow up",
    "support & community",
    "my orders",
    "follow up",
    "contact admin",
    "join community",
    "settings",
    "admin",
    "dashboard",
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def money(value: int) -> str:
    return f"${value:,.2f}"


def short_id() -> str:
    return uuid.uuid4().hex[:8].upper()


def parse_admin_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part:
            try:
                ids.add(int(part))
            except ValueError:
                logger.warning("Ignoring invalid ADMIN_IDS value: %s", part)
    return ids


def parse_payment_methods(raw: str) -> list[dict[str, str]]:
    methods = []
    for index, item in enumerate(raw.split(","), start=1):
        item = item.strip()
        if not item:
            continue
        label, _, instructions = item.partition(":")
        methods.append(
            {
                "id": f"pay{index}",
                "label": label.strip(),
                "instructions": instructions.strip() or "Contact admin for payment instructions.",
            }
        )
    return methods or [
        {
            "id": "manual",
            "label": "Manual Payment",
            "instructions": "Contact admin for payment instructions.",
        }
    ]


class StoreDB:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.lock = RLock()
        self.data = {
            "products": {},
            "carts": {},
            "orders": {},
            "users": {},
            "settings": {
                "payment_methods": [],
                "checkout_instructions": "",
                "admin_contact_url": "",
                "community_url": "",
                "wallet_addresses": [],
                "warranty_requires_vouch": True,
                "playful_mode": True,
                "meme_gif_urls": [],
                "language": "en",
                "abandoned_cart_enabled": False,
                "abandoned_cart_interval_minutes": 60,
                "abandoned_cart_max_followups": 2,
                "abandoned_cart_messages": [
                    "Your cart is still saved for you. If you want the smoothest checkout, open View Cart, review the recommended add-ons, then pick your payment method when you are ready.",
                    "Friendly last nudge: your cart is still waiting. Stock can move, so open View Cart soon if you want to keep these items and add any final upgrades before payment.",
                ],
            },
        }
        self.load()

    def load(self) -> None:
        with self.lock:
            remote_data = load_store_state(self.data, self.path)
            if remote_data is not None:
                loaded = remote_data
            elif not self.path.exists():
                self.save()
                return
            else:
                loaded = json.loads(self.path.read_text(encoding="utf-8-sig"))
            self.data.update(loaded)
            self.data.setdefault("users", {})
            self.data.setdefault("settings", {})
            self.data["settings"].setdefault("payment_methods", [])
            self.data["settings"].setdefault("checkout_instructions", "")
            self.data["settings"].setdefault("admin_contact_url", "")
            self.data["settings"].setdefault("community_url", "")
            self.data["settings"].setdefault("wallet_addresses", [])
            self.data["settings"].setdefault("warranty_requires_vouch", True)
            self.data["settings"].setdefault("playful_mode", True)
            self.data["settings"].setdefault("meme_gif_urls", [])
            self.data["settings"].setdefault("language", "en")
            self.data["settings"].setdefault("abandoned_cart_enabled", False)
            self.data["settings"].setdefault("abandoned_cart_interval_minutes", 60)
            self.data["settings"].setdefault("abandoned_cart_max_followups", 2)
            self.data["settings"].setdefault("abandoned_cart_messages", [
                "Your cart is still saved for you. If you want the smoothest checkout, open View Cart, review the recommended add-ons, then pick your payment method when you are ready.",
                "Friendly last nudge: your cart is still waiting. Stock can move, so open View Cart soon if you want to keep these items and add any final upgrades before payment.",
            ])
            for product in self.data.get("products", {}).values():
                product.setdefault("highlight_rank", 0)

    def save(self) -> None:
        with self.lock:
            if supabase_configured() and save_store_state(self.data):
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(prefix=self.path.name, suffix=".tmp", dir=self.path.parent or ".")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
                    json.dump(self.data, temp_file, indent=2)
                for attempt in range(5):
                    try:
                        os.replace(temp_name, self.path)
                        break
                    except PermissionError:
                        if attempt == 4:
                            raise
                        time.sleep(0.1)
            finally:
                if os.path.exists(temp_name):
                    os.unlink(temp_name)

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            self.load()
            return deepcopy(self.data)

    def add_product(self, product: dict[str, Any]) -> None:
        with self.lock:
            self.load()
            product.setdefault("highlight_rank", 0)
            self.data["products"][product["id"]] = product
            self.save()

    def upsert_user(self, user: Any) -> None:
        if not user:
            return
        with self.lock:
            self.load()
            existing = self.data.setdefault("users", {}).get(str(user.id), {})
            self.data.setdefault("users", {})[str(user.id)] = {
                "id": user.id,
                "username": user.username,
                "full_name": user.full_name,
                "auto_updates_enabled": existing.get("auto_updates_enabled", True),
                "language": existing.get("language", "en"),
                "vouches": existing.get("vouches", []),
                "messages": existing.get("messages", []),
                "cart_updated_at": existing.get("cart_updated_at", ""),
                "abandoned_followups_sent": existing.get("abandoned_followups_sent", 0),
                "abandoned_last_followup_at": existing.get("abandoned_last_followup_at", ""),
                "updated_at": now_iso(),
            }
            self.save()

    def user_settings(self, user_id: int) -> dict[str, Any]:
        with self.lock:
            self.load()
            user = self.data.setdefault("users", {}).setdefault(
                str(user_id),
                {
                    "id": user_id,
                    "auto_updates_enabled": True,
                    "language": "en",
                    "vouches": [],
                    "updated_at": now_iso(),
                },
            )
            user.setdefault("auto_updates_enabled", True)
            user.setdefault("language", "en")
            self.save()
            return deepcopy(user)

    def update_user_settings(self, user_id: int, updates: dict[str, Any]) -> dict[str, Any]:
        with self.lock:
            self.load()
            user = self.data.setdefault("users", {}).setdefault(str(user_id), {"id": user_id})
            user.setdefault("auto_updates_enabled", True)
            user.setdefault("language", "en")
            user.update(updates)
            user["updated_at"] = now_iso()
            self.save()
            return deepcopy(user)

    def update_product(self, product_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        with self.lock:
            self.load()
            product = self.data["products"].get(product_id)
            if not product:
                return None
            product.update(updates)
            product["updated_at"] = now_iso()
            self.save()
            return deepcopy(product)

    def get_product(self, product_id: str) -> dict[str, Any] | None:
        with self.lock:
            self.load()
            product = self.data["products"].get(product_id)
            return deepcopy(product) if product else None

    def active_products(self) -> list[dict[str, Any]]:
        with self.lock:
            self.load()
            return [
                deepcopy(product)
                for product in self.data["products"].values()
                if product.get("active", True) and int(product.get("stock", 0)) > 0
            ]

    def all_products(self) -> list[dict[str, Any]]:
        with self.lock:
            self.load()
            return [
                deepcopy(product)
                for product in self.data["products"].values()
            ]

    def payment_methods(self) -> list[dict[str, str]]:
        with self.lock:
            self.load()
            methods = [
                deepcopy(method)
                for method in self.data.get("settings", {}).get("payment_methods", [])
                if method.get("active", True)
            ]
            return methods or deepcopy(PAYMENT_METHODS)

    def checkout_instructions(self) -> str:
        with self.lock:
            self.load()
            return str(self.data.get("settings", {}).get("checkout_instructions", "")).strip()

    def playful_mode(self) -> bool:
        with self.lock:
            self.load()
            return bool(self.data.get("settings", {}).get("playful_mode", True))

    def meme_gif_urls(self) -> list[str]:
        with self.lock:
            self.load()
            return [
                str(url).strip()
                for url in self.data.get("settings", {}).get("meme_gif_urls", [])
                if str(url).strip()
            ]

    def cart(self, user_id: int) -> dict[str, int]:
        with self.lock:
            self.load()
            return {k: int(v) for k, v in self.data["carts"].get(str(user_id), {}).items()}

    def set_cart(self, user_id: int, cart: dict[str, int]) -> None:
        with self.lock:
            self.load()
            cleaned = {k: int(v) for k, v in cart.items() if int(v) > 0}
            if cleaned:
                self.data["carts"][str(user_id)] = cleaned
                user = self.data.setdefault("users", {}).setdefault(str(user_id), {"id": user_id})
                user["cart_updated_at"] = now_iso()
                user["abandoned_followups_sent"] = 0
                user["abandoned_last_followup_at"] = ""
            else:
                self.data["carts"].pop(str(user_id), None)
                user = self.data.setdefault("users", {}).setdefault(str(user_id), {"id": user_id})
                user["abandoned_followups_sent"] = 0
                user["abandoned_last_followup_at"] = ""
            self.save()

    def add_to_cart(self, user_id: int, product_id: str, qty: int = 1) -> tuple[bool, str]:
        with self.lock:
            self.load()
            product = self.data["products"].get(product_id)
            if not product or not product.get("active", True):
                return False, "Product is not available."
            stock = int(product.get("stock", 0))
            cart = self.data["carts"].setdefault(str(user_id), {})
            current_qty = int(cart.get(product_id, 0))
            if current_qty + qty > stock:
                return False, f"Only {stock} stock left."
            cart[product_id] = current_qty + qty
            user = self.data.setdefault("users", {}).setdefault(str(user_id), {"id": user_id})
            user["cart_updated_at"] = now_iso()
            user["abandoned_followups_sent"] = 0
            user["abandoned_last_followup_at"] = ""
            self.save()
            return True, "Added to cart."

    def remove_from_cart(self, user_id: int, product_id: str) -> None:
        with self.lock:
            self.load()
            cart = self.data["carts"].get(str(user_id), {})
            cart.pop(product_id, None)
            self.set_cart(user_id, cart)

    def create_order(self, user_id: int, username: str, payment_method: dict[str, str]) -> tuple[dict[str, Any] | None, str]:
        with self.lock:
            self.load()
            cart = self.data["carts"].get(str(user_id), {})
            if not cart:
                return None, "Cart is empty."

            items = []
            total = 0
            for product_id, qty in cart.items():
                product = self.data["products"].get(product_id)
                if not product or not product.get("active", True):
                    return None, f"{product_id} is no longer available."
                stock = int(product.get("stock", 0))
                qty = int(qty)
                if qty > stock:
                    return None, f"{product['name']} only has {stock} stock left."
                line_total = int(product["price_credits"]) * qty
                total += line_total
                items.append(
                    {
                        "product_id": product_id,
                        "name": product["name"],
                        "qty": qty,
                        "price_credits": int(product["price_credits"]),
                        "line_total": line_total,
                        "warranty_days": int(product.get("warranty_days", 0)),
                        "subscription_days": int(product.get("subscription_days", 0)),
                    }
                )

            for item in items:
                self.data["products"][item["product_id"]]["stock"] = (
                    int(self.data["products"][item["product_id"]]["stock"]) - item["qty"]
                )

            order = {
                "id": short_id(),
                "user_id": user_id,
                "username": username,
                "items": items,
                "total_credits": total,
                "payment_method": payment_method,
                "status": "pending_payment",
                "proof": None,
                "created_at": now_iso(),
                "updated_at": now_iso(),
                "followups": [],
                "vouches": [],
                "delivery_message": "",
                "delivered_at": "",
            }
            self.data["orders"][order["id"]] = order
            self.data["carts"].pop(str(user_id), None)
            user = self.data.setdefault("users", {}).setdefault(str(user_id), {"id": user_id})
            user["abandoned_followups_sent"] = 0
            user["abandoned_last_followup_at"] = ""
            self.save()
            return deepcopy(order), "Order created."

    def abandoned_cart_candidates(self) -> list[dict[str, Any]]:
        with self.lock:
            self.load()
            settings = self.data.get("settings", {})
            if not settings.get("abandoned_cart_enabled", False):
                return []
            carts = self.data.get("carts", {})
            users = self.data.setdefault("users", {})
            candidates = []
            for user_id, cart in carts.items():
                if not cart:
                    continue
                user = users.setdefault(str(user_id), {"id": int(user_id)})
                if not user.get("auto_updates_enabled", True):
                    continue
                candidates.append({"user_id": int(user_id), "cart": deepcopy(cart), "user": deepcopy(user)})
            return candidates

    def abandoned_cart_settings(self) -> dict[str, Any]:
        with self.lock:
            self.load()
            return deepcopy(self.data.get("settings", {}))

    def mark_abandoned_followup_sent(self, user_id: int) -> None:
        with self.lock:
            self.load()
            user = self.data.setdefault("users", {}).setdefault(str(user_id), {"id": user_id})
            user["abandoned_followups_sent"] = int(user.get("abandoned_followups_sent", 0)) + 1
            user["abandoned_last_followup_at"] = now_iso()
            self.save()

    def user_orders(self, user_id: int) -> list[dict[str, Any]]:
        with self.lock:
            self.load()
            orders = [deepcopy(order) for order in self.data["orders"].values() if int(order["user_id"]) == user_id]
            return sorted(orders, key=lambda order: order["created_at"], reverse=True)

    def recent_orders(self, limit: int = 20) -> list[dict[str, Any]]:
        with self.lock:
            self.load()
            orders = [deepcopy(order) for order in self.data["orders"].values()]
            return sorted(orders, key=lambda order: order["created_at"], reverse=True)[:limit]

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        with self.lock:
            self.load()
            order = self.data["orders"].get(order_id)
            return deepcopy(order) if order else None

    def update_order(self, order_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        with self.lock:
            self.load()
            order = self.data["orders"].get(order_id)
            if not order:
                return None
            previous_status = order.get("status")
            order.update(updates)
            order["updated_at"] = now_iso()
            if updates.get("status") == "cancelled" and previous_status != "cancelled":
                for item in order["items"]:
                    product = self.data["products"].get(item["product_id"])
                    if product:
                        product["stock"] = int(product.get("stock", 0)) + int(item["qty"])
            self.save()
            return deepcopy(order)

    def add_followup(self, order_id: str, user_id: int, message: str) -> dict[str, Any] | None:
        with self.lock:
            self.load()
            order = self.data["orders"].get(order_id)
            if not order or int(order["user_id"]) != user_id:
                return None
            order["followups"].append({"from": "customer", "message": message, "created_at": now_iso()})
            order["updated_at"] = now_iso()
            self.save()
            return deepcopy(order)

    def wallet_instructions(self) -> str:
        with self.lock:
            self.load()
            wallets = self.data.get("settings", {}).get("wallet_addresses", [])
            lines = []
            for wallet in wallets:
                label = str(wallet.get("label", "Wallet")).strip() or "Wallet"
                network = str(wallet.get("network", "")).strip()
                address = str(wallet.get("address", "")).strip()
                note = str(wallet.get("note", "")).strip()
                if not address:
                    continue
                title = f"{label} ({network})" if network else label
                lines.append(f"- {title}: {address}" + (f"\n  {note}" if note else ""))
            return "\n".join(lines)

    def warranty_requires_vouch(self) -> bool:
        with self.lock:
            self.load()
            return bool(self.data.get("settings", {}).get("warranty_requires_vouch", True))

    def add_vouch(self, order_id: str, user_id: int, message: str, proof: dict[str, str] | None = None) -> dict[str, Any] | None:
        with self.lock:
            self.load()
            order = self.data["orders"].get(order_id)
            if not order or int(order.get("user_id", 0)) != user_id:
                return None
            vouch = {
                "from": "customer",
                "message": message,
                "proof": proof or None,
                "created_at": now_iso(),
                "warranty_activated": True,
            }
            order.setdefault("vouches", []).append(vouch)
            order["updated_at"] = now_iso()
            user = self.data.setdefault("users", {}).setdefault(str(user_id), {"id": user_id})
            user.setdefault("vouches", []).append({"order_id": order_id, **vouch})
            user["updated_at"] = now_iso()
            self.save()
            return deepcopy(order)

    def add_user_message(
        self,
        user: Any,
        message: str,
        sender: str = "customer",
        context: str = "Contact admin",
        media_url: str = "",
        media_type: str = "",
    ) -> None:
        if not user:
            return
        with self.lock:
            self.load()
            existing = self.data.setdefault("users", {}).setdefault(
                str(user.id),
                {
                    "id": user.id,
                    "username": user.username,
                    "full_name": user.full_name,
                    "auto_updates_enabled": True,
                    "language": "en",
                    "updated_at": now_iso(),
                },
            )
            existing.setdefault("messages", []).append(
                {
                    "from": sender,
                    "message": message,
                    "media_url": media_url,
                    "media_type": media_type,
                    "created_at": now_iso(),
                    "context": context,
                    "read": sender != "customer",
                }
            )
            existing["updated_at"] = now_iso()
            self.save()


load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
ADMIN_IDS = parse_admin_ids(os.getenv("ADMIN_IDS", ""))
PAYMENT_METHODS = parse_payment_methods(os.getenv("PAYMENT_METHODS", ""))
DB = StoreDB(str(configured_store_db_path()))
ADMIN_CONTACT_URL = os.getenv("ADMIN_CONTACT_URL", "").strip()
COMMUNITY_URL = os.getenv("COMMUNITY_URL", "").strip()
CLOUDINARY_URL = os.getenv("CLOUDINARY_URL", "").strip()
CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME", "").strip()
CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY", "").strip()
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET", "").strip()


def cloudinary_config() -> dict[str, str] | None:
    if CLOUDINARY_CLOUD_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET:
        return {
            "cloud_name": CLOUDINARY_CLOUD_NAME,
            "api_key": CLOUDINARY_API_KEY,
            "api_secret": CLOUDINARY_API_SECRET,
        }
    if not CLOUDINARY_URL:
        return None
    parsed = urllib.parse.urlparse(CLOUDINARY_URL)
    if parsed.scheme != "cloudinary" or not parsed.hostname or not parsed.username or not parsed.password:
        return None
    return {
        "cloud_name": parsed.hostname,
        "api_key": urllib.parse.unquote(parsed.username),
        "api_secret": urllib.parse.unquote(parsed.password),
    }


def telegram_file_url(file_id: str | None) -> str:
    if not BOT_TOKEN or not file_id:
        return ""
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/getFile?file_id={urllib.parse.quote(file_id)}"
        with urllib.request.urlopen(url, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
        file_path = payload.get("result", {}).get("file_path")
        if not file_path:
            return ""
        return f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    except Exception:
        logger.exception("Failed to resolve Telegram file URL.")
        return ""


def cloudinary_upload_url(source_url: str, folder: str = "telegram_bot") -> dict[str, str]:
    config = cloudinary_config()
    if not config or not source_url:
        return {}
    timestamp = str(int(time.time()))
    params = {"folder": folder, "timestamp": timestamp}
    signature_base = "&".join(f"{key}={params[key]}" for key in sorted(params)) + config["api_secret"]
    signature = sha1(signature_base.encode("utf-8")).hexdigest()
    payload = urllib.parse.urlencode(
        {
            "file": source_url,
            "api_key": config["api_key"],
            "timestamp": timestamp,
            "folder": folder,
            "signature": signature,
        }
    ).encode("utf-8")
    url = f"https://api.cloudinary.com/v1_1/{config['cloud_name']}/auto/upload"
    try:
        request_obj = urllib.request.Request(url, data=payload, method="POST")
        with urllib.request.urlopen(request_obj, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
        return {
            "media_url": result.get("secure_url", ""),
            "public_id": result.get("public_id", ""),
            "resource_type": result.get("resource_type", ""),
        }
    except Exception:
        logger.exception("Failed to upload media to Cloudinary.")
        return {}


def cloudinary_upload_telegram_file(file_id: str | None, folder: str = "telegram_bot") -> dict[str, str]:
    return cloudinary_upload_url(telegram_file_url(file_id), folder=folder)


def is_admin(user_id: int | None) -> bool:
    return bool(user_id and user_id in ADMIN_IDS)


def track_user(update: Update) -> None:
    DB.upsert_user(update.effective_user)


def user_identity(user: Any) -> str:
    username = f"@{user.username}" if getattr(user, "username", None) else "@no_username"
    return f"{user.full_name} ({user.id}, {username})"


def admin_delivery_failure_text() -> str:
    if not ADMIN_IDS:
        return (
            "I saved your message in the dashboard inbox, but no Telegram admin ID is configured yet. "
            "An admin can still read it in the dashboard once ADMIN_IDS is set."
        )
    return (
        "I saved your message in the dashboard inbox, but Telegram could not deliver the alert to admin. "
        "Please ask admin to confirm ADMIN_IDS is the numeric Telegram ID and that they have started this bot."
    )


def customer_message_sent_text(kind: str = "message") -> str:
    return (
        f"Got it - your {kind} is safely in the admin inbox and I also pinged admin on Telegram.\n\n"
        "While you wait, you can keep browsing, check your cart, or open My Orders if this is about an existing order."
    )


def configured_url(setting_key: str, env_fallback: str) -> str:
    return str(DB.snapshot().get("settings", {}).get(setting_key) or env_fallback).strip()


def configured_admin_contact_url() -> str:
    return configured_url("admin_contact_url", ADMIN_CONTACT_URL)


def configured_community_url() -> str:
    return configured_url("community_url", COMMUNITY_URL)


def customer_text(user_id: int, key: str, **kwargs: Any) -> str:
    settings = DB.user_settings(user_id)
    language = settings.get("language", "en")
    value = BOT_TEXT.get(key, {}).get(language) or BOT_TEXT.get(key, {}).get("en") or key
    return value.format(**kwargs)


def product_text(product: dict[str, Any]) -> str:
    status = "Active" if product.get("active", True) else "Inactive"
    if int(product.get("stock", 0)) <= 0:
        status = "Out of stock"
    tip = "✨ Tip: add this to your cart now to hold your spot, then check the recommended add-ons before checkout. I will keep the steps organized so payment, receipt upload, and delivery stay easy."
    if int(product.get("stock", 0)) <= 0:
        tip = "🔴 This item is out of stock right now, so checkout is paused for it. You can still review every detail here and message admin from Support & Community if you want restock timing or a close alternative."
    elif not product.get("active", True):
        tip = "⏸ This item is paused for checkout right now. You can still review the details or ask admin for a recommendation that fits the same goal."
    rank = product_highlight_rank(product)
    featured_line = f"⭐ <b>Featured pick #{rank}</b> - this is one of the two products admin is highlighting right now.\n" if rank else ""
    return (
        f"<b>{product['name']}</b>\n"
        f"{featured_line}"
        f"{product['description']}\n\n"
        f"Price: <b>{money(int(product['price_credits']))}</b>\n"
        f"Stock left: <b>{int(product.get('stock', 0))}</b>\n"
        f"Warranty: <b>{int(product.get('warranty_days', 0))} days</b>\n"
        f"Subscription: <b>{int(product.get('subscription_days', 0))} days</b>\n"
        f"Status: <b>{status}</b>\n"
        f"ID: <code>{product['id']}</code>\n\n"
        f"{tip}"
    )


def strikethrough(text: str) -> str:
    return "".join(f"{char}\u0336" for char in text)


def product_is_available(product: dict[str, Any]) -> bool:
    return bool(product.get("active", True) and int(product.get("stock", 0)) > 0)


def product_highlight_rank(product: dict[str, Any]) -> int:
    try:
        rank = int(product.get("highlight_rank", 0))
    except (TypeError, ValueError):
        return 0
    return rank if rank in (1, 2) else 0


def product_sort_key(product: dict[str, Any]) -> tuple[int, int, str, str]:
    rank = product_highlight_rank(product)
    featured_bucket = rank if rank else 99
    available_bucket = 0 if product_is_available(product) else 1
    return (featured_bucket, available_bucket, product.get("name", "").lower(), product.get("id", ""))


def product_button_label(product: dict[str, Any]) -> str:
    name = product["name"]
    rank = product_highlight_rank(product)
    prefix = f"⭐ Featured #{rank} | " if rank else ""
    if int(product.get("stock", 0)) <= 0:
        return f"🔴 {prefix}{name} | OUT OF STOCK"
    if not product.get("active", True):
        return f"⏸ {prefix}{name} | Paused"
    return f"{prefix}{name} | {money(int(product['price_credits']))}"
    if int(product.get("stock", 0)) <= 0:
        return f"🔴 {name} - OUT OF STOCK"
    if not product.get("active", True):
        return f"{strikethrough(name)} - INACTIVE"
    return f"{name} - {money(int(product['price_credits']))}"


def cart_summary(user_id: int) -> tuple[str, list[dict[str, Any]], int]:
    cart = DB.cart(user_id)
    if not cart:
        return (
            "🛒 Your cart is empty right now.\n\n"
            "Tap Store to pick your first item. I will show you the useful add-ons along the way so checkout feels easy."
        ), [], 0

    lines = ["<b>🛒 Your Cart</b>", "Review your items below, adjust the quantity if needed, then checkout when it looks perfect.", ""]
    items = []
    total = 0
    for product_id, qty in cart.items():
        product = DB.get_product(product_id)
        if not product:
            continue
        line_total = int(product["price_credits"]) * qty
        total += line_total
        items.append({"product": product, "qty": qty, "line_total": line_total})
        lines.append(f"- {product['name']} x {qty}: {money(line_total)}")
    lines.append("")
    lines.append(f"Total: <b>{money(total)}</b>")
    lines.append("")
    lines.append("Quick tip: before paying, look at the recommended add-ons so you do not miss a useful upgrade.")
    return "\n".join(lines), items, total


def order_text(order: dict[str, Any]) -> str:
    lines = [
        f"<b>Order {order['id']}</b>",
        f"Status: <b>{ORDER_STATUSES.get(order['status'], order['status'])}</b>",
        f"Payment: <b>{order['payment_method']['label']}</b>",
        "",
        "<b>Items</b>",
    ]
    for item in order["items"]:
        lines.append(
            f"- {item['name']} x {item['qty']}: {money(item['line_total'])} "
            f"(warranty {item['warranty_days']}d, sub {item['subscription_days']}d)"
        )
    lines.extend(["", f"Total: <b>{money(int(order['total_credits']))}</b>", f"Created: {order['created_at']}"])
    proof = order.get("proof")
    if proof:
        lines.append(f"Receipt: <b>Uploaded ({proof.get('type', 'file')})</b>")
    if order_has_warranty(order) and DB.warranty_requires_vouch():
        warranty_state = "Active after vouch" if order_has_vouch(order) else "Vouch recommended to activate"
        lines.append(f"Warranty status: <b>{warranty_state}</b>")
    followups = order.get("followups", [])
    if followups:
        lines.extend(["", "<b>Follow Ups</b>"])
        for item in followups[-5:]:
            sender = item.get("from", "customer").title()
            lines.append(f"- {sender}: {item.get('message', '')}")
    return "\n".join(lines)


def admin_dashboard_text() -> str:
    snapshot = DB.snapshot()
    products = list(snapshot["products"].values())
    orders = list(snapshot["orders"].values())

    active_products = [product for product in products if product.get("active", True)]
    low_stock = [
        product
        for product in active_products
        if 0 < int(product.get("stock", 0)) <= 5
    ]
    out_of_stock = [
        product
        for product in active_products
        if int(product.get("stock", 0)) <= 0
    ]
    total_stock = sum(int(product.get("stock", 0)) for product in active_products)

    status_counts = {status: 0 for status in ORDER_STATUSES}
    total_order_value = 0
    completed_value = 0
    for order in orders:
        status = order.get("status", "")
        status_counts[status] = status_counts.get(status, 0) + 1
        if status != "cancelled":
            total_order_value += int(order.get("total_credits", 0))
        if status == "completed":
            completed_value += int(order.get("total_credits", 0))

    lines = [
        "<b>Admin Dashboard</b>",
        "",
        f"Products: <b>{len(products)}</b> total, <b>{len(active_products)}</b> active",
        f"Stock available: <b>{total_stock}</b>",
        f"Low stock: <b>{len(low_stock)}</b>",
        f"Out of stock: <b>{len(out_of_stock)}</b>",
        "",
        f"Orders: <b>{len(orders)}</b>",
        f"Pending payment: <b>{status_counts.get('pending_payment', 0)}</b>",
        f"Proof uploaded: <b>{status_counts.get('proof_uploaded', 0)}</b>",
        f"Processing: <b>{status_counts.get('processing', 0)}</b>",
        f"Completed: <b>{status_counts.get('completed', 0)}</b>",
        "",
        f"Open order value: <b>{money(total_order_value)}</b>",
        f"Completed value: <b>{money(completed_value)}</b>",
    ]
    return "\n".join(lines)


def admin_dashboard_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Add Product", callback_data="admin:add_product")],
            [
                InlineKeyboardButton("Manage Products", callback_data="admin:products"),
                InlineKeyboardButton("Process Orders", callback_data="admin:orders"),
            ],
            [InlineKeyboardButton("Refresh", callback_data="admin:dashboard")],
        ]
    )


def product_keyboard(product: dict[str, Any], include_back: bool = True) -> InlineKeyboardMarkup:
    product_id = product["id"]
    rows = []
    if product_is_available(product):
        rows.append([InlineKeyboardButton("🛒 Add to Cart", callback_data=f"cart:add:{product_id}")])
    elif int(product.get("stock", 0)) <= 0:
        rows.append([InlineKeyboardButton("🔴 Out of Stock - Details Only", callback_data="admin:noop")])
    else:
        rows.append([InlineKeyboardButton("⏸ Paused - Ask Admin", callback_data="admin:noop")])
    rows.append([InlineKeyboardButton("🧺 View Cart", callback_data="cart:view")])
    if include_back:
        rows.append([InlineKeyboardButton("⬅ Back to Store", callback_data="store:list")])
    return InlineKeyboardMarkup(rows)


def upsell_suggestions(user_id: int, source_product_id: str | None = None, limit: int = 3) -> list[dict[str, Any]]:
    cart_ids = set(DB.cart(user_id))
    suggestions: list[dict[str, Any]] = []
    if source_product_id:
        source = DB.get_product(source_product_id)
        if source:
            for product_id in source.get("upsell_ids", []):
                product = DB.get_product(product_id)
                if product and product.get("active", True) and int(product.get("stock", 0)) > 0 and product_id not in cart_ids:
                    suggestions.append(product)

    if not suggestions:
        for product in DB.active_products():
            if product["id"] != source_product_id and product["id"] not in cart_ids:
                suggestions.append(product)
            if len(suggestions) >= limit:
                break

    return suggestions[:limit]


def upsell_keyboard(user_id: int, source_product_id: str | None = None) -> InlineKeyboardMarkup | None:
    suggestions = upsell_suggestions(user_id, source_product_id)
    if not suggestions:
        return None

    rows = [[InlineKeyboardButton(f"➕ Add Upgrade: {product['name']}", callback_data=f"cart:add:{product['id']}")] for product in suggestions]
    rows.append([InlineKeyboardButton("🧺 Review Cart", callback_data="cart:view")])
    return InlineKeyboardMarkup(rows)


def upsell_teaser(user_id: int, source_product_id: str | None = None) -> str:
    suggestions = upsell_suggestions(user_id, source_product_id)
    if not suggestions:
        return ""
    names = ", ".join(escape(product["name"]) for product in suggestions)
    return (
        f"\n\n<b>Recommended add-ons:</b> {names}\n"
        "These are optional, but they are the easiest way to complete your setup before you pay."
    )


def order_has_warranty(order: dict[str, Any]) -> bool:
    return any(int(item.get("warranty_days", 0)) > 0 for item in order.get("items", []))


def order_has_vouch(order: dict[str, Any]) -> bool:
    return bool(order.get("vouches"))


def known_user_ids() -> list[int]:
    snapshot = DB.snapshot()
    ids = {int(user_id) for user_id in snapshot.get("users", {}) if str(user_id).isdigit()}
    for order in snapshot.get("orders", {}).values():
        if str(order.get("user_id", "")).isdigit():
            ids.add(int(order["user_id"]))
    for user_id in snapshot.get("carts", {}):
        if str(user_id).isdigit():
            ids.add(int(user_id))
    return sorted(ids)


async def broadcast_vouch(context: ContextTypes.DEFAULT_TYPE, from_user_id: int, order_id: str, message: str, proof: dict[str, str] | None) -> None:
    text = (
        "New customer vouch just came in.\n\n"
        f"Order: {order_id}\n"
        f"Vouch: {message}"
    )
    for user_id in known_user_ids():
        if user_id == from_user_id:
            continue
        try:
            if proof and proof.get("type") == "photo":
                await context.bot.send_photo(user_id, proof.get("media_url") or proof["value"], caption=text)
            elif proof and proof.get("type") == "document":
                await context.bot.send_document(user_id, proof.get("media_url") or proof["value"], caption=text)
            else:
                await context.bot.send_message(user_id, text)
        except Exception:
            logger.exception("Failed to broadcast vouch for order %s to %s", order_id, user_id)


def vouch_prompt_text(order_id: str) -> str:
    return (
        f"🎉 Your order {order_id} is completed and your product has been delivered.\n\n"
        "Warranty is optional, but strongly recommended. To activate warranty tracking, send one clear photo or screenshot showing the account/product is working, plus a short caption like what you bought and that it works for you.\n\n"
        "After you send it, admin keeps the vouch on your customer record and the proof can be shared with other customers as social proof."
    )


async def orders_support_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "📦 Orders & Follow Up\n\n"
        "This is your order desk. Open My Orders to check payment status, resend a receipt, view delivery details, or activate warranty with a vouch after delivery.\n\n"
        "Use Follow Up when you want to send admin one clear message about an existing order, such as a payment reference, delivery question, or account issue."
    )
    markup = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("My Orders", callback_data="orders:mine")],
            [InlineKeyboardButton("Follow Up", callback_data="followup:menu")],
        ]
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await update.message.reply_text(text, reply_markup=markup)


async def support_community_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "💬 Support & Community\n\n"
        "Need help choosing, paying, or checking an order? Contact Admin keeps the conversation private inside the dashboard.\n\n"
        "Want drops, restocks, vouches, and updates? Join Community opens the invite link admin saved. Settings lets you choose bot language and turn automatic updates on or off."
    )
    rows = [
        [InlineKeyboardButton("Contact Admin", callback_data="support:contact")],
        [InlineKeyboardButton("Join Community", callback_data="support:community")],
        [InlineKeyboardButton("Settings", callback_data="settings:menu")],
    ]
    markup = InlineKeyboardMarkup(rows)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await update.message.reply_text(text, reply_markup=markup)


async def customer_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    settings = DB.user_settings(update.effective_user.id)
    language = CUSTOMER_LANGUAGES.get(settings.get("language", "en"), "English")
    notifications = "On" if settings.get("auto_updates_enabled", True) else "Off"
    text = (
        "⚙️ Customer Settings\n\n"
        f"Language: {language}\n"
        f"Automatic updates: {notifications}\n\n"
        "Automatic updates control restock alerts, social proof, and abandoned-cart reminders. Admin can still send direct support replies and manual bulk messages when needed."
    )
    markup = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Change Language", callback_data="settings:language")],
            [InlineKeyboardButton(f"Turn {'Off' if settings.get('auto_updates_enabled', True) else 'On'} Automatic Updates", callback_data="settings:notifications:toggle")],
            [InlineKeyboardButton("Back", callback_data="support:menu")],
        ]
    )
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await update.message.reply_text(text, reply_markup=markup)


async def customer_language_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = [
        [InlineKeyboardButton(label, callback_data=f"settings:language:{code}")]
        for code, label in CUSTOMER_LANGUAGES.items()
    ]
    rows.append([InlineKeyboardButton("Back to Settings", callback_data="settings:menu")])
    await update.callback_query.edit_message_text(
        "Choose your preferred bot language.\n\nThis saves your preference for customer settings and supported automatic messages.",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    track_user(update)
    await update.message.reply_text(
        "👋 Welcome in! I will guide you step by step so ordering stays simple.\n\n"
        "1. Tap Store and choose what you want.\n"
        "2. Add it to your cart, then check the recommended add-ons I show you.\n"
        "3. Checkout, choose your payment method, and send your receipt here.\n\n"
        "Ready when you are - tap Store to start browsing.",
        reply_markup=MAIN_MENU,
    )


async def show_store(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    products = sorted(DB.all_products(), key=product_sort_key)
    if not products:
        text = (
            "🛒 The store is taking a tiny shelf break - no products are available right now.\n\n"
            "You can tap Contact Admin if you want a recommendation or want to ask when the next stock lands."
        )
        if update.callback_query:
            await update.callback_query.edit_message_text(text)
        else:
            await update.message.reply_text(text, reply_markup=MAIN_MENU)
        return

    rows = []
    for product in products:
        rows.append(
            [
                InlineKeyboardButton(
                    product_button_label(product),
                    callback_data=f"product:view:{product['id']}",
                )
            ]
        )
    rows.append([InlineKeyboardButton("View Cart", callback_data="cart:view")])
    text = (
        "<b>🛍️ Store</b>\n"
        "Welcome to the shelf. ⭐ Featured products appear first, out-of-stock items stay visible for details, and each product page tells you exactly what to do next.\n\n"
        "Pick an item to inspect it. If it is available, add it to cart and I will suggest useful add-ons before checkout so your order feels complete in one trip."
    )
    markup = InlineKeyboardMarkup(rows)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)


async def show_product(update: Update, context: ContextTypes.DEFAULT_TYPE, product_id: str) -> None:
    product = DB.get_product(product_id)
    if not product:
        await update.callback_query.answer("Product not found.", show_alert=True)
        return
    await update.callback_query.edit_message_text(
        product_text(product),
        reply_markup=product_keyboard(product),
        parse_mode=ParseMode.HTML,
    )


async def add_cart(update: Update, context: ContextTypes.DEFAULT_TYPE, product_id: str) -> None:
    query = update.callback_query
    user_id = query.from_user.id
    ok, message = DB.add_to_cart(user_id, product_id)
    await query.answer(message, show_alert=not ok)
    if not ok:
        return
    keyboard = upsell_keyboard(user_id, product_id)
    if keyboard:
        await query.edit_message_text(
            "✅ Nice pick - added to your cart.\n\n"
            "🔥 Before you checkout, here are a few add-ons that pair well with it. "
            "Tap one to add it instantly, or go straight to your cart if you are already set.",
            reply_markup=keyboard,
        )
    else:
        await query.edit_message_text(
            "✅ Added to your cart.\n\n"
            "No matching add-ons are available right now, so you can review your cart and checkout when ready.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("View Cart", callback_data="cart:view")]]),
        )
    await send_random_meme(context, user_id)


async def view_cart(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_id = update.effective_user.id
    text, items, _ = cart_summary(user_id)
    rows = []
    for item in items:
        product = item["product"]
        rows.append(
            [
                InlineKeyboardButton("-", callback_data=f"cart:dec:{product['id']}"),
                InlineKeyboardButton(f"{item['qty']} x {product['name']}", callback_data=f"product:view:{product['id']}"),
                InlineKeyboardButton("+", callback_data=f"cart:add:{product['id']}"),
            ]
        )
        rows.append([InlineKeyboardButton(f"Remove {product['name']}", callback_data=f"cart:remove:{product['id']}")])
    if items:
        suggestions = upsell_suggestions(user_id)
        if suggestions:
            text += upsell_teaser(user_id)
            for product in suggestions:
                rows.append([InlineKeyboardButton(f"✨ Add Recommended: {product['name']}", callback_data=f"cart:add:{product['id']}")])
        rows.append([InlineKeyboardButton("💳 Checkout - Send Receipt After Paying", callback_data="checkout:start")])
    rows.append([InlineKeyboardButton("🛍 Back to Store", callback_data="store:list")])
    markup = InlineKeyboardMarkup(rows)

    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)


async def dec_cart(update: Update, context: ContextTypes.DEFAULT_TYPE, product_id: str) -> None:
    user_id = update.effective_user.id
    cart = DB.cart(user_id)
    if product_id in cart:
        cart[product_id] -= 1
        DB.set_cart(user_id, cart)
    await view_cart(update, context)


async def remove_cart(update: Update, context: ContextTypes.DEFAULT_TYPE, product_id: str) -> None:
    DB.remove_from_cart(update.effective_user.id, product_id)
    await view_cart(update, context)


async def checkout_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text, items, _ = cart_summary(update.effective_user.id)
    if not items:
        await update.callback_query.edit_message_text(
            "🛒 Your cart is empty right now.\n\nTap Store to pick an item first, then I will help with add-ons and checkout.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Store", callback_data="store:list")]]),
        )
        return

    payment_methods = DB.payment_methods()
    suggestions = upsell_suggestions(update.effective_user.id)
    rows = []
    if suggestions:
        for product in suggestions:
            rows.append([InlineKeyboardButton(f"✨ Add Recommended: {product['name']}", callback_data=f"cart:add:{product['id']}")])

    rows.extend([[InlineKeyboardButton(f"💳 Pay with {method['label']}", callback_data=f"checkout:pay:{method['id']}")] for method in payment_methods])
    rows.append([InlineKeyboardButton("⬅ Back to Cart", callback_data="cart:view")])
    checkout_instructions = DB.checkout_instructions()
    wallet_instructions = DB.wallet_instructions()
    instruction_text = f"\n\n<b>Checkout Instructions</b>\n{escape(checkout_instructions)}" if checkout_instructions else ""
    wallet_text = f"\n\n<b>Wallets</b>\n{escape(wallet_instructions)}" if wallet_instructions else ""
    instruction_text = f"{instruction_text}{wallet_text}"
    extra = upsell_teaser(update.effective_user.id) if suggestions else "\n\nEverything looks ready. Choose a payment method below when you are happy with the cart."
    await update.callback_query.edit_message_text(
        f"{text}{instruction_text}{extra}\n\n💳 Choose your payment method below. After payment, I will ask for your receipt so admin can verify it quickly.",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.HTML,
    )


async def checkout_payment(update: Update, context: ContextTypes.DEFAULT_TYPE, method_id: str) -> None:
    method = next((item for item in DB.payment_methods() if item["id"] == method_id), None)
    if not method:
        await update.callback_query.answer("Payment method not found.", show_alert=True)
        return
    user = update.effective_user
    username = user.username or user.full_name or str(user.id)
    order, message = DB.create_order(user.id, username, method)
    if not order:
        await update.callback_query.answer(message, show_alert=True)
        await view_cart(update, context)
        return

    checkout_instructions = DB.checkout_instructions()
    wallet_instructions = DB.wallet_instructions()
    payment_note = f"\n\n<b>Payment Notes</b>\n{escape(checkout_instructions)}" if checkout_instructions else ""
    wallet_note = f"\n\n<b>Wallets</b>\n{escape(wallet_instructions)}" if wallet_instructions else ""
    payment_note = f"{payment_note}{wallet_note}"
    payment_note = (
        f"{payment_note}\n\n<b>What happens next</b>\n"
        "1. Send your receipt here.\n"
        "2. Admin verifies the receipt.\n"
        "3. Admin sends your product or credentials through this bot.\n"
        "4. The order is finished. Vouch is optional, but recommended if you want warranty coverage tracked."
    )
    await update.callback_query.edit_message_text(
        f"{order_text(order)}\n\n<b>Payment Instructions</b>\n{escape(method['instructions'])}\n\n"
        f"📸 After payment, tap Send Receipt and upload a screenshot, photo, document, or text reference in this chat."
        f"{payment_note}\n\n"
        "Tiny reminder: after this receipt is sent, you can still browse Store again if you want to add another item in a new order.",
        reply_markup=InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("Send Receipt", callback_data=f"receipt:start:{order['id']}")],
                [InlineKeyboardButton("My Orders", callback_data="orders:mine")],
                [InlineKeyboardButton("Browse More Deals", callback_data="store:list")],
            ]
        ),
        parse_mode=ParseMode.HTML,
    )
    context.user_data["awaiting_proof_order_id"] = order["id"]
    await send_random_meme(context, user.id, "Order created. Time to pay and send the receipt 💳")
    await notify_admins(
        context,
        f"New order {order['id']} from {username} ({user.id}). Total: {money(int(order['total_credits']))}",
        order_admin_keyboard(order["id"]),
        exclude_user_id=user.id,
    )


async def handle_payment_proof(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.user_data.get("awaiting_vouch_order_id"):
        await handle_vouch_message(update, context)
        return
    order_id = context.user_data.get("awaiting_proof_order_id")
    if context.user_data.get("awaiting_admin_message") and not order_id:
        await handle_admin_contact_media(update, context)
        return
    if not order_id:
        await update.message.reply_text(
            "📎 I received a file, but I do not know which order it belongs to yet.\n\n"
            "Open My Orders, choose the order, then tap Send Receipt before uploading. If this file is only for admin, tap Contact Admin first and send it again with a short caption.",
            reply_markup=MAIN_MENU,
        )
        return

    proof = {"type": "text", "value": update.message.text or ""}
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        upload = cloudinary_upload_telegram_file(file_id, folder="telegram_bot/receipts")
        proof = {"type": "photo", "value": file_id, **upload}
    elif update.message.document:
        file_id = update.message.document.file_id
        upload = cloudinary_upload_telegram_file(file_id, folder="telegram_bot/receipts")
        proof = {"type": "document", "value": file_id, **upload}

    order = DB.update_order(order_id, {"proof": proof, "status": "proof_uploaded"})
    if not order:
        await update.message.reply_text("Order not found.", reply_markup=MAIN_MENU)
        return

    context.user_data.pop("awaiting_proof_order_id", None)
    await update.message.reply_text(
        f"🧾 Receipt received for order {order_id}.\n\n"
        "Admin has been notified and will review it. You can check My Orders anytime for the status, or browse Store if you want to add anything else.",
        reply_markup=MAIN_MENU,
    )
    await notify_admins(
        context,
        f"🧾 Receipt uploaded for order {order_id}.",
        order_admin_keyboard(order_id),
        exclude_user_id=update.effective_user.id,
    )
    await forward_receipt_to_admins(context, update, order, exclude_user_id=update.effective_user.id)


async def start_vouch(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: str) -> None:
    order = DB.get_order(order_id)
    if not order or int(order["user_id"]) != update.effective_user.id:
        await update.callback_query.answer("Order not found.", show_alert=True)
        return
    if order.get("status") != "completed":
        await update.callback_query.answer("Vouch opens after the order is completed.", show_alert=True)
        return
    context.user_data["awaiting_vouch_order_id"] = order_id
    await update.callback_query.edit_message_text(
        vouch_prompt_text(order_id),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back to Order", callback_data=f"order:view:{order_id}")]]),
    )


async def handle_vouch_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    order_id = context.user_data.get("awaiting_vouch_order_id")
    if not order_id:
        return
    message = (update.message.caption or update.message.text or "").strip()
    proof = None
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
        upload = cloudinary_upload_telegram_file(file_id, folder="telegram_bot/vouches")
        proof = {"type": "photo", "value": file_id, **upload}
    elif update.message.document:
        file_id = update.message.document.file_id
        upload = cloudinary_upload_telegram_file(file_id, folder="telegram_bot/vouches")
        proof = {"type": "document", "value": file_id, **upload}
    if not proof:
        await update.message.reply_text(
            "Please send a photo or screenshot showing the account/product is working, with a short caption message. That proof is needed before warranty can be tracked.",
            reply_markup=MAIN_MENU,
        )
        return
    if not message:
        await update.message.reply_text(
            "Please add a short caption message with the photo, like 'received and working'. Send the photo again with that caption.",
            reply_markup=MAIN_MENU,
        )
        return
    order = DB.add_vouch(order_id, update.effective_user.id, message, proof)
    if not order:
        context.user_data.pop("awaiting_vouch_order_id", None)
        await update.message.reply_text("Order not found for vouch.", reply_markup=MAIN_MENU)
        return
    context.user_data.pop("awaiting_vouch_order_id", None)
    await update.message.reply_text(
        f"Thanks - your vouch for order {order_id} is saved. Warranty is now marked active in admin records.",
        reply_markup=MAIN_MENU,
    )
    await notify_admins(
        context,
        f"Customer vouch for order {order_id} from {user_identity(update.effective_user)}:\n\n{message}",
        order_admin_keyboard(order_id),
        exclude_user_id=update.effective_user.id,
    )
    await broadcast_vouch(context, update.effective_user.id, order_id, message, proof)


async def my_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    orders = DB.user_orders(update.effective_user.id)
    if not orders:
        text = (
            "📦 You do not have orders yet.\n\n"
            "Start in Store, add your favorite item, then I will show recommended add-ons before payment."
        )
        rows = [[InlineKeyboardButton("Store", callback_data="store:list")]]
    else:
        text = (
            "<b>📦 My Orders</b>\n"
            "Pick an order to see the latest status, send a receipt, or follow up with admin. You can also return to Store anytime for add-ons or a new order."
        )
        rows = [
            [InlineKeyboardButton(f"{order['id']} - {ORDER_STATUSES.get(order['status'], order['status'])}", callback_data=f"order:view:{order['id']}")]
            for order in orders[:10]
        ]
        rows.append([InlineKeyboardButton("Store", callback_data="store:list")])
    markup = InlineKeyboardMarkup(rows)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(text, reply_markup=markup, parse_mode=ParseMode.HTML)


async def show_order(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: str) -> None:
    order = DB.get_order(order_id)
    if not order or (not is_admin(update.effective_user.id) and int(order["user_id"]) != update.effective_user.id):
        await update.callback_query.answer("Order not found.", show_alert=True)
        return

    rows = []
    if int(order["user_id"]) == update.effective_user.id and order.get("status") not in {"completed", "cancelled"}:
        rows.append([InlineKeyboardButton("Send Receipt", callback_data=f"receipt:start:{order_id}")])
    if (
        int(order["user_id"]) == update.effective_user.id
        and order.get("status") == "completed"
        and order_has_warranty(order)
        and DB.warranty_requires_vouch()
    ):
        label = "Warranty Vouch Saved" if order_has_vouch(order) else "Send Vouch for Warranty"
        rows.append([InlineKeyboardButton(label, callback_data=f"vouch:start:{order_id}")])
    rows.append([InlineKeyboardButton("Follow Up", callback_data=f"followup:start:{order_id}")])
    if is_admin(update.effective_user.id):
        rows.extend(order_admin_rows(order_id))
    rows.append([InlineKeyboardButton("My Orders", callback_data="orders:mine")])
    await update.callback_query.edit_message_text(
        order_text(order),
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.HTML,
    )


def order_admin_rows(order_id: str) -> list[list[InlineKeyboardButton]]:
    return [
        [
            InlineKeyboardButton("Mark Paid", callback_data=f"admin:order:{order_id}:paid"),
            InlineKeyboardButton("Processing", callback_data=f"admin:order:{order_id}:processing"),
        ],
        [
            InlineKeyboardButton("Completed (after delivery)", callback_data=f"admin:order:{order_id}:completed"),
            InlineKeyboardButton("Cancel", callback_data=f"admin:order:{order_id}:cancelled"),
        ],
    ]


def order_admin_keyboard(order_id: str) -> InlineKeyboardMarkup:
    rows = order_admin_rows(order_id)
    rows.append([InlineKeyboardButton("View Order", callback_data=f"order:view:{order_id}")])
    return InlineKeyboardMarkup(rows)


async def send_random_meme(context: ContextTypes.DEFAULT_TYPE, chat_id: int, caption: str | None = None) -> None:
    if not DB.playful_mode():
        return
    urls = DB.meme_gif_urls()
    if not urls:
        return
    try:
        await context.bot.send_animation(chat_id, random.choice(urls), caption=caption)
    except Exception:
        return


async def notify_admins(
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    exclude_user_id: int | None = None,
) -> int:
    if not ADMIN_IDS:
        logger.warning("Admin notification skipped because ADMIN_IDS is empty.")
        return 0
    sent = 0
    for admin_id in ADMIN_IDS:
        if exclude_user_id and admin_id == exclude_user_id:
            continue
        try:
            await context.bot.send_message(admin_id, text, reply_markup=reply_markup)
            sent += 1
        except Exception:
            logger.exception("Failed to send Telegram admin notification to %s", admin_id)
            continue
    return sent


async def forward_receipt_to_admins(
    context: ContextTypes.DEFAULT_TYPE,
    update: Update,
    order: dict[str, Any],
    exclude_user_id: int | None = None,
) -> None:
    if not ADMIN_IDS:
        return

    caption = f"Receipt for order {order['id']} from {order.get('username') or order['user_id']}."
    for admin_id in ADMIN_IDS:
        if exclude_user_id and admin_id == exclude_user_id:
            continue
        try:
            if update.message.photo:
                await context.bot.send_photo(
                    admin_id,
                    update.message.photo[-1].file_id,
                    caption=caption,
                    reply_markup=order_admin_keyboard(order["id"]),
                )
            elif update.message.document:
                await context.bot.send_document(
                    admin_id,
                    update.message.document.file_id,
                    caption=caption,
                    reply_markup=order_admin_keyboard(order["id"]),
                )
            else:
                await context.bot.send_message(
                    admin_id,
                    f"{caption}\n\n{update.message.text or ''}",
                    reply_markup=order_admin_keyboard(order["id"]),
                )
        except Exception:
            logger.exception("Failed to forward receipt for order %s to admin %s", order["id"], admin_id)
            continue


async def start_receipt_upload(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: str) -> None:
    order = DB.get_order(order_id)
    if not order or int(order["user_id"]) != update.effective_user.id:
        await update.callback_query.answer("Order not found.", show_alert=True)
        return
    if order.get("status") in {"completed", "cancelled"}:
        await update.callback_query.answer("Receipt upload is closed for this order.", show_alert=True)
        return

    context.user_data["awaiting_proof_order_id"] = order_id
    await update.callback_query.edit_message_text(
        f"🧾 Send your receipt or payment screenshot for order {order_id} now.\n\n"
        "You can send a photo, document, or text reference. A clear screenshot helps admin verify faster, so include the amount and transaction reference if you have it.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Back to Order", callback_data=f"order:view:{order_id}")]]),
    )


async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        if update.callback_query:
            await update.callback_query.answer("Admin access only.", show_alert=True)
        else:
            await update.message.reply_text("Admin access only.", reply_markup=MAIN_MENU)
        return

    text = admin_dashboard_text()
    markup = admin_dashboard_keyboard()
    if update.callback_query:
        await update.callback_query.edit_message_text(
            text,
            reply_markup=markup,
            parse_mode=ParseMode.HTML,
        )
        return

    await update.message.reply_text(
        text,
        reply_markup=markup,
        parse_mode=ParseMode.HTML,
    )


async def admin_products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.callback_query.answer("Admin access only.", show_alert=True)
        return
    products = DB.snapshot()["products"].values()
    rows = []
    for product in products:
        state = "on" if product.get("active", True) else "off"
        rows.append([InlineKeyboardButton(f"{product['name']} ({state}, stock {product.get('stock', 0)})", callback_data=f"admin:product:{product['id']}")])
    rows.append([InlineKeyboardButton("Add Product", callback_data="admin:add_product")])
    rows.append([InlineKeyboardButton("Dashboard", callback_data="admin:dashboard")])
    await update.callback_query.edit_message_text(
        "<b>Products</b>",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.HTML,
    )


async def admin_product_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, product_id: str) -> None:
    product = DB.get_product(product_id)
    if not product:
        await update.callback_query.answer("Product not found.", show_alert=True)
        return
    rows = [
        [InlineKeyboardButton("Toggle Active", callback_data=f"admin:toggle_product:{product_id}")],
        [InlineKeyboardButton("Back to Products", callback_data="admin:products")],
        [InlineKeyboardButton("Dashboard", callback_data="admin:dashboard")],
    ]
    await update.callback_query.edit_message_text(product_text(product), reply_markup=InlineKeyboardMarkup(rows), parse_mode=ParseMode.HTML)


async def admin_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        await update.callback_query.answer("Admin access only.", show_alert=True)
        return
    orders = DB.recent_orders()
    rows = [
        [InlineKeyboardButton(f"{order['id']} - {ORDER_STATUSES.get(order['status'], order['status'])}", callback_data=f"order:view:{order['id']}")]
        for order in orders
    ]
    if not rows:
        rows = [[InlineKeyboardButton("No orders yet", callback_data="admin:noop")]]
    rows.append([InlineKeyboardButton("Dashboard", callback_data="admin:dashboard")])
    await update.callback_query.edit_message_text(
        "<b>Recent Orders</b>",
        reply_markup=InlineKeyboardMarkup(rows),
        parse_mode=ParseMode.HTML,
    )


async def admin_update_order(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: str, status: str) -> None:
    if not is_admin(update.effective_user.id):
        await update.callback_query.answer("Admin access only.", show_alert=True)
        return
    existing_order = DB.get_order(order_id)
    if status == "completed" and existing_order and not str(existing_order.get("delivery_message", "")).strip():
        await update.callback_query.answer("Send product/credentials from dashboard first.", show_alert=True)
        return
    order = DB.update_order(order_id, {"status": status})
    if not order:
        await update.callback_query.answer("Order not found.", show_alert=True)
        return
    await update.callback_query.answer(f"Order marked {ORDER_STATUSES.get(status, status)}.")
    await context.bot.send_message(
        order["user_id"],
        customer_text(int(order["user_id"]), "status_updated", order_id=order_id, status=ORDER_STATUSES.get(status, status)),
        reply_markup=(
            InlineKeyboardMarkup([[InlineKeyboardButton("Send Vouch for Warranty", callback_data=f"vouch:start:{order_id}")]])
            if status == "completed" and order_has_warranty(order) and DB.warranty_requires_vouch() and not order_has_vouch(order)
            else MAIN_MENU
        ),
    )
    await update.callback_query.edit_message_text(order_text(order), reply_markup=order_admin_keyboard(order_id), parse_mode=ParseMode.HTML)


async def start_add_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_admin(update.effective_user.id):
        await update.callback_query.answer("Admin access only.", show_alert=True)
        return ConversationHandler.END
    context.user_data["new_product"] = {}
    await update.callback_query.edit_message_text("Product name?")
    return ADD_NAME


async def add_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_product"]["name"] = update.message.text.strip()
    await update.message.reply_text("Product description?")
    return ADD_DESCRIPTION


async def add_description(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data["new_product"]["description"] = update.message.text.strip()
    await update.message.reply_text("USD price? Send a whole number, for example 10 for $10.00.")
    return ADD_PRICE


async def add_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        price = int(update.message.text.strip())
        if price < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Send a valid whole number for USD price.")
        return ADD_PRICE
    context.user_data["new_product"]["price_credits"] = price
    await update.message.reply_text("How many stock left? Send a whole number.")
    return ADD_STOCK


async def add_stock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        stock = int(update.message.text.strip())
        if stock < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Send a valid whole number for stock.")
        return ADD_STOCK
    context.user_data["new_product"]["stock"] = stock
    await update.message.reply_text("Warranty days? Send 0 if none.")
    return ADD_WARRANTY


async def add_warranty(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        days = int(update.message.text.strip())
        if days < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Send a valid whole number for warranty days.")
        return ADD_WARRANTY
    context.user_data["new_product"]["warranty_days"] = days
    await update.message.reply_text("Subscription days? Send 0 if none.")
    return ADD_SUBSCRIPTION


async def add_subscription(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    try:
        days = int(update.message.text.strip())
        if days < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Send a valid whole number for subscription days.")
        return ADD_SUBSCRIPTION
    context.user_data["new_product"]["subscription_days"] = days
    await update.message.reply_text("Upsell product IDs separated by comma, or send - for none.")
    return ADD_UPSELLS


async def add_upsells(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    raw = update.message.text.strip()
    upsell_ids = [] if raw == "-" else [item.strip() for item in raw.split(",") if item.strip()]
    product = context.user_data.pop("new_product")
    product.update(
        {
            "id": short_id(),
            "upsell_ids": upsell_ids,
            "active": True,
            "created_at": now_iso(),
            "updated_at": now_iso(),
        }
    )
    DB.add_product(product)
    await update.message.reply_text(
        f"Product created.\n\n{product_text(product)}",
        reply_markup=MAIN_MENU,
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


async def cancel_conversation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("new_product", None)
    context.user_data.pop("followup_order_id", None)
    await update.message.reply_text("Cancelled.", reply_markup=MAIN_MENU)
    return ConversationHandler.END


async def followup_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    orders = DB.user_orders(update.effective_user.id)
    if not orders:
        await update.message.reply_text(
            "You do not have orders to follow up yet.\n\n"
            "Start with Store when you are ready, and I will help you build the cart with useful add-ons before checkout.",
            reply_markup=MAIN_MENU,
        )
        return ConversationHandler.END
    rows = [[InlineKeyboardButton(f"{order['id']} - {ORDER_STATUSES.get(order['status'], order['status'])}", callback_data=f"followup:choose:{order['id']}")] for order in orders[:10]]
    await update.message.reply_text(
        "Choose the order you want to ask about.\n\n"
        "Send one clear message with the order question, receipt concern, or delivery note, and I will pass it straight to admin.",
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return FOLLOWUP_ORDER


async def followup_menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    orders = DB.user_orders(update.effective_user.id)
    if not orders:
        await update.callback_query.edit_message_text(
            "You do not have orders to follow up yet.\n\nStart with Store when you are ready, and I will help you build the cart with useful add-ons before checkout.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Store", callback_data="store:list")]]),
        )
        return
    rows = [
        [InlineKeyboardButton(f"{order['id']} - {ORDER_STATUSES.get(order['status'], order['status'])}", callback_data=f"followup:start:{order['id']}")]
        for order in orders[:10]
    ]
    rows.append([InlineKeyboardButton("Back", callback_data="orders:menu")])
    await update.callback_query.edit_message_text(
        "Choose the order you want to ask about.\n\nSend one clear message with the order question, receipt concern, or delivery note, and I will pass it straight to admin.",
        reply_markup=InlineKeyboardMarkup(rows),
    )


async def followup_start_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, order_id: str) -> int:
    order = DB.get_order(order_id)
    if not order or int(order["user_id"]) != update.effective_user.id:
        await update.callback_query.answer("Order not found.", show_alert=True)
        return ConversationHandler.END
    context.user_data["followup_order_id"] = order_id
    await update.callback_query.edit_message_text(
        f"💬 Send your follow-up message for order {order_id}.\n\n"
        "Include the detail admin needs, like payment reference, product question, or what you want checked. One tidy message is easiest to review."
    )
    return FOLLOWUP_MESSAGE


async def followup_choose(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    order_id = update.callback_query.data.split(":")[-1]
    return await followup_start_callback(update, context, order_id)


async def followup_start_entry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    order_id = update.callback_query.data.split(":")[-1]
    return await followup_start_callback(update, context, order_id)


async def followup_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    order_id = context.user_data.pop("followup_order_id", None)
    if not order_id:
        await update.message.reply_text("No order is selected yet. Tap Follow Up and choose the order first.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    order = DB.add_followup(order_id, update.effective_user.id, update.message.text.strip())
    if not order:
        await update.message.reply_text("Order not found.", reply_markup=MAIN_MENU)
        return ConversationHandler.END
    DB.add_user_message(update.effective_user, update.message.text.strip(), sender="customer", context=f"Order {order_id}")
    sent = await notify_admins(
        context,
        f"Follow-up for order {order_id} from {update.effective_user.full_name} ({update.effective_user.id}):\n\n{update.message.text.strip()}",
        order_admin_keyboard(order_id),
        exclude_user_id=update.effective_user.id,
    )
    if sent:
        await update.message.reply_text(customer_message_sent_text("follow-up"), reply_markup=MAIN_MENU)
    else:
        await update.message.reply_text(admin_delivery_failure_text(), reply_markup=MAIN_MENU)
    return ConversationHandler.END


async def start_contact_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data["awaiting_admin_message"] = True
    rows = []
    admin_contact_url = configured_admin_contact_url()
    if admin_contact_url:
        rows.append([InlineKeyboardButton("Open Admin Chat", url=admin_contact_url)])
    text = (
        "💬 Send your message for admin now.\n\n"
        "Write the question, order ID if you have one, and any detail admin needs. I will save it privately in the admin dashboard inbox."
    )
    markup = InlineKeyboardMarkup(rows) if rows else None
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await update.message.reply_text(text, reply_markup=markup)


async def handle_admin_contact_message(update: Update, context: ContextTypes.DEFAULT_TYPE, context_label: str = "Contact admin") -> None:
    context.user_data.pop("awaiting_admin_message", None)
    message = (update.message.text or update.message.caption or "").strip()
    if not message:
        await update.message.reply_text(
            "I can forward that, but please add a short message or caption so admin knows what you need.",
            reply_markup=MAIN_MENU,
        )
        return
    user = update.effective_user
    DB.add_user_message(user, message, sender="customer", context=context_label)
    logger.info("Customer message from %s saved to dashboard inbox.", user.id)


async def handle_admin_contact_media(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("awaiting_admin_message", None)
    user = update.effective_user
    caption = (update.message.caption or "").strip()
    media_type = "photo" if update.message.photo else "document" if update.message.document else "media"
    file_id = ""
    if update.message.photo:
        file_id = update.message.photo[-1].file_id
    elif update.message.document:
        file_id = update.message.document.file_id
    upload = cloudinary_upload_telegram_file(file_id, folder="telegram_bot/customer_messages")
    saved_message = f"[{media_type}] {caption}".strip()
    media_url = upload.get("media_url", "")
    DB.add_user_message(
        user,
        saved_message,
        sender="customer",
        context="Contact admin",
        media_url=media_url,
        media_type=media_type,
    )
    logger.info("Customer media message from %s saved to dashboard inbox.", user.id)


async def show_community(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    community_url = configured_community_url()
    if community_url:
        text = (
            "👥 Join the community here.\n\n"
            "It is a handy place for updates, restocks, and proof drops. You can come back to the bot anytime to browse products or finish checkout."
        )
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("Join Community", url=community_url)]])
    else:
        text = (
            "The community link is not configured yet.\n\n"
            "You can still browse Store, build your cart, or Contact Admin if you need a recommendation."
        )
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("Back", callback_data="support:menu")]])
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=markup)
    else:
        await update.message.reply_text(text, reply_markup=markup)


async def abandoned_cart_loop(app: Application) -> None:
    while True:
        await asyncio.sleep(60)
        settings = DB.abandoned_cart_settings()
        if not settings.get("abandoned_cart_enabled", False):
            continue
        interval = max(1, int(settings.get("abandoned_cart_interval_minutes", 60)))
        max_followups = max(1, int(settings.get("abandoned_cart_max_followups", 2)))
        messages = [msg for msg in settings.get("abandoned_cart_messages", []) if str(msg).strip()]
        if not messages:
            continue

        now = datetime.now(timezone.utc)
        for candidate in DB.abandoned_cart_candidates():
            user = candidate["user"]
            sent_count = int(user.get("abandoned_followups_sent", 0))
            if sent_count >= max_followups:
                continue
            cart_updated_at = parse_iso(user.get("cart_updated_at", ""))
            if not cart_updated_at:
                continue
            elapsed_minutes = (now - cart_updated_at).total_seconds() / 60
            required_minutes = interval * (sent_count + 1)
            if elapsed_minutes < required_minutes:
                continue
            message = messages[min(sent_count, len(messages) - 1)]
            try:
                await app.bot.send_message(
                    candidate["user_id"],
                    f"{message}\n\nOpen the bot and tap View Cart to continue. I will keep the checkout steps ready for you.",
                    reply_markup=MAIN_MENU,
                )
                DB.mark_abandoned_followup_sent(candidate["user_id"])
            except Exception:
                continue


async def post_init(app: Application) -> None:
    asyncio.create_task(abandoned_cart_loop(app))


async def handle_text_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    track_user(update)
    text = update.message.text.strip().lower()
    if text in MENU_COMMANDS:
        context.user_data.pop("awaiting_admin_message", None)
        context.user_data.pop("awaiting_vouch_order_id", None)

    if text == "store":
        await show_store(update, context)
    elif text == "view cart":
        await view_cart(update, context)
    elif text == "orders & follow up":
        await orders_support_menu(update, context)
    elif text == "support & community":
        await support_community_menu(update, context)
    elif text == "settings":
        await customer_settings_menu(update, context)
    elif text == "my orders":
        await my_orders(update, context)
    elif text == "admin":
        await admin_panel(update, context)
    elif text == "dashboard":
        await admin_panel(update, context)
    elif text == "follow up":
        await followup_entry(update, context)
    elif text == "contact admin":
        await start_contact_admin(update, context)
    elif text == "join community":
        await show_community(update, context)
    elif context.user_data.get("awaiting_vouch_order_id"):
        await handle_vouch_message(update, context)
    elif context.user_data.get("awaiting_proof_order_id"):
        await handle_payment_proof(update, context)
    elif context.user_data.get("awaiting_admin_message"):
        await handle_admin_contact_message(update, context)
    else:
        await handle_admin_contact_message(update, context, context_label="General message")


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    track_user(update)
    query = update.callback_query
    await query.answer()
    data = query.data
    parts = data.split(":")

    if data == "store:list":
        await show_store(update, context)
    elif data == "orders:menu":
        await orders_support_menu(update, context)
    elif data == "followup:menu":
        await followup_menu_callback(update, context)
    elif data == "support:menu":
        await support_community_menu(update, context)
    elif data == "support:contact":
        await start_contact_admin(update, context)
    elif data == "support:community":
        await show_community(update, context)
    elif data == "settings:menu":
        await customer_settings_menu(update, context)
    elif data == "settings:language":
        await customer_language_menu(update, context)
    elif parts[:2] == ["settings", "language"]:
        language = parts[2] if len(parts) > 2 else "en"
        if language not in CUSTOMER_LANGUAGES:
            return
        DB.update_user_settings(update.effective_user.id, {"language": language})
        await customer_settings_menu(update, context)
    elif data == "settings:notifications:toggle":
        settings = DB.user_settings(update.effective_user.id)
        DB.update_user_settings(update.effective_user.id, {"auto_updates_enabled": not settings.get("auto_updates_enabled", True)})
        await customer_settings_menu(update, context)
    elif data == "admin:dashboard":
        await admin_panel(update, context)
    elif parts[:2] == ["product", "view"]:
        await show_product(update, context, parts[2])
    elif parts[:2] == ["cart", "add"]:
        await add_cart(update, context, parts[2])
    elif data == "cart:view":
        await view_cart(update, context)
    elif parts[:2] == ["cart", "dec"]:
        await dec_cart(update, context, parts[2])
    elif parts[:2] == ["cart", "remove"]:
        await remove_cart(update, context, parts[2])
    elif data == "checkout:start":
        await checkout_start(update, context)
    elif parts[:2] == ["checkout", "pay"]:
        await checkout_payment(update, context, parts[2])
    elif data == "orders:mine":
        await my_orders(update, context)
    elif parts[:2] == ["order", "view"]:
        await show_order(update, context, parts[2])
    elif parts[:2] == ["receipt", "start"]:
        await start_receipt_upload(update, context, parts[2])
    elif parts[:2] == ["vouch", "start"]:
        await start_vouch(update, context, parts[2])
    elif data == "admin:products":
        await admin_products(update, context)
    elif data == "admin:orders":
        await admin_orders(update, context)
    elif parts[:2] == ["admin", "product"]:
        await admin_product_detail(update, context, parts[2])
    elif parts[:2] == ["admin", "toggle_product"]:
        product = DB.get_product(parts[2])
        if product:
            DB.update_product(parts[2], {"active": not product.get("active", True)})
        await admin_product_detail(update, context, parts[2])
    elif parts[:2] == ["admin", "order"]:
        await admin_update_order(update, context, parts[2], parts[3])
    elif data == "admin:noop":
        return


def build_application() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing. Copy .env.example to .env and set BOT_TOKEN.")

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    add_product_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(start_add_product, pattern=r"^admin:add_product$")],
        states={
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name)],
            ADD_DESCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_description)],
            ADD_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_price)],
            ADD_STOCK: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_stock)],
            ADD_WARRANTY: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_warranty)],
            ADD_SUBSCRIPTION: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_subscription)],
            ADD_UPSELLS: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_upsells)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )

    followup_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Regex(r"(?i)^follow up$"), followup_entry),
            CallbackQueryHandler(followup_choose, pattern=r"^followup:choose:"),
            CallbackQueryHandler(followup_start_entry, pattern=r"^followup:start:"),
        ],
        states={
            FOLLOWUP_ORDER: [CallbackQueryHandler(followup_choose, pattern=r"^followup:choose:")],
            FOLLOWUP_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, followup_message)],
        },
        fallbacks=[CommandHandler("cancel", cancel_conversation)],
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("dashboard", admin_panel))
    app.add_handler(add_product_conv)
    app.add_handler(followup_conv)
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_payment_proof))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_menu))
    return app


def main() -> None:
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    asyncio.set_event_loop(asyncio.new_event_loop())
    app = build_application()
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
