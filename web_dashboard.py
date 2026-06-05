import hmac
import json
import asyncio
import os
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from functools import wraps
from hashlib import sha1
from pathlib import Path
from threading import RLock
from typing import Any

from dotenv import load_dotenv
from flask import Flask, abort, flash, jsonify, redirect, render_template_string, request, session, url_for
from telegram import Update as TelegramUpdate


load_dotenv()

DB_PATH = Path(os.getenv("STORE_DB_PATH", "store_db.json"))
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "change-this-password")
DASHBOARD_SECRET_KEY = os.getenv("DASHBOARD_SECRET_KEY", "change-this-secret-key")
DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8080"))
CLOUDINARY_URL = os.getenv("CLOUDINARY_URL", "").strip()
CLOUDINARY_CLOUD_NAME = os.getenv("CLOUDINARY_CLOUD_NAME", "").strip()
CLOUDINARY_API_KEY = os.getenv("CLOUDINARY_API_KEY", "").strip()
CLOUDINARY_API_SECRET = os.getenv("CLOUDINARY_API_SECRET", "").strip()
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET", "").strip()
TELEGRAM_WEBHOOK_PATH_SECRET = os.getenv("TELEGRAM_WEBHOOK_PATH_SECRET", "").strip()

ORDER_STATUSES = {
    "pending_payment": "Pending payment",
    "proof_uploaded": "Proof uploaded",
    "paid": "Paid",
    "processing": "Processing",
    "completed": "Completed",
    "cancelled": "Cancelled",
}

LANGUAGES = {
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

SOCIAL_PROOF_DEFAULTS = [
    "✅ Another happy customer just got their order.",
    "🔥 Fresh order completed. Thanks for trusting us.",
    "💯 Proof update: another customer order is done.",
    "🎉 Another smooth delivery completed.",
    "🧾 Real order, real proof, another happy customer.",
    "🚀 Another customer secured their product.",
    "⭐ Customer order completed successfully.",
    "🙌 Another legit delivery finished.",
    "📦 Fresh delivery completed with receipt proof.",
    "💬 Customer checked out, paid, and got handled smoothly.",
    "🔐 Another order verified and completed.",
    "⚡ Fast checkout, verified receipt, smooth finish.",
    "🏁 Another customer order is complete.",
    "🛒 Real checkout completed, proof attached when available.",
    "✅ Order done. Receipt proof speaks for itself.",
    "🔥 Stock moved again - another customer secured theirs.",
    "⭐ Smooth service, verified payment, completed order.",
    "🧾 Receipt checked, order completed, customer handled.",
]

TEXT = {
    "new_product": {
        "en": "ðŸ†• New product is live",
        "tl": "ðŸ†• May bagong produkto",
        "es": "ðŸ†• Nuevo producto disponible",
    },
    "restock": {
        "en": "ðŸ”” Restock alert",
        "tl": "ðŸ”” May bagong stock",
        "es": "ðŸ”” ReposiciÃ³n disponible",
    },
    "price": {"en": "Price", "tl": "Presyo", "es": "Precio"},
    "stock": {"en": "Stock", "tl": "Stock", "es": "Stock"},
    "stock_now": {"en": "Stock now", "tl": "Stock ngayon", "es": "Stock ahora"},
    "bulk_prefix": {"en": "ðŸ“£", "tl": "ðŸ“£", "es": "ðŸ“£"},
    "status_updated": {
        "en": "Order {order_id} status updated: {status}.",
        "tl": "Na-update ang status ng order {order_id}: {status}.",
        "es": "Estado del pedido {order_id} actualizado: {status}.",
    },
    "followup": {
        "en": "ðŸ’¬ Follow-up for order {order_id}:",
        "tl": "ðŸ’¬ Follow-up para sa order {order_id}:",
        "es": "ðŸ’¬ Seguimiento del pedido {order_id}:",
    },
}

DEFAULT_DATA = {
    "products": {},
    "carts": {},
    "orders": {},
    "users": {},
    "settings": {
        "payment_methods": [],
        "checkout_instructions": "",
        "admin_contact_url": "",
        "community_url": "",
        "playful_mode": True,
        "meme_gif_urls": [],
        "social_proof_enabled": False,
        "social_proof_attach_receipt": True,
        "warranty_requires_vouch": True,
        "social_proof_templates": SOCIAL_PROOF_DEFAULTS,
        "social_proof_template_index": 0,
        "wallet_addresses": [],
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

app = Flask(__name__)
app.secret_key = DASHBOARD_SECRET_KEY
_telegram_application = None
_telegram_loop = asyncio.new_event_loop()
_telegram_lock = RLock()


async def telegram_application():
    global _telegram_application
    if _telegram_application is None:
        from bot import build_application

        application = build_application()
        await application.initialize()
        await application.start()
        _telegram_application = application
    return _telegram_application


def run_telegram_webhook(payload: dict[str, Any]) -> None:
    async def process_payload() -> None:
        application = await telegram_application()
        update = TelegramUpdate.de_json(payload, application.bot)
        if update:
            await application.process_update(update)

    with _telegram_lock:
        _telegram_loop.run_until_complete(process_payload())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def short_id() -> str:
    return uuid.uuid4().hex[:8].upper()


def money(value: int) -> str:
    return f"${value:,.2f}"


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
        return {}


def cloudinary_media_url(media_url: str, media_type: str = "media") -> str:
    if not media_url:
        return ""
    if "res.cloudinary.com" in media_url:
        return media_url
    return cloudinary_upload_url(media_url, folder=f"telegram_bot/admin_{media_type}").get("media_url") or media_url


def t(data: dict[str, Any], key: str, **kwargs) -> str:
    language = data.get("settings", {}).get("language", "en")
    value = TEXT.get(key, {}).get(language) or TEXT.get(key, {}).get("en") or key
    return value.format(**kwargs)


def t_user(data: dict[str, Any], user_id: int, key: str, **kwargs) -> str:
    language = data.get("users", {}).get(str(user_id), {}).get("language", "en")
    value = TEXT.get(key, {}).get(language) or TEXT.get(key, {}).get("en") or key
    return value.format(**kwargs)


def parse_env_payment_methods(raw: str) -> list[dict[str, Any]]:
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
                "active": True,
                "created_at": now_iso(),
                "updated_at": now_iso(),
            }
        )
    return methods


def default_data() -> dict[str, Any]:
    return {
        "products": {},
        "carts": {},
        "orders": {},
        "users": {},
        "settings": {
            "payment_methods": [],
            "checkout_instructions": "",
            "admin_contact_url": "",
            "community_url": "",
            "playful_mode": True,
            "meme_gif_urls": [],
            "social_proof_enabled": False,
            "social_proof_attach_receipt": True,
            "warranty_requires_vouch": True,
            "social_proof_templates": SOCIAL_PROOF_DEFAULTS,
            "social_proof_template_index": 0,
            "wallet_addresses": [],
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


def load_data() -> dict[str, Any]:
    if not DB_PATH.exists():
        save_data(default_data())
    with DB_PATH.open("r", encoding="utf-8-sig") as db_file:
        data = json.load(db_file)
    changed = False
    settings_missing = "settings" not in data
    for key, value in default_data().items():
        if key not in data:
            data[key] = value
            changed = True
    if settings_missing or "payment_methods" not in data["settings"]:
        data["settings"]["payment_methods"] = parse_env_payment_methods(os.getenv("PAYMENT_METHODS", ""))
        changed = True
    if "checkout_instructions" not in data["settings"]:
        data["settings"]["checkout_instructions"] = ""
        changed = True
    if "admin_contact_url" not in data["settings"]:
        data["settings"]["admin_contact_url"] = os.getenv("ADMIN_CONTACT_URL", "").strip()
        changed = True
    if "community_url" not in data["settings"]:
        data["settings"]["community_url"] = os.getenv("COMMUNITY_URL", "").strip()
        changed = True
    if "playful_mode" not in data["settings"]:
        data["settings"]["playful_mode"] = True
        changed = True
    if "meme_gif_urls" not in data["settings"]:
        data["settings"]["meme_gif_urls"] = []
        changed = True
    if "social_proof_enabled" not in data["settings"]:
        data["settings"]["social_proof_enabled"] = False
        changed = True
    if "social_proof_attach_receipt" not in data["settings"]:
        data["settings"]["social_proof_attach_receipt"] = True
        changed = True
    if "warranty_requires_vouch" not in data["settings"]:
        data["settings"]["warranty_requires_vouch"] = True
        changed = True
    if "wallet_addresses" not in data["settings"]:
        data["settings"]["wallet_addresses"] = []
        changed = True
    if "social_proof_templates" not in data["settings"]:
        data["settings"]["social_proof_templates"] = SOCIAL_PROOF_DEFAULTS
        changed = True
    else:
        existing_templates = [str(item).strip() for item in data["settings"].get("social_proof_templates", []) if str(item).strip()]
        merged_templates = existing_templates + [item for item in SOCIAL_PROOF_DEFAULTS if item not in existing_templates]
        if merged_templates != existing_templates:
            data["settings"]["social_proof_templates"] = merged_templates
            changed = True
    if "social_proof_template_index" not in data["settings"]:
        data["settings"]["social_proof_template_index"] = 0
        changed = True
    if "language" not in data["settings"]:
        data["settings"]["language"] = "en"
        changed = True
    if "abandoned_cart_enabled" not in data["settings"]:
        data["settings"]["abandoned_cart_enabled"] = False
        changed = True
    if "abandoned_cart_interval_minutes" not in data["settings"]:
        data["settings"]["abandoned_cart_interval_minutes"] = 60
        changed = True
    if "abandoned_cart_max_followups" not in data["settings"]:
        data["settings"]["abandoned_cart_max_followups"] = 2
        changed = True
    if "abandoned_cart_messages" not in data["settings"]:
        data["settings"]["abandoned_cart_messages"] = [
            "Your cart is still saved for you. If you want the smoothest checkout, open View Cart, review the recommended add-ons, then pick your payment method when you are ready.",
            "Friendly last nudge: your cart is still waiting. Stock can move, so open View Cart soon if you want to keep these items and add any final upgrades before payment.",
        ]
        changed = True
    highlight_slots: dict[int, str] = {}
    for product_id, product in data.get("products", {}).items():
        try:
            rank = int(product.get("highlight_rank", 0))
        except (TypeError, ValueError):
            rank = 0
        if rank not in (0, 1, 2):
            rank = 0
        if rank in (1, 2) and rank in highlight_slots:
            rank = 0
        elif rank in (1, 2):
            highlight_slots[rank] = product_id
        if product.get("highlight_rank") != rank:
            product["highlight_rank"] = rank
            changed = True
    for order in data.get("orders", {}).values():
        if "vouches" not in order:
            order["vouches"] = []
            changed = True
        if "delivery_message" not in order:
            order["delivery_message"] = ""
            changed = True
        if "delivered_at" not in order:
            order["delivered_at"] = ""
            changed = True
        user_id = str(order.get("user_id", ""))
        if user_id and user_id not in data["users"]:
            data["users"][user_id] = {
                "id": int(order["user_id"]),
                "username": order.get("username"),
                "full_name": order.get("username"),
                "auto_updates_enabled": True,
                "language": "en",
                "updated_at": order.get("created_at", now_iso()),
            }
            changed = True
    for user in data.get("users", {}).values():
        if "auto_updates_enabled" not in user:
            user["auto_updates_enabled"] = True
            changed = True
        if "language" not in user:
            user["language"] = "en"
            changed = True
        if "messages" not in user:
            user["messages"] = []
            changed = True
        if "vouches" not in user:
            user["vouches"] = []
            changed = True
    if changed:
        save_data(data)
    return data


def save_data(data: dict[str, Any]) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=DB_PATH.name, suffix=".tmp", dir=DB_PATH.parent or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
            json.dump(data, temp_file, indent=2)
        for attempt in range(5):
            try:
                os.replace(temp_name, DB_PATH)
                break
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.1)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def login_required(handler):
    @wraps(handler)
    def wrapper(*args, **kwargs):
        if not session.get("dashboard_authed"):
            return redirect(url_for("login", next=request.path))
        return handler(*args, **kwargs)

    return wrapper


def parse_int(name: str, default: int = 0) -> int:
    raw = request.form.get(name, "").strip()
    if raw == "":
        return default
    value = int(raw)
    if value < 0:
        raise ValueError(f"{name} cannot be negative")
    return value


def parse_upsells() -> list[str]:
    raw = request.form.get("upsell_ids", "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def product_from_form(product_id: str | None = None) -> dict[str, Any]:
    name = request.form.get("name", "").strip()
    description = request.form.get("description", "").strip()
    if not name:
        raise ValueError("Product name is required")
    highlight_rank = parse_int("highlight_rank", 0)
    if highlight_rank not in (0, 1, 2):
        raise ValueError("Store highlight must be 0, 1, or 2")
    return {
        "id": product_id or short_id(),
        "name": name,
        "description": description,
        "price_credits": parse_int("price_credits"),
        "stock": parse_int("stock"),
        "warranty_days": parse_int("warranty_days"),
        "subscription_days": parse_int("subscription_days"),
        "upsell_ids": parse_upsells(),
        "highlight_rank": highlight_rank,
        "active": request.form.get("active") == "on",
    }


def enforce_highlight_slots(data: dict[str, Any], selected_product_id: str) -> None:
    selected = data.get("products", {}).get(selected_product_id)
    if not selected:
        return
    try:
        selected_rank = int(selected.get("highlight_rank", 0))
    except (TypeError, ValueError):
        selected_rank = 0
    if selected_rank not in (1, 2):
        selected["highlight_rank"] = 0
        return
    for product_id, product in data.get("products", {}).items():
        if product_id != selected_product_id and int(product.get("highlight_rank", 0) or 0) == selected_rank:
            product["highlight_rank"] = 0
            product["updated_at"] = now_iso()


def payment_method_from_form(method_id: str | None = None) -> dict[str, Any]:
    label = request.form.get("label", "").strip()
    instructions = request.form.get("instructions", "").strip()
    if not label:
        raise ValueError("Payment method name is required")
    if not instructions:
        raise ValueError("Payment instructions are required")
    return {
        "id": method_id or f"pay_{short_id()}",
        "label": label,
        "instructions": instructions,
        "active": request.form.get("active") == "on",
    }


def send_telegram_message(chat_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> bool:
    if not BOT_TOKEN:
        return False
    payload_data = {"chat_id": chat_id, "text": text}
    if reply_markup:
        payload_data["reply_markup"] = json.dumps(reply_markup)
    payload = urllib.parse.urlencode(payload_data).encode("utf-8")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        request_obj = urllib.request.Request(url, data=payload, method="POST")
        with urllib.request.urlopen(request_obj, timeout=10) as response:
            return response.status == 200
    except Exception:
        return False


def send_telegram_animation(chat_id: int, animation_url: str, caption: str = "") -> bool:
    if not BOT_TOKEN or not animation_url:
        return False
    payload = urllib.parse.urlencode(
        {"chat_id": chat_id, "animation": animation_url, "caption": caption}
    ).encode("utf-8")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendAnimation"
    try:
        request_obj = urllib.request.Request(url, data=payload, method="POST")
        with urllib.request.urlopen(request_obj, timeout=10) as response:
            return response.status == 200
    except Exception:
        return False


def send_telegram_media(chat_id: int, media_type: str, media_url: str, caption: str = "") -> bool:
    if not BOT_TOKEN or not media_url:
        return False
    cloudinary_media = cloudinary_upload_url(media_url, folder=f"telegram_bot/admin_{media_type}").get("media_url")
    media_url = cloudinary_media or media_url
    method_map = {
        "photo": ("sendPhoto", "photo"),
        "animation": ("sendAnimation", "animation"),
        "document": ("sendDocument", "document"),
        "video": ("sendVideo", "video"),
    }
    method_info = method_map.get(media_type)
    if not method_info:
        return False
    method, field = method_info
    payload = urllib.parse.urlencode(
        {"chat_id": chat_id, field: media_url, "caption": caption}
    ).encode("utf-8")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    try:
        request_obj = urllib.request.Request(url, data=payload, method="POST")
        with urllib.request.urlopen(request_obj, timeout=10) as response:
            return response.status == 200
    except Exception:
        return False


def known_user_ids(data: dict[str, Any]) -> list[int]:
    ids = {int(user_id) for user_id in data.get("users", {}) if str(user_id).isdigit()}
    for order in data.get("orders", {}).values():
        if str(order.get("user_id", "")).isdigit():
            ids.add(int(order["user_id"]))
    for user_id in data.get("carts", {}):
        if str(user_id).isdigit():
            ids.add(int(user_id))
    return sorted(ids)


def broadcast_message(
    data: dict[str, Any],
    message: str,
    media_url: str = "",
    media_type: str = "animation",
    exclude_user_id: int | None = None,
    respect_auto_updates: bool = False,
) -> tuple[int, int]:
    sent = 0
    failed = 0
    for user_id in known_user_ids(data):
        if exclude_user_id and user_id == exclude_user_id:
            continue
        user = data.get("users", {}).get(str(user_id), {})
        if respect_auto_updates and not user.get("auto_updates_enabled", True):
            continue
        if media_url:
            ok = send_telegram_media(user_id, media_type, media_url, message)
        else:
            ok = send_telegram_message(user_id, message)
        if ok:
            sent += 1
        else:
            failed += 1
    return sent, failed


def maybe_notify_product_change(data: dict[str, Any], product: dict[str, Any], previous: dict[str, Any] | None) -> None:
    if not product.get("active", True) or int(product.get("stock", 0)) <= 0:
        return

    if previous is None:
        message = (
            f"ðŸ†• New product is live: {product['name']}\n"
            f"Price: {money(int(product.get('price_credits', 0)))}\n"
            f"Stock: {int(product.get('stock', 0))}"
        )
    else:
        old_stock = int(previous.get("stock", 0))
        new_stock = int(product.get("stock", 0))
        was_inactive = not previous.get("active", True)
        if not was_inactive and new_stock <= old_stock:
            return
        message = (
            f"ðŸ”” Restock alert: {product['name']}\n"
            f"Stock now: {new_stock}\n"
            f"Price: {money(int(product.get('price_credits', 0)))}"
        )

    gif_urls = data.get("settings", {}).get("meme_gif_urls", [])
    gif_url = gif_urls[0] if data.get("settings", {}).get("playful_mode", True) and gif_urls else ""
    broadcast_message(data, message, gif_url)


def maybe_notify_product_change(data: dict[str, Any], product: dict[str, Any], previous: dict[str, Any] | None) -> None:
    if not product.get("active", True) or int(product.get("stock", 0)) <= 0:
        return

    if previous is None:
        message = (
            f"{t(data, 'new_product')}: {product['name']}\n"
            f"{t(data, 'price')}: {money(int(product.get('price_credits', 0)))}\n"
            f"{t(data, 'stock')}: {int(product.get('stock', 0))}"
        )
    else:
        old_stock = int(previous.get("stock", 0))
        new_stock = int(product.get("stock", 0))
        was_inactive = not previous.get("active", True)
        if not was_inactive and new_stock <= old_stock:
            return
        message = (
            f"{t(data, 'restock')}: {product['name']}\n"
            f"{t(data, 'stock_now')}: {new_stock}\n"
            f"{t(data, 'price')}: {money(int(product.get('price_credits', 0)))}"
        )

    gif_urls = data.get("settings", {}).get("meme_gif_urls", [])
    gif_url = gif_urls[0] if data.get("settings", {}).get("playful_mode", True) and gif_urls else ""
    broadcast_message(data, message, media_url=gif_url, media_type="animation", respect_auto_updates=True)


def next_social_proof_message(data: dict[str, Any]) -> str:
    settings = data.setdefault("settings", {})
    templates = [
        item.strip()
        for item in settings.get("social_proof_templates", [])
        if str(item).strip()
    ]
    if not templates:
        templates = ["Another happy customer just got their order."]
    index = int(settings.get("social_proof_template_index", 0)) % len(templates)
    settings["social_proof_template_index"] = index + 1
    return templates[index]


def maybe_send_social_proof(data: dict[str, Any], order: dict[str, Any], previous_status: str | None) -> None:
    settings = data.get("settings", {})
    if not settings.get("social_proof_enabled", False):
        return
    if previous_status == "completed" or order.get("status") != "completed":
        return
    send_social_proof_for_order(data, order, respect_auto_updates=True)


def send_social_proof_for_order(data: dict[str, Any], order: dict[str, Any], respect_auto_updates: bool = True) -> tuple[int, int]:
    settings = data.get("settings", {})
    message = next_social_proof_message(data)
    media_url = ""
    media_type = "photo"
    proof = order.get("proof") or {}
    if settings.get("social_proof_attach_receipt", True):
        if proof.get("type") == "photo":
            media_url = proof.get("media_url") or telegram_file_url(proof.get("value"))
            media_type = "photo"
        elif proof.get("type") == "document":
            media_url = proof.get("media_url") or telegram_file_url(proof.get("value"))
            media_type = "document"
        elif proof.get("type") == "text" and proof.get("value"):
            message = f"{message}\n\nReceipt proof: {proof.get('value')}"

    return broadcast_message(
        data,
        message,
        media_url=media_url,
        media_type=media_type,
        exclude_user_id=int(order["user_id"]),
        respect_auto_updates=respect_auto_updates,
    )


def order_has_warranty(order: dict[str, Any]) -> bool:
    return any(int(item.get("warranty_days", 0)) > 0 for item in order.get("items", []))


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
        return ""


def dashboard_stats(data: dict[str, Any]) -> dict[str, int]:
    products = list(data["products"].values())
    orders = list(data["orders"].values())
    active_products = [product for product in products if product.get("active", True)]
    paid_orders = [order for order in orders if order.get("status") != "cancelled"]
    completed_orders = [order for order in orders if order.get("status") == "completed"]
    total_value = sum(int(order.get("total_credits", 0)) for order in paid_orders)
    users = known_user_ids(data)
    customers = customer_metrics(data)
    non_buyers = len([customer for customer in customers if not customer["has_bought"]])
    return {
        "products": len(products),
        "active_products": len(active_products),
        "stock": sum(int(product.get("stock", 0)) for product in active_products),
        "low_stock": len([product for product in active_products if 0 < int(product.get("stock", 0)) <= 5]),
        "out_of_stock": len([product for product in active_products if int(product.get("stock", 0)) <= 0]),
        "orders": len(orders),
        "pending": len([order for order in orders if order.get("status") in {"pending_payment", "proof_uploaded"}]),
        "processing": len([order for order in orders if order.get("status") == "processing"]),
        "completed": len(completed_orders),
        "users": len(users),
        "non_buyers": non_buyers,
        "churn_rate": int((non_buyers / len(users)) * 100) if users else 0,
        "average_order_value": int(total_value / len(paid_orders)) if paid_orders else 0,
        "lifetime_value": int(total_value / len(users)) if users else 0,
        "open_value": total_value,
        "completed_value": sum(int(order.get("total_credits", 0)) for order in completed_orders),
    }


def top_selling_products(data: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    totals: dict[str, dict[str, Any]] = {}
    for order in data.get("orders", {}).values():
        if order.get("status") == "cancelled":
            continue
        for item in order.get("items", []):
            product_id = item.get("product_id", "")
            entry = totals.setdefault(
                product_id,
                {
                    "product_id": product_id,
                    "name": item.get("name", product_id),
                    "qty": 0,
                    "revenue": 0,
                    "orders": 0,
                },
            )
            entry["qty"] += int(item.get("qty", 0))
            entry["revenue"] += int(item.get("line_total", 0))
            entry["orders"] += 1
    return sorted(totals.values(), key=lambda item: (item["qty"], item["revenue"]), reverse=True)[:limit]


def customer_metrics(data: dict[str, Any]) -> list[dict[str, Any]]:
    users: dict[str, dict[str, Any]] = {}
    for user_id, user in data.get("users", {}).items():
        users[str(user_id)] = {
            "user_id": int(user_id),
            "username": user.get("username"),
            "full_name": user.get("full_name"),
            "auto_updates_enabled": user.get("auto_updates_enabled", True),
            "language": user.get("language", "en"),
            "unread_count": len([msg for msg in user.get("messages", []) if msg.get("from") == "customer" and not msg.get("read")]),
            "orders": 0,
            "completed_orders": 0,
            "total_value": 0,
            "average_order_value": 0,
            "last_order_at": "",
            "has_bought": False,
        }

    for order in data.get("orders", {}).values():
        user_id = str(order.get("user_id"))
        entry = users.setdefault(
            user_id,
            {
                "user_id": int(order.get("user_id", 0)),
                "username": order.get("username"),
                "full_name": order.get("username"),
                "auto_updates_enabled": data.get("users", {}).get(user_id, {}).get("auto_updates_enabled", True),
                "language": data.get("users", {}).get(user_id, {}).get("language", "en"),
                "unread_count": len([msg for msg in data.get("users", {}).get(user_id, {}).get("messages", []) if msg.get("from") == "customer" and not msg.get("read")]),
                "orders": 0,
                "completed_orders": 0,
                "total_value": 0,
                "average_order_value": 0,
                "last_order_at": "",
                "has_bought": False,
            },
        )
        if order.get("status") == "cancelled":
            continue
        entry["orders"] += 1
        entry["has_bought"] = True
        entry["total_value"] += int(order.get("total_credits", 0))
        if order.get("status") == "completed":
            entry["completed_orders"] += 1
        if order.get("created_at", "") > entry["last_order_at"]:
            entry["last_order_at"] = order.get("created_at", "")

    for entry in users.values():
        entry["average_order_value"] = int(entry["total_value"] / entry["orders"]) if entry["orders"] else 0

    return sorted(users.values(), key=lambda item: (item["total_value"], item["orders"]), reverse=True)


def top_customers(data: dict[str, Any], limit: int = 5) -> list[dict[str, Any]]:
    return [customer for customer in customer_metrics(data) if customer["has_bought"]][:limit]


def user_orders(data: dict[str, Any], user_id: int) -> list[dict[str, Any]]:
    orders = [order for order in data.get("orders", {}).values() if int(order.get("user_id", 0)) == user_id]
    return sorted(orders, key=lambda order: order.get("created_at", ""), reverse=True)


def user_message_history(data: dict[str, Any], user_id: int) -> list[dict[str, Any]]:
    user = data.get("users", {}).get(str(user_id), {})
    messages = list(user.get("messages", []))
    for order in user_orders(data, user_id):
        proof = order.get("proof") or {}
        if proof:
            media_url = ""
            media_type = proof.get("type", "")
            message = f"Payment receipt uploaded for order {order['id']}."
            if proof.get("type") == "text":
                message = f"Payment receipt text for order {order['id']}:\n\n{proof.get('value', '')}"
            elif proof.get("type") in {"photo", "document"}:
                media_url = proof.get("media_url") or telegram_file_url(proof.get("value"))
            messages.append(
                {
                    "from": "customer",
                    "message": message,
                    "media_url": media_url,
                    "media_type": media_type,
                    "created_at": order.get("updated_at") or order.get("created_at", ""),
                    "context": f"Receipt | Order {order['id']}",
                    "read": True,
                }
            )
        for item in order.get("followups", []):
            messages.append(
                {
                    "from": item.get("from", "customer"),
                    "message": item.get("message", ""),
                    "media_url": "",
                    "media_type": "",
                    "created_at": item.get("created_at", ""),
                    "context": f"Order {order['id']}",
                }
            )
        for item in order.get("vouches", []):
            proof = item.get("proof") or {}
            media_url = proof.get("media_url") or (telegram_file_url(proof.get("value")) if proof.get("type") in {"photo", "document"} else "")
            messages.append(
                {
                    "from": item.get("from", "customer"),
                    "message": item.get("message", ""),
                    "media_url": media_url,
                    "media_type": proof.get("type", ""),
                    "created_at": item.get("created_at", ""),
                    "context": f"Vouch | Order {order['id']}",
                }
            )
    return sorted(messages, key=lambda item: item.get("created_at", ""))


def filtered_orders(data: dict[str, Any], date_from: str = "", date_to: str = "") -> list[dict[str, Any]]:
    orders = list(data.get("orders", {}).values())
    if date_from:
        orders = [order for order in orders if order.get("created_at", "")[:10] >= date_from]
    if date_to:
        orders = [order for order in orders if order.get("created_at", "")[:10] <= date_to]
    return orders


def data_with_orders(data: dict[str, Any], orders: list[dict[str, Any]]) -> dict[str, Any]:
    scoped = deepcopy(data)
    scoped["orders"] = {order["id"]: order for order in orders}
    return scoped


def page_number(name: str = "page") -> int:
    try:
        return max(1, int(request.args.get(name, "1")))
    except ValueError:
        return 1


def paginate(items: list[Any], page: int, per_page: int = 10) -> dict[str, Any]:
    total = len(items)
    pages = max(1, (total + per_page - 1) // per_page)
    page = min(max(1, page), pages)
    start = (page - 1) * per_page
    return {
        "items": items[start:start + per_page],
        "page": page,
        "pages": pages,
        "total": total,
        "has_prev": page > 1,
        "has_next": page < pages,
        "prev_page": page - 1,
        "next_page": page + 1,
    }


def parse_wallet_addresses(raw: str) -> list[dict[str, str]]:
    wallets = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split("|")]
        if len(parts) == 1:
            label, network, address, note = "Wallet", "", parts[0], ""
        elif len(parts) == 2:
            label, network, address, note = parts[0], "", parts[1], ""
        elif len(parts) == 3:
            label, network, address, note = parts[0], parts[1], parts[2], ""
        else:
            label, network, address, note = parts[0], parts[1], parts[2], " | ".join(parts[3:])
        if address:
            wallets.append({"label": label or "Wallet", "network": network, "address": address, "note": note})
    return wallets


def wallet_addresses_text(wallets: list[dict[str, str]]) -> str:
    lines = []
    for wallet in wallets:
        lines.append(
            " | ".join(
                [
                    str(wallet.get("label", "")).strip(),
                    str(wallet.get("network", "")).strip(),
                    str(wallet.get("address", "")).strip(),
                    str(wallet.get("note", "")).strip(),
                ]
            ).rstrip(" |")
        )
    return "\n".join(lines)


def pager(param_name: str, page_obj: dict[str, Any]) -> str:
    if page_obj["pages"] <= 1:
        return ""
    links = [f"<span class=\"muted\">Page {page_obj['page']} of {page_obj['pages']} ({page_obj['total']} total)</span>"]
    if page_obj["has_prev"]:
        args = request.args.to_dict()
        args[param_name] = str(page_obj["prev_page"])
        links.append(f"<a class=\"button\" href=\"?{urllib.parse.urlencode(args)}\">Previous</a>")
    if page_obj["has_next"]:
        args = request.args.to_dict()
        args[param_name] = str(page_obj["next_page"])
        links.append(f"<a class=\"button\" href=\"?{urllib.parse.urlencode(args)}\">Next</a>")
    return f"<div class=\"actions\" style=\"padding:12px 16px\">{''.join(links)}</div>"


def apply_customer_filters(customers: list[dict[str, Any]], args: Any) -> list[dict[str, Any]]:
    query = args.get("q", "").strip().lower()
    status_filter = args.get("status", "")
    date_from = args.get("date_from", "")
    date_to = args.get("date_to", "")
    sort = args.get("sort", "lifetime_desc")

    def int_arg(name: str) -> int | None:
        raw = args.get(name, "").strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    min_aov = int_arg("min_aov")
    max_aov = int_arg("max_aov")
    min_ltv = int_arg("min_ltv")
    max_ltv = int_arg("max_ltv")

    rows = list(customers)
    if query:
        rows = [
            user for user in rows
            if query in str(user["user_id"]).lower()
            or query in str(user.get("username") or "").lower()
            or query in str(user.get("full_name") or "").lower()
        ]
    if status_filter == "buyer":
        rows = [user for user in rows if user["has_bought"]]
    elif status_filter == "no_purchase":
        rows = [user for user in rows if not user["has_bought"]]
    elif status_filter == "unread":
        rows = [user for user in rows if user["unread_count"] > 0]
    if date_from:
        rows = [user for user in rows if user["last_order_at"] and user["last_order_at"][:10] >= date_from]
    if date_to:
        rows = [user for user in rows if user["last_order_at"] and user["last_order_at"][:10] <= date_to]
    if min_aov is not None:
        rows = [user for user in rows if int(user["average_order_value"]) >= min_aov]
    if max_aov is not None:
        rows = [user for user in rows if int(user["average_order_value"]) <= max_aov]
    if min_ltv is not None:
        rows = [user for user in rows if int(user["total_value"]) >= min_ltv]
    if max_ltv is not None:
        rows = [user for user in rows if int(user["total_value"]) <= max_ltv]

    if sort == "recent":
        return sorted(rows, key=lambda user: user["last_order_at"], reverse=True)
    if sort == "avg_order_desc":
        return sorted(rows, key=lambda user: user["average_order_value"], reverse=True)
    if sort == "orders_desc":
        return sorted(rows, key=lambda user: user["orders"], reverse=True)
    if sort == "unread_desc":
        return sorted(rows, key=lambda user: user["unread_count"], reverse=True)
    if sort == "name":
        return sorted(rows, key=lambda user: str(user.get("username") or user.get("full_name") or user["user_id"]).lower())
    return sorted(rows, key=lambda user: user["total_value"], reverse=True)


def selected_broadcast_users(data: dict[str, Any], form: Any) -> list[int]:
    raw_ids = form.get("specific_user_ids", "").strip()
    if raw_ids:
        ids = []
        for item in raw_ids.replace("\n", ",").split(","):
            item = item.strip()
            if item.isdigit():
                ids.append(int(item))
        return sorted(set(ids))
    customers = apply_customer_filters(customer_metrics(data), form)
    return [int(customer["user_id"]) for customer in customers]


def update_order_status(order_id: str, status: str) -> bool:
    data = load_data()
    order = data["orders"].get(order_id)
    if not order or status not in ORDER_STATUSES:
        return False
    if status == "completed" and not str(order.get("delivery_message", "")).strip():
        return False
    previous_status = order.get("status")
    order["status"] = status
    order["updated_at"] = now_iso()
    if status == "cancelled" and previous_status != "cancelled":
        for item in order.get("items", []):
            product = data["products"].get(item.get("product_id"))
            if product:
                product["stock"] = int(product.get("stock", 0)) + int(item.get("qty", 0))
    maybe_send_social_proof(data, order, previous_status)
    save_data(data)
    message = t_user(data, int(order["user_id"]), "status_updated", order_id=order_id, status=ORDER_STATUSES[status])
    if (
        status == "completed"
        and data.get("settings", {}).get("warranty_requires_vouch", True)
        and order_has_warranty(order)
        and not order.get("vouches")
    ):
        message += "\n\nWarranty note: please open the bot, go to Orders & Follow Up > My Orders, open this order, and send a vouch to activate warranty coverage."
    send_telegram_message(int(order["user_id"]), message)
    return True


BASE_TEMPLATE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{{ title }} - Store Dashboard</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --ink: #18202a;
      --muted: #647184;
      --line: #d8dde6;
      --accent: #16745f;
      --accent-dark: #105747;
      --warn: #a85d00;
      --danger: #a83232;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--ink);
      font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    header {
      background: var(--panel);
      border-bottom: 1px solid var(--line);
      position: sticky;
      top: 0;
      z-index: 2;
    }
    .bar, main {
      max-width: 1180px;
      margin: 0 auto;
      padding: 16px;
    }
    .bar {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
    }
    .brand {
      font-weight: 700;
      font-size: 18px;
    }
    nav {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
    }
    a, button {
      color: inherit;
      font: inherit;
    }
    .navlink, .button, button {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 36px;
      padding: 8px 12px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      text-decoration: none;
      cursor: pointer;
      white-space: nowrap;
    }
    .button.primary, button.primary {
      background: var(--accent);
      border-color: var(--accent);
      color: #fff;
    }
    .button.primary:hover, button.primary:hover { background: var(--accent-dark); }
    .button.danger, button.danger {
      border-color: #d9b8b8;
      color: var(--danger);
    }
    h1 {
      margin: 0 0 16px;
      font-size: 24px;
      letter-spacing: 0;
    }
    h2 {
      margin: 28px 0 12px;
      font-size: 17px;
      letter-spacing: 0;
    }
    .flash {
      border: 1px solid #c9d8ce;
      background: #eff8f2;
      padding: 10px 12px;
      border-radius: 6px;
      margin-bottom: 14px;
    }
    .stats {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
      gap: 12px;
    }
    .stat, .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
    }
    .stat { padding: 14px; }
    .stat span {
      display: block;
      color: var(--muted);
      font-size: 12px;
    }
    .stat strong {
      display: block;
      margin-top: 4px;
      font-size: 22px;
    }
    .panel { overflow: hidden; }
    .panel-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      padding: 14px 16px;
      border-bottom: 1px solid var(--line);
    }
    .panel-body { padding: 16px; }
    table {
      width: 100%;
      border-collapse: collapse;
      background: var(--panel);
    }
    th, td {
      border-bottom: 1px solid var(--line);
      padding: 10px 12px;
      text-align: left;
      vertical-align: top;
    }
    th {
      color: var(--muted);
      font-weight: 600;
      font-size: 12px;
      text-transform: uppercase;
    }
    .status {
      display: inline-block;
      border: 1px solid var(--line);
      border-radius: 999px;
      padding: 3px 8px;
      font-size: 12px;
      background: #f8fafb;
    }
    .muted { color: var(--muted); }
    .prewrap { white-space: pre-wrap; }
    .actions {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
    }
    form.grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 14px;
    }
    label {
      display: grid;
      gap: 6px;
      font-weight: 600;
    }
    input, textarea, select {
      width: 100%;
      min-height: 38px;
      padding: 8px 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      font: inherit;
    }
    textarea { min-height: 110px; resize: vertical; }
    .span-2 { grid-column: span 2; }
    .checkbox {
      display: flex;
      align-items: center;
      gap: 8px;
      font-weight: 600;
    }
    .checkbox input { width: auto; min-height: auto; }
    .followup {
      border-top: 1px solid var(--line);
      padding: 12px 0;
    }
    .followup:first-child { border-top: 0; padding-top: 0; }
    .chat-thread {
      display: flex;
      flex-direction: column;
      gap: 10px;
      max-height: 460px;
      overflow-y: auto;
      padding: 16px;
      background: #f9fbfc;
    }
    .bubble {
      max-width: min(720px, 86%);
      padding: 10px 12px;
      border: 1px solid var(--line);
      border-radius: 8px;
      background: #fff;
    }
    .bubble.admin {
      align-self: flex-end;
      background: #eaf5f1;
      border-color: #c6ded5;
    }
    .bubble.customer { align-self: flex-start; }
    .bubble .meta {
      display: block;
      margin-bottom: 4px;
      color: var(--muted);
      font-size: 12px;
    }
    .messenger {
      display: grid;
      grid-template-columns: minmax(260px, 360px) minmax(0, 1fr);
      min-height: 620px;
    }
    .conversation-list {
      border-right: 1px solid var(--line);
      background: #fbfcfd;
    }
    .conversation-link {
      display: block;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      text-decoration: none;
    }
    .conversation-link.active { background: #eaf5f1; }
    .conversation-link strong,
    .conversation-link span {
      display: block;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .conversation-pane {
      display: grid;
      grid-template-rows: auto minmax(240px, 1fr) auto;
      min-width: 0;
    }
    @media (max-width: 720px) {
      .bar { align-items: flex-start; flex-direction: column; }
      form.grid { grid-template-columns: 1fr; }
      .span-2 { grid-column: span 1; }
      table { display: block; overflow-x: auto; }
      .messenger { grid-template-columns: 1fr; }
      .conversation-list { border-right: 0; border-bottom: 1px solid var(--line); }
    }
  </style>
</head>
<body>
  <header>
    <div class="bar">
      <div class="brand">Store Dashboard</div>
      {% if authed %}
      <nav>
        <a class="navlink" href="{{ url_for('dashboard') }}">Dashboard</a>
        <a class="navlink" href="{{ url_for('users') }}">Users & Messenger</a>
        <a class="navlink" href="{{ url_for('products') }}">Products</a>
        <a class="navlink" href="{{ url_for('payments') }}">Payments</a>
        <a class="navlink" href="{{ url_for('engagement') }}">Engagement</a>
        <a class="navlink" href="{{ url_for('broadcast') }}">Broadcast</a>
        <a class="navlink" href="{{ url_for('orders') }}">Orders</a>
        <a class="navlink" href="{{ url_for('logout') }}">Logout</a>
      </nav>
      {% endif %}
    </div>
  </header>
  <main>
    {% with messages = get_flashed_messages() %}
      {% if messages %}
        {% for message in messages %}
          <div class="flash">{{ message }}</div>
        {% endfor %}
      {% endif %}
    {% endwith %}
    {{ content|safe }}
  </main>
  {% if auto_refresh_seconds %}
  <script>
    (() => {
      const intervalMs = {{ auto_refresh_seconds }} * 1000;
      const editableTags = new Set(["INPUT", "TEXTAREA", "SELECT"]);
      const isAdminTyping = () => {
        const active = document.activeElement;
        if (active && editableTags.has(active.tagName)) return true;
        return Array.from(document.querySelectorAll("input, textarea, select")).some((field) => {
          if (field.type === "hidden") return false;
          return field.value !== field.defaultValue;
        });
      };

      document.querySelectorAll(".chat-thread").forEach((thread) => {
        thread.scrollTop = thread.scrollHeight;
      });

      window.setInterval(() => {
        if (!isAdminTyping()) window.location.reload();
      }, intervalMs);
    })();
  </script>
  {% endif %}
</body>
</html>
"""


def render_page(title: str, content_template: str, auto_refresh_seconds: int = 0, **context):
    content = render_template_string(content_template, **context)
    return render_template_string(
        BASE_TEMPLATE,
        title=title,
        content=content,
        authed=session.get("dashboard_authed"),
        auto_refresh_seconds=auto_refresh_seconds,
    )


@app.route("/telegram/webhook/<path_secret>", methods=["POST"])
def telegram_webhook(path_secret: str):
    expected_path_secret = TELEGRAM_WEBHOOK_PATH_SECRET or (BOT_TOKEN.split(":", 1)[0] if BOT_TOKEN else "")
    if expected_path_secret and not hmac.compare_digest(path_secret, expected_path_secret):
        abort(404)
    if TELEGRAM_WEBHOOK_SECRET:
        request_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(request_secret, TELEGRAM_WEBHOOK_SECRET):
            abort(403)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        abort(400)
    run_telegram_webhook(payload)
    return "ok"


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        password = request.form.get("password", "")
        if hmac.compare_digest(password, DASHBOARD_PASSWORD):
            session["dashboard_authed"] = True
            return redirect(request.args.get("next") or url_for("dashboard"))
        flash("Invalid dashboard password.")
    return render_page(
        "Login",
        """
        <h1>Login</h1>
        <div class="panel">
          <div class="panel-body">
            <form method="post" class="grid">
              <label class="span-2">Dashboard password
                <input type="password" name="password" required autofocus>
              </label>
              <div class="actions span-2">
                <button class="primary" type="submit">Login</button>
              </div>
            </form>
          </div>
        </div>
        """,
    )


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    data = load_data()
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    scoped = data_with_orders(data, filtered_orders(data, date_from, date_to))
    stats = dashboard_stats(scoped)
    top_products = paginate(top_selling_products(scoped, limit=100), page_number("products_page"), per_page=5)
    top_customer_rows = paginate(top_customers(scoped, limit=100), page_number("customers_page"), per_page=5)
    recent_orders = paginate(
        sorted(scoped["orders"].values(), key=lambda order: order.get("created_at", ""), reverse=True),
        page_number("orders_page"),
        per_page=5,
    )
    low_stock = [
        product
        for product in data["products"].values()
        if product.get("active", True) and int(product.get("stock", 0)) <= 5
    ]
    low_stock = paginate(low_stock, page_number("stock_page"), per_page=5)
    return render_page(
        "Dashboard",
        """
        <h1>Dashboard</h1>
        <div class="panel" style="margin-bottom:16px">
          <div class="panel-body">
            <form method="get" class="grid">
              <label>From date
                <input type="date" name="date_from" value="{{ filters.date_from }}">
              </label>
              <label>To date
                <input type="date" name="date_to" value="{{ filters.date_to }}">
              </label>
              <div class="actions span-2">
                <button class="primary" type="submit">Apply Date Filter</button>
                <a class="button" href="{{ url_for('dashboard') }}">Clear</a>
              </div>
            </form>
          </div>
        </div>
        <section class="stats">
          <div class="stat"><span>Products</span><strong>{{ stats.products }}</strong></div>
          <div class="stat"><span>Active Products</span><strong>{{ stats.active_products }}</strong></div>
          <div class="stat"><span>Stock Available</span><strong>{{ stats.stock }}</strong></div>
          <div class="stat"><span>Low Stock</span><strong>{{ stats.low_stock }}</strong></div>
          <div class="stat"><span>Pending Orders</span><strong>{{ stats.pending }}</strong></div>
          <div class="stat"><span>Processing</span><strong>{{ stats.processing }}</strong></div>
          <div class="stat"><span>Completed</span><strong>{{ stats.completed }}</strong></div>
          <div class="stat"><span>Known Users</span><strong>{{ stats.users }}</strong></div>
          <div class="stat"><span>No Purchase Users</span><strong>{{ stats.non_buyers }}</strong></div>
          <div class="stat"><span>No Purchase Rate</span><strong>{{ stats.churn_rate }}%</strong></div>
          <div class="stat"><span>Average Order Value</span><strong>{{ money(stats.average_order_value) }}</strong></div>
          <div class="stat"><span>Lifetime Value</span><strong>{{ money(stats.lifetime_value) }}</strong></div>
          <div class="stat"><span>Open Value</span><strong>{{ money(stats.open_value) }}</strong></div>
        </section>

        <h2>Quick Actions</h2>
        <div class="actions">
          <a class="button primary" href="{{ url_for('new_product') }}">Add Product</a>
          <a class="button" href="{{ url_for('users') }}">Users & Messenger</a>
          <a class="button" href="{{ url_for('products') }}">Manage Products</a>
          <a class="button" href="{{ url_for('payments') }}">Payment Methods</a>
          <a class="button" href="{{ url_for('engagement') }}">Bot Engagement</a>
          <a class="button" href="{{ url_for('broadcast') }}">Bulk Message</a>
          <a class="button" href="{{ url_for('orders') }}">Process Orders</a>
        </div>

        <h2>Top Selling Products</h2>
        <div class="panel">
          <table>
            <thead><tr><th>Product</th><th>Units Sold</th><th>Orders</th><th>Revenue</th></tr></thead>
            <tbody>
              {% for product in top_products["items"] %}
              <tr>
                <td>{{ product.name }}</td>
                <td>{{ product.qty }}</td>
                <td>{{ product.orders }}</td>
                <td>{{ money(product.revenue) }}</td>
              </tr>
              {% else %}
              <tr><td colspan="4" class="muted">No sales yet.</td></tr>
              {% endfor %}
            </tbody>
          </table>
          {{ pager("products_page", top_products)|safe }}
        </div>

        <h2>Top Customers</h2>
        <div class="panel">
          <table>
            <thead><tr><th>Customer</th><th>Orders</th><th>Completed</th><th>Lifetime Value</th><th>Last Order</th><th></th></tr></thead>
            <tbody>
              {% for customer in top_customers["items"] %}
              <tr>
                <td>{{ customer.username or customer.full_name or customer.user_id }}</td>
                <td>{{ customer.orders }}</td>
                <td>{{ customer.completed_orders }}</td>
                <td>{{ money(customer.total_value) }}</td>
                <td>{{ customer.last_order_at or "-" }}</td>
                <td><a href="{{ url_for('user_detail', user_id=customer.user_id) }}">Open</a></td>
              </tr>
              {% else %}
              <tr><td colspan="6" class="muted">No buying customers yet.</td></tr>
              {% endfor %}
            </tbody>
          </table>
          {{ pager("customers_page", top_customers)|safe }}
        </div>

        <h2>Recent Orders</h2>
        <div class="panel">
          <table>
            <thead><tr><th>Order</th><th>Customer</th><th>Status</th><th>Total</th><th></th></tr></thead>
            <tbody>
              {% for order in recent_orders["items"] %}
              <tr>
                <td>{{ order.id }}</td>
                <td>{{ order.username or order.user_id }}</td>
                <td><span class="status">{{ statuses.get(order.status, order.status) }}</span></td>
                <td>{{ money(order.total_credits) }}</td>
                <td><a href="{{ url_for('order_detail', order_id=order.id) }}">Open</a></td>
              </tr>
              {% else %}
              <tr><td colspan="5" class="muted">No orders yet.</td></tr>
              {% endfor %}
            </tbody>
          </table>
          {{ pager("orders_page", recent_orders)|safe }}
        </div>

        <h2>Low Stock</h2>
        <div class="panel">
          <table>
            <thead><tr><th>Product</th><th>Stock</th><th>Status</th><th></th></tr></thead>
            <tbody>
              {% for product in low_stock["items"] %}
              <tr>
                <td>{{ product.name }}</td>
                <td>{{ product.stock }}</td>
                <td><span class="status">{{ "Active" if product.active else "Inactive" }}</span></td>
                <td><a href="{{ url_for('edit_product', product_id=product.id) }}">Edit</a></td>
              </tr>
              {% else %}
              <tr><td colspan="4" class="muted">No low-stock active products.</td></tr>
              {% endfor %}
            </tbody>
          </table>
          {{ pager("stock_page", low_stock)|safe }}
        </div>
        """,
        stats=stats,
        top_products=top_products,
        top_customers=top_customer_rows,
        recent_orders=recent_orders,
        low_stock=low_stock,
        statuses=ORDER_STATUSES,
        money=money,
        filters={"date_from": date_from, "date_to": date_to},
        pager=pager,
    )


@app.route("/users", methods=["GET", "POST"])
@login_required
def users():
    data = load_data()

    if request.method == "POST":
        user_id = int(request.form.get("user_id", "0"))
        message = request.form.get("message", "").strip()
        media_url = request.form.get("media_url", "").strip()
        media_type = request.form.get("media_type", "animation")
        media_url = cloudinary_media_url(media_url, media_type)
        if not user_id or not message:
            flash("Choose a customer and enter a message.")
            return redirect(url_for("users", user_id=user_id) if user_id else url_for("users"))
        sent = send_telegram_media(user_id, media_type, media_url, message) if media_url else send_telegram_message(user_id, message)
        user = data.setdefault("users", {}).setdefault(str(user_id), {"id": user_id})
        user.setdefault("messages", []).append(
            {
                "from": "admin",
                "message": message,
                "media_url": media_url,
                "media_type": media_type if media_url else "",
                "created_at": now_iso(),
                "sent": sent,
                "read": True,
                "context": "Users inbox",
            }
        )
        save_data(data)
        flash("Message sent." if sent else "Message saved, but Telegram send failed.")
        return redirect(url_for("users", user_id=user_id))

    filters = {
        "q": request.args.get("q", ""),
        "status": request.args.get("status", ""),
        "date_from": request.args.get("date_from", ""),
        "date_to": request.args.get("date_to", ""),
        "sort": request.args.get("sort", "conversation_recent"),
        "min_aov": request.args.get("min_aov", ""),
        "max_aov": request.args.get("max_aov", ""),
        "min_ltv": request.args.get("min_ltv", ""),
        "max_ltv": request.args.get("max_ltv", ""),
        "conversation_q": request.args.get("conversation_q", ""),
    }
    filter_args = request.args.copy()
    if not filter_args.get("sort") or filter_args.get("sort") == "conversation_recent":
        filter_args = filter_args.copy()
        filter_args["sort"] = "lifetime_desc"
    customers = apply_customer_filters(customer_metrics(data), filter_args)

    def latest_activity(customer: dict[str, Any]) -> str:
        history = user_message_history(data, int(customer["user_id"]))
        latest_message = history[-1].get("created_at", "") if history else ""
        return max(latest_message, customer.get("last_order_at", ""))

    if filters["sort"] == "conversation_recent":
        customers = sorted(customers, key=latest_activity, reverse=True)

    customer_page = paginate(customers, page_number(), per_page=20)
    selected_user_id = request.args.get("user_id", "")
    if not selected_user_id and customer_page["items"]:
        selected_user_id = str(customer_page["items"][0]["user_id"])

    selected = None
    messages: list[dict[str, Any]] = []
    if selected_user_id and selected_user_id.isdigit():
        selected = next((customer for customer in customers if int(customer["user_id"]) == int(selected_user_id)), None)
        if selected:
            user_record = data.setdefault("users", {}).get(str(selected["user_id"]), {})
            changed = False
            for msg in user_record.get("messages", []):
                if msg.get("from") == "customer" and not msg.get("read"):
                    msg["read"] = True
                    changed = True
            if changed:
                save_data(data)
            messages = user_message_history(data, int(selected["user_id"]))
            conversation_query = filters["conversation_q"].strip().lower()
            if conversation_query:
                messages = [
                    item for item in messages
                    if conversation_query in str(item.get("message", "")).lower()
                    or conversation_query in str(item.get("context", "")).lower()
                    or conversation_query in str(item.get("media_url", "")).lower()
                ]

    def inbox_url(user_id: int) -> str:
        args = request.args.to_dict()
        args["user_id"] = str(user_id)
        return url_for("users", **args)

    return render_page(
        "Users & Messenger",
        """
        <h1>Users & Messenger</h1>
        <div class="panel messenger">
          <aside class="conversation-list">
            <div class="panel-body">
              <form method="get" class="grid" style="display:block">
                <label>Search users
                  <input name="q" value="{{ filters.q }}" placeholder="Name, username, or Telegram ID">
                </label>
                <label>Status
                  <select name="status">
                    <option value="">All users</option>
                    <option value="buyer" {% if filters.status == "buyer" %}selected{% endif %}>Buyers</option>
                    <option value="no_purchase" {% if filters.status == "no_purchase" %}selected{% endif %}>No purchase</option>
                    <option value="unread" {% if filters.status == "unread" %}selected{% endif %}>Unread</option>
                  </select>
                </label>
                <label>Sort
                  <select name="sort">
                    <option value="conversation_recent" {% if filters.sort == "conversation_recent" %}selected{% endif %}>Most recent conversation</option>
                    <option value="recent" {% if filters.sort == "recent" %}selected{% endif %}>Most recent order</option>
                    <option value="avg_order_desc" {% if filters.sort == "avg_order_desc" %}selected{% endif %}>Average order value</option>
                    <option value="lifetime_desc" {% if filters.sort == "lifetime_desc" %}selected{% endif %}>Lifetime value</option>
                    <option value="orders_desc" {% if filters.sort == "orders_desc" %}selected{% endif %}>Most orders</option>
                    <option value="unread_desc" {% if filters.sort == "unread_desc" %}selected{% endif %}>Unread first</option>
                    <option value="name" {% if filters.sort == "name" %}selected{% endif %}>Name</option>
                  </select>
                </label>
                <label>Last order from
                  <input type="date" name="date_from" value="{{ filters.date_from }}">
                </label>
                <label>Last order to
                  <input type="date" name="date_to" value="{{ filters.date_to }}">
                </label>
                <label>Min avg order USD
                  <input type="number" min="0" name="min_aov" value="{{ filters.min_aov }}">
                </label>
                <label>Max avg order USD
                  <input type="number" min="0" name="max_aov" value="{{ filters.max_aov }}">
                </label>
                <label>Min LTV USD
                  <input type="number" min="0" name="min_ltv" value="{{ filters.min_ltv }}">
                </label>
                <label>Max LTV USD
                  <input type="number" min="0" name="max_ltv" value="{{ filters.max_ltv }}">
                </label>
                <label>Search conversation
                  <input name="conversation_q" value="{{ filters.conversation_q }}" placeholder="Message text">
                </label>
                <div class="actions">
                  <button class="primary" type="submit">Apply</button>
                  <a class="button" href="{{ url_for('users') }}">Clear</a>
                </div>
              </form>
            </div>
            {% for customer in users["items"] %}
            <a class="conversation-link {% if selected and customer.user_id == selected.user_id %}active{% endif %}" href="{{ inbox_url(customer.user_id) }}">
              <strong>{{ customer.username or customer.full_name or customer.user_id }}</strong>
              <span class="muted">{{ customer.unread_count }} unread | {{ customer.orders }} orders | {{ money(customer.average_order_value) }} AOV</span>
              <span class="muted">{{ money(customer.total_value) }} LTV | {{ customer.last_order_at or "No orders yet" }}</span>
            </a>
            {% else %}
            <div class="panel-body muted">No customers matched.</div>
            {% endfor %}
            {{ pager("page", users)|safe }}
          </aside>
          <section class="conversation-pane">
            {% if selected %}
            <div class="panel-body" style="border-bottom:1px solid var(--line)">
              <strong>{{ selected.username or selected.full_name or selected.user_id }}</strong>
              <span class="muted"> | {{ selected.orders }} orders | {{ money(selected.average_order_value) }} AOV | {{ money(selected.total_value) }} LTV</span>
              <div class="actions" style="margin-top:8px">
                <a class="button" href="{{ url_for('user_detail', user_id=selected.user_id) }}">Open Profile</a>
              </div>
            </div>
            <div class="chat-thread" id="user-chat-thread" data-user-id="{{ selected.user_id }}">
              {% for item in messages %}
              <div class="bubble {{ 'admin' if item.get('from') == 'admin' else 'customer' }}">
                <span class="meta">{{ (item.get("from") or "customer").title() }} | {{ item.created_at }}{% if item.context %} | {{ item.context }}{% endif %}</span>
                <div class="prewrap">{{ item.message }}</div>
                {% if item.media_url %}
                <div><a href="{{ item.media_url }}" target="_blank" rel="noopener">{{ item.media_type or "media" }}</a></div>
                {% endif %}
              </div>
              {% else %}
              <p class="muted">No messages yet.</p>
              {% endfor %}
            </div>
            <div class="panel-body" style="border-top:1px solid var(--line)">
              <form method="post" class="grid">
                <input type="hidden" name="user_id" value="{{ selected.user_id }}">
                <label class="span-2">Message
                  <textarea name="message" placeholder="Message this customer through the bot" required></textarea>
                </label>
                <label>Media type
                  <select name="media_type">
                    <option value="animation">GIF / Animation</option>
                    <option value="photo">Photo</option>
                    <option value="video">Video</option>
                    <option value="document">Document</option>
                  </select>
                </label>
                <label>Optional media URL
                  <input name="media_url" placeholder="https://example.com/media">
                </label>
                <div class="actions span-2">
                  <button class="primary" type="submit">Send Message</button>
                </div>
              </form>
            </div>
            {% else %}
            <div class="panel-body muted">Select a customer to start messaging.</div>
            {% endif %}
          </section>
        </div>
        {% if selected %}
        <script>
          (() => {
            const thread = document.getElementById("user-chat-thread");
            if (!thread) return;
            const userId = thread.dataset.userId;
            let lastSignature = "";
            const nearBottom = () => thread.scrollHeight - thread.scrollTop - thread.clientHeight < 80;
            const buildBubble = (item) => {
              const bubble = document.createElement("div");
              bubble.className = `bubble ${item.from === "admin" ? "admin" : "customer"}`;

              const meta = document.createElement("span");
              meta.className = "meta";
              meta.textContent = `${item.from_label} | ${item.created_at}${item.context ? ` | ${item.context}` : ""}`;
              bubble.appendChild(meta);

              const body = document.createElement("div");
              body.className = "prewrap";
              body.textContent = item.message || "";
              bubble.appendChild(body);

              if (item.media_url) {
                const media = document.createElement("div");
                const link = document.createElement("a");
                link.href = item.media_url;
                link.target = "_blank";
                link.rel = "noopener";
                link.textContent = item.media_type || "media";
                media.appendChild(link);
                bubble.appendChild(media);
              }
              return bubble;
            };

            const refreshMessages = async () => {
              const response = await fetch(`/users/${userId}/messages.json`, {cache: "no-store"});
              if (!response.ok) return;
              const payload = await response.json();
              const signature = JSON.stringify(payload.messages.map((item) => [item.from, item.message, item.created_at, item.media_url]));
              if (signature === lastSignature) return;
              lastSignature = signature;
              const shouldStick = nearBottom();
              thread.replaceChildren(...payload.messages.map(buildBubble));
              if (shouldStick) thread.scrollTop = thread.scrollHeight;
            };

            thread.scrollTop = thread.scrollHeight;
            refreshMessages();
            window.setInterval(refreshMessages, 2000);
          })();
        </script>
        {% endif %}
        """,
        users=customer_page,
        selected=selected,
        messages=messages,
        filters=filters,
        inbox_url=inbox_url,
        money=money,
        pager=pager,
        auto_refresh_seconds=0,
    )


@app.route("/users/<int:user_id>/messages.json")
@login_required
def user_messages_json(user_id: int):
    data = load_data()
    user_record = data.setdefault("users", {}).get(str(user_id), {})
    changed = False
    for msg in user_record.get("messages", []):
        if msg.get("from") == "customer" and not msg.get("read"):
            msg["read"] = True
            changed = True
    if changed:
        save_data(data)
    messages = user_message_history(data, user_id)
    return jsonify(
        {
            "messages": [
                {
                    "from": item.get("from") or "customer",
                    "from_label": (item.get("from") or "customer").title(),
                    "message": item.get("message", ""),
                    "media_url": item.get("media_url", ""),
                    "media_type": item.get("media_type", ""),
                    "created_at": item.get("created_at", ""),
                    "context": item.get("context", ""),
                }
                for item in messages
            ]
        }
    )


@app.route("/users/<int:user_id>", methods=["GET", "POST"])
@login_required
def user_detail(user_id: int):
    data = load_data()
    if request.method == "POST":
        message = request.form.get("message", "").strip()
        media_url = request.form.get("media_url", "").strip()
        media_type = request.form.get("media_type", "animation")
        media_url = cloudinary_media_url(media_url, media_type)
        if not message:
            flash("Message cannot be empty.")
            return redirect(url_for("user_detail", user_id=user_id))
        if media_url:
            sent = send_telegram_media(user_id, media_type, media_url, message)
        else:
            sent = send_telegram_message(user_id, message)
        user = data.setdefault("users", {}).setdefault(str(user_id), {"id": user_id})
        user.setdefault("messages", []).append(
            {
                "from": "admin",
                "message": message,
                "media_url": media_url,
                "media_type": media_type if media_url else "",
                "created_at": now_iso(),
                "sent": sent,
                "context": "Direct message",
            }
        )
        save_data(data)
        flash("Message sent to customer." if sent else "Message failed. The user may not have started the bot, or BOT_TOKEN is invalid.")
        return redirect(url_for("user_detail", user_id=user_id))

    metrics = next((item for item in customer_metrics(data) if int(item["user_id"]) == user_id), None)
    if not metrics:
        flash("User not found.")
        return redirect(url_for("users"))
    user_record = data.setdefault("users", {}).get(str(user_id), {})
    changed = False
    for msg in user_record.get("messages", []):
        if msg.get("from") == "customer" and not msg.get("read"):
            msg["read"] = True
            changed = True
    if changed:
        save_data(data)
    orders = user_orders(data, user_id)
    messages = user_message_history(data, user_id)
    vouches = data.get("users", {}).get(str(user_id), {}).get("vouches", [])
    conversation_query = request.args.get("conversation_q", "").strip().lower()
    if conversation_query:
        messages = [
            item for item in messages
            if conversation_query in str(item.get("message", "")).lower()
            or conversation_query in str(item.get("context", "")).lower()
        ]
    return render_page(
        "User",
        """
        <h1>User {{ user.username or user.full_name or user.user_id }}</h1>
        <section class="stats">
          <div class="stat"><span>Orders</span><strong>{{ user.orders }}</strong></div>
          <div class="stat"><span>Completed</span><strong>{{ user.completed_orders }}</strong></div>
          <div class="stat"><span>Lifetime Value</span><strong>{{ money(user.total_value) }}</strong></div>
          <div class="stat"><span>Status</span><strong style="font-size:18px">{{ "Buyer" if user.has_bought else "No purchase" }}</strong></div>
          <div class="stat"><span>Auto Updates</span><strong style="font-size:18px">{{ "On" if user.auto_updates_enabled else "Off" }}</strong></div>
          <div class="stat"><span>Language</span><strong style="font-size:18px">{{ languages.get(user.language or "en", "English") }}</strong></div>
        </section>

        <h2>Customer Settings</h2>
        <div class="panel">
          <div class="panel-body">
            <form method="post" action="{{ url_for('toggle_user_updates', user_id=user.user_id) }}">
              <button type="submit">{{ "Stop automatic updates" if user.auto_updates_enabled else "Enable automatic updates" }}</button>
            </form>
            <p class="muted" style="margin-bottom:0">This only controls automatic restock, proof, and abandoned-cart notifications. Admin messenger replies and manual bulk messages can still be sent.</p>
          </div>
        </div>

        <h2>Messenger</h2>
        <div class="panel">
          <div class="panel-body" style="border-bottom:1px solid var(--line)">
            <form method="get" class="grid">
              <label class="span-2">Search conversation
                <input name="conversation_q" value="{{ conversation_q }}" placeholder="Search messages or order context">
              </label>
              <div class="actions span-2">
                <button type="submit">Search</button>
                <a class="button" href="{{ url_for('user_detail', user_id=user.user_id) }}">Clear</a>
              </div>
            </form>
          </div>
          <div class="chat-thread">
            {% for item in messages %}
            <div class="bubble {{ 'admin' if item.get('from') == 'admin' else 'customer' }}">
              <span class="meta">{{ (item.get("from") or "customer").title() }} · {{ item.created_at }}{% if item.context %} · {{ item.context }}{% endif %}</span>
              <div class="prewrap">{{ item.message }}</div>
              {% if item.media_url %}
              <div><a href="{{ item.media_url }}" target="_blank" rel="noopener">{{ item.media_type or "media" }}</a></div>
              {% endif %}
            </div>
            {% else %}
            <p class="muted">No messages yet.</p>
            {% endfor %}
          </div>
          <div class="panel-body" style="border-top:1px solid var(--line)">
            <form method="post" class="grid">
              <label class="span-2">Message
                <textarea name="message" placeholder="Message this customer through the bot" required></textarea>
              </label>
              <label>Media type
                <select name="media_type">
                  <option value="animation">GIF / Animation</option>
                  <option value="photo">Photo</option>
                  <option value="video">Video</option>
                  <option value="document">Document</option>
                </select>
              </label>
              <label>Optional media URL
                <input name="media_url" placeholder="https://example.com/media">
              </label>
              <div class="actions span-2">
                <button class="primary" type="submit">Send Message</button>
              </div>
            </form>
          </div>
        </div>

        <h2>Vouches</h2>
        <div class="panel">
          <div class="panel-body">
            {% for item in vouches %}
            <div class="followup">
              <strong>Order {{ item.order_id }}</strong>
              <span class="muted">{{ item.created_at }}</span>
              <div class="prewrap">{{ item.message }}</div>
              <div class="muted">Warranty active: {{ "Yes" if item.warranty_activated else "No" }}</div>
            </div>
            {% else %}
            <p class="muted">No customer vouches yet.</p>
            {% endfor %}
          </div>
        </div>

        <h2>Order History</h2>
        <div class="panel">
          <table>
            <thead><tr><th>Order</th><th>Status</th><th>Total</th><th>Created</th><th></th></tr></thead>
            <tbody>
              {% for order in orders %}
              <tr>
                <td>{{ order.id }}</td>
                <td><span class="status">{{ statuses.get(order.status, order.status) }}</span></td>
                <td>{{ money(order.total_credits) }}</td>
                <td>{{ order.created_at }}</td>
                <td><a href="{{ url_for('order_detail', order_id=order.id) }}">Open</a></td>
              </tr>
              {% else %}
              <tr><td colspan="5" class="muted">No orders yet.</td></tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
        """,
        user=metrics,
        orders=orders,
        messages=messages,
        vouches=vouches,
        conversation_q=request.args.get("conversation_q", ""),
        statuses=ORDER_STATUSES,
        money=money,
        languages=LANGUAGES,
        auto_refresh_seconds=5,
    )


@app.route("/users/<int:user_id>/toggle-updates", methods=["POST"])
@login_required
def toggle_user_updates(user_id: int):
    data = load_data()
    user = data.setdefault("users", {}).setdefault(
        str(user_id),
        {
            "id": user_id,
            "username": None,
            "full_name": None,
            "updated_at": now_iso(),
            "auto_updates_enabled": True,
        },
    )
    user["auto_updates_enabled"] = not user.get("auto_updates_enabled", True)
    user["updated_at"] = now_iso()
    save_data(data)
    flash("Customer automatic update setting changed.")
    return redirect(url_for("user_detail", user_id=user_id))


@app.route("/messenger", methods=["GET", "POST"])
@login_required
def messenger():
    if request.method == "GET":
        return redirect(url_for("users", **request.args.to_dict()))

    data = load_data()
    if request.method == "POST":
        user_id = int(request.form.get("user_id", "0"))
        message = request.form.get("message", "").strip()
        media_url = request.form.get("media_url", "").strip()
        media_type = request.form.get("media_type", "animation")
        media_url = cloudinary_media_url(media_url, media_type)
        if not user_id or not message:
            flash("Choose a customer and enter a message.")
            return redirect(url_for("users", user_id=user_id) if user_id else url_for("users"))
        sent = send_telegram_media(user_id, media_type, media_url, message) if media_url else send_telegram_message(user_id, message)
        user = data.setdefault("users", {}).setdefault(str(user_id), {"id": user_id})
        user.setdefault("messages", []).append(
            {
                "from": "admin",
                "message": message,
                "media_url": media_url,
                "media_type": media_type if media_url else "",
                "created_at": now_iso(),
                "sent": sent,
                "read": True,
                "context": "Messenger",
            }
        )
        save_data(data)
        flash("Message sent." if sent else "Message saved, but Telegram send failed.")
        return redirect(url_for("users", user_id=user_id))

    query = request.args.get("q", "").strip().lower()
    customers = customer_metrics(data)
    if query:
        customers = [
            customer for customer in customers
            if query in str(customer["user_id"]).lower()
            or query in str(customer.get("username") or "").lower()
            or query in str(customer.get("full_name") or "").lower()
        ]

    def latest_activity(customer: dict[str, Any]) -> str:
        messages = user_message_history(data, int(customer["user_id"]))
        latest_message = messages[-1].get("created_at", "") if messages else ""
        return max(latest_message, customer.get("last_order_at", ""))

    customers = sorted(customers, key=latest_activity, reverse=True)
    selected_user_id = request.args.get("user_id", "")
    if not selected_user_id and customers:
        selected_user_id = str(customers[0]["user_id"])

    selected = None
    messages: list[dict[str, Any]] = []
    if selected_user_id and selected_user_id.isdigit():
        selected = next((customer for customer in customers if int(customer["user_id"]) == int(selected_user_id)), None)
        if not selected:
            selected = next((customer for customer in customer_metrics(data) if int(customer["user_id"]) == int(selected_user_id)), None)
        if selected:
            user_record = data.setdefault("users", {}).get(str(selected["user_id"]), {})
            changed = False
            for msg in user_record.get("messages", []):
                if msg.get("from") == "customer" and not msg.get("read"):
                    msg["read"] = True
                    changed = True
            if changed:
                save_data(data)
            messages = user_message_history(data, int(selected["user_id"]))

    return render_page(
        "Messenger",
        """
        <h1>Messenger</h1>
        <div class="panel messenger">
          <aside class="conversation-list">
            <div class="panel-body">
              <form method="get">
                <input name="q" value="{{ q }}" placeholder="Search customers">
              </form>
            </div>
            {% for customer in customers %}
            <a class="conversation-link {% if selected and customer.user_id == selected.user_id %}active{% endif %}" href="{{ url_for('messenger', user_id=customer.user_id, q=q) }}">
              <strong>{{ customer.username or customer.full_name or customer.user_id }}</strong>
              <span class="muted">{{ customer.unread_count }} unread · {{ money(customer.total_value) }} LTV</span>
              <span class="muted">{{ customer.last_order_at or "No orders yet" }}</span>
            </a>
            {% else %}
            <div class="panel-body muted">No customers found.</div>
            {% endfor %}
          </aside>
          <section class="conversation-pane">
            {% if selected %}
            <div class="panel-body" style="border-bottom:1px solid var(--line)">
              <strong>{{ selected.username or selected.full_name or selected.user_id }}</strong>
              <span class="muted"> · {{ selected.orders }} orders · {{ money(selected.total_value) }} LTV</span>
            </div>
            <div class="chat-thread">
              {% for item in messages %}
              <div class="bubble {{ 'admin' if item.get('from') == 'admin' else 'customer' }}">
                <span class="meta">{{ (item.get("from") or "customer").title() }} · {{ item.created_at }}{% if item.context %} · {{ item.context }}{% endif %}</span>
                <div class="prewrap">{{ item.message }}</div>
                {% if item.media_url %}
                <div><a href="{{ item.media_url }}" target="_blank" rel="noopener">{{ item.media_type or "media" }}</a></div>
                {% endif %}
              </div>
              {% else %}
              <p class="muted">No messages yet.</p>
              {% endfor %}
            </div>
            <div class="panel-body" style="border-top:1px solid var(--line)">
              <form method="post" class="grid">
                <input type="hidden" name="user_id" value="{{ selected.user_id }}">
                <label class="span-2">Message
                  <textarea name="message" placeholder="Message this customer through the bot" required></textarea>
                </label>
                <label>Media type
                  <select name="media_type">
                    <option value="animation">GIF / Animation</option>
                    <option value="photo">Photo</option>
                    <option value="video">Video</option>
                    <option value="document">Document</option>
                  </select>
                </label>
                <label>Optional media URL
                  <input name="media_url" placeholder="https://example.com/media">
                </label>
                <div class="actions span-2">
                  <button class="primary" type="submit">Send Message</button>
                  <a class="button" href="{{ url_for('user_detail', user_id=selected.user_id) }}">Open User Profile</a>
                </div>
              </form>
            </div>
            {% else %}
            <div class="panel-body muted">Select a customer to start messaging.</div>
            {% endif %}
          </section>
        </div>
        """,
        customers=customers,
        selected=selected,
        messages=messages,
        q=request.args.get("q", ""),
        money=money,
        auto_refresh_seconds=5,
    )


@app.route("/products")
@login_required
def products():
    data = load_data()
    product_list = sorted(
        data["products"].values(),
        key=lambda product: (
            int(product.get("highlight_rank", 0) or 0) if int(product.get("highlight_rank", 0) or 0) in (1, 2) else 99,
            product.get("name", "").lower(),
            product.get("created_at", ""),
        ),
    )
    return render_page(
        "Products",
        """
        <div class="panel-head">
          <h1 style="margin:0">Products</h1>
          <a class="button primary" href="{{ url_for('new_product') }}">Add Product</a>
        </div>
        <div class="panel">
          <table>
            <thead>
              <tr><th>Name</th><th>ID</th><th>Featured</th><th>Price</th><th>Stock</th><th>Warranty</th><th>Subscription</th><th>Status</th><th></th></tr>
            </thead>
            <tbody>
              {% for product in products %}
              <tr>
                <td>{{ product.name }}</td>
                <td><code>{{ product.id }}</code></td>
                <td>{% if product.get("highlight_rank", 0) in [1, 2] %}<span class="status">Top {{ product.highlight_rank }}</span>{% else %}<span class="muted">No</span>{% endif %}</td>
                <td>{{ money(product.price_credits) }}</td>
                <td>{{ product.stock }}</td>
                <td>{{ product.warranty_days }} days</td>
                <td>{{ product.subscription_days }} days</td>
                <td><span class="status">{{ "Active" if product.active else "Inactive" }}</span></td>
                <td class="actions">
                  <a href="{{ url_for('edit_product', product_id=product.id) }}">Edit</a>
                  <form method="post" action="{{ url_for('toggle_product', product_id=product.id) }}">
                    <button type="submit">{{ "Disable" if product.active else "Enable" }}</button>
                  </form>
                  <form method="post" action="{{ url_for('delete_product', product_id=product.id) }}" onsubmit="return confirm('Delete this product from the store? Existing orders keep their item snapshot, but the product will be removed from catalog and upsells.');">
                    <button class="danger" type="submit">Delete</button>
                  </form>
                </td>
              </tr>
              {% else %}
              <tr><td colspan="9" class="muted">No products yet.</td></tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
        """,
        products=product_list,
        money=money,
    )


PRODUCT_FORM = """
<h1>{{ heading }}</h1>
<div class="panel">
  <div class="panel-body">
    <form method="post" class="grid">
      <label>Name
        <input name="name" value="{{ product.name or '' }}" required>
      </label>
      <label>USD price
        <input type="number" min="0" step="1" name="price_credits" value="{{ product.price_credits or 0 }}" required>
      </label>
      <label>Stock left
        <input type="number" min="0" step="1" name="stock" value="{{ product.stock or 0 }}" required>
      </label>
      <label>Warranty days
        <input type="number" min="0" step="1" name="warranty_days" value="{{ product.warranty_days or 0 }}" required>
      </label>
      <label>Subscription days
        <input type="number" min="0" step="1" name="subscription_days" value="{{ product.subscription_days or 0 }}" required>
      </label>
      <label>Store highlight
        <select name="highlight_rank">
          <option value="0" {% if (product.get("highlight_rank", 0) or 0)|int == 0 %}selected{% endif %}>Not featured</option>
          <option value="1" {% if (product.get("highlight_rank", 0) or 0)|int == 1 %}selected{% endif %}>Top 1 featured</option>
          <option value="2" {% if (product.get("highlight_rank", 0) or 0)|int == 2 %}selected{% endif %}>Top 2 featured</option>
        </select>
        <small class="muted">Only one product can use each featured slot.</small>
      </label>
      <label>Upsell product IDs
        <input name="upsell_ids" value="{{ (product.upsell_ids or [])|join(', ') }}" placeholder="ABC12345, XYZ67890">
      </label>
      <label class="span-2">Description
        <textarea name="description">{{ product.description or '' }}</textarea>
      </label>
      <label class="checkbox span-2">
        <input type="checkbox" name="active" {% if product.active is not false %}checked{% endif %}>
        Active in store
      </label>
      <div class="actions span-2">
        <button class="primary" type="submit">Save Product</button>
        <a class="button" href="{{ url_for('products') }}">Cancel</a>
      </div>
    </form>
  </div>
</div>
"""


@app.route("/products/new", methods=["GET", "POST"])
@login_required
def new_product():
    product = {
        "active": True,
        "price_credits": 0,
        "stock": 0,
        "warranty_days": 0,
        "subscription_days": 0,
        "highlight_rank": 0,
        "upsell_ids": [],
    }
    if request.method == "POST":
        try:
            product = product_from_form()
        except ValueError as error:
            flash(str(error))
        else:
            product["created_at"] = now_iso()
            product["updated_at"] = now_iso()
            data = load_data()
            data["products"][product["id"]] = product
            enforce_highlight_slots(data, product["id"])
            save_data(data)
            maybe_notify_product_change(data, product, None)
            flash("Product created.")
            return redirect(url_for("products"))
    return render_page("Add Product", PRODUCT_FORM, heading="Add Product", product=product)


@app.route("/products/<product_id>/edit", methods=["GET", "POST"])
@login_required
def edit_product(product_id: str):
    data = load_data()
    product = data["products"].get(product_id)
    if not product:
        flash("Product not found.")
        return redirect(url_for("products"))
    if request.method == "POST":
        previous = product.copy()
        try:
            updated = product_from_form(product_id)
        except ValueError as error:
            flash(str(error))
        else:
            updated["created_at"] = product.get("created_at", now_iso())
            updated["updated_at"] = now_iso()
            data["products"][product_id] = updated
            enforce_highlight_slots(data, product_id)
            save_data(data)
            maybe_notify_product_change(data, updated, previous)
            flash("Product updated.")
            return redirect(url_for("products"))
    return render_page("Edit Product", PRODUCT_FORM, heading=f"Edit {product['name']}", product=product)


@app.route("/products/<product_id>/toggle", methods=["POST"])
@login_required
def toggle_product(product_id: str):
    data = load_data()
    product = data["products"].get(product_id)
    if product:
        previous = product.copy()
        product["active"] = not product.get("active", True)
        product["updated_at"] = now_iso()
        save_data(data)
        maybe_notify_product_change(data, product, previous)
        flash("Product status updated.")
    else:
        flash("Product not found.")
    return redirect(url_for("products"))


@app.route("/products/<product_id>/delete", methods=["POST"])
@login_required
def delete_product(product_id: str):
    data = load_data()
    product = data.get("products", {}).pop(product_id, None)
    if not product:
        flash("Product not found.")
        return redirect(url_for("products"))

    for other_product in data.get("products", {}).values():
        upsell_ids = other_product.get("upsell_ids", [])
        if product_id in upsell_ids:
            other_product["upsell_ids"] = [item for item in upsell_ids if item != product_id]
            other_product["updated_at"] = now_iso()
    for cart in data.get("carts", {}).values():
        cart.pop(product_id, None)

    save_data(data)
    flash(f"Deleted product {product.get('name', product_id)}.")
    return redirect(url_for("products"))


@app.route("/payments")
@login_required
def payments():
    data = load_data()
    methods = data["settings"].get("payment_methods", [])
    return render_page(
        "Payments",
        """
        <div class="panel-head">
          <h1 style="margin:0">Payment Methods</h1>
          <a class="button primary" href="{{ url_for('new_payment') }}">Add Payment Method</a>
        </div>
        <div class="panel" style="margin-bottom:16px">
          <div class="panel-body">
            <form method="post" action="{{ url_for('checkout_instructions') }}" class="grid">
              <label class="span-2">Payment instructions shown during checkout and payment
                <textarea name="checkout_instructions" placeholder="Example: Please double-check your cart before paying. Upload receipt after payment.">{{ checkout_instructions }}</textarea>
              </label>
              <label class="span-2">Wallet addresses
                <textarea name="wallet_addresses" placeholder="One wallet per line: Label | Network | Address | Optional note">{{ wallet_addresses }}</textarea>
              </label>
              <div class="actions span-2">
                <button class="primary" type="submit">Save Payment Settings</button>
              </div>
            </form>
          </div>
        </div>
        <div class="panel">
          <table>
            <thead>
              <tr><th>Name</th><th>Instructions</th><th>Status</th><th></th></tr>
            </thead>
            <tbody>
              {% for method in methods %}
              <tr>
                <td>{{ method.label }}</td>
                <td class="prewrap">{{ method.instructions }}</td>
                <td><span class="status">{{ "Active" if method.active else "Inactive" }}</span></td>
                <td class="actions">
                  <a href="{{ url_for('edit_payment', method_id=method.id) }}">Edit</a>
                  <form method="post" action="{{ url_for('toggle_payment', method_id=method.id) }}">
                    <button type="submit">{{ "Disable" if method.active else "Enable" }}</button>
                  </form>
                  <form method="post" action="{{ url_for('delete_payment', method_id=method.id) }}">
                    <button class="danger" type="submit">Delete</button>
                  </form>
                </td>
              </tr>
              {% else %}
              <tr><td colspan="4" class="muted">No payment methods yet.</td></tr>
              {% endfor %}
            </tbody>
          </table>
        </div>
        """,
        methods=methods,
        checkout_instructions=data["settings"].get("checkout_instructions", ""),
        wallet_addresses=wallet_addresses_text(data["settings"].get("wallet_addresses", [])),
    )


@app.route("/payments/checkout-instructions", methods=["POST"])
@login_required
def checkout_instructions():
    data = load_data()
    data["settings"]["checkout_instructions"] = request.form.get("checkout_instructions", "").strip()
    data["settings"]["wallet_addresses"] = parse_wallet_addresses(request.form.get("wallet_addresses", ""))
    save_data(data)
    flash("Payment settings updated.")
    return redirect(url_for("payments"))


PAYMENT_FORM = """
<h1>{{ heading }}</h1>
<div class="panel">
  <div class="panel-body">
    <form method="post" class="grid">
      <label class="span-2">Payment method name
        <input name="label" value="{{ method.label or '' }}" placeholder="Crypto - USDT TRC20" required>
      </label>
      <label class="span-2">Customer payment instructions
        <textarea name="instructions" placeholder="Send USDT TRC20 to: Txxxxxxxxxxxxxxxxxxxxxxxx&#10;After payment, upload the receipt in Telegram." required>{{ method.instructions or '' }}</textarea>
      </label>
      <label class="checkbox span-2">
        <input type="checkbox" name="active" {% if method.active is not false %}checked{% endif %}>
        Active at checkout
      </label>
      <div class="actions span-2">
        <button class="primary" type="submit">Save Payment Method</button>
        <a class="button" href="{{ url_for('payments') }}">Cancel</a>
      </div>
    </form>
  </div>
</div>
"""


@app.route("/payments/new", methods=["GET", "POST"])
@login_required
def new_payment():
    method = {"active": True, "label": "", "instructions": ""}
    if request.method == "POST":
        try:
            method = payment_method_from_form()
        except ValueError as error:
            flash(str(error))
        else:
            method["created_at"] = now_iso()
            method["updated_at"] = now_iso()
            data = load_data()
            data["settings"].setdefault("payment_methods", []).append(method)
            save_data(data)
            flash("Payment method created.")
            return redirect(url_for("payments"))
    return render_page("Add Payment Method", PAYMENT_FORM, heading="Add Payment Method", method=method)


@app.route("/payments/<method_id>/edit", methods=["GET", "POST"])
@login_required
def edit_payment(method_id: str):
    data = load_data()
    methods = data["settings"].setdefault("payment_methods", [])
    method = next((item for item in methods if item.get("id") == method_id), None)
    if not method:
        flash("Payment method not found.")
        return redirect(url_for("payments"))
    if request.method == "POST":
        try:
            updated = payment_method_from_form(method_id)
        except ValueError as error:
            flash(str(error))
        else:
            updated["created_at"] = method.get("created_at", now_iso())
            updated["updated_at"] = now_iso()
            methods[methods.index(method)] = updated
            save_data(data)
            flash("Payment method updated.")
            return redirect(url_for("payments"))
    return render_page("Edit Payment Method", PAYMENT_FORM, heading=f"Edit {method['label']}", method=method)


@app.route("/payments/<method_id>/toggle", methods=["POST"])
@login_required
def toggle_payment(method_id: str):
    data = load_data()
    methods = data["settings"].setdefault("payment_methods", [])
    method = next((item for item in methods if item.get("id") == method_id), None)
    if method:
        method["active"] = not method.get("active", True)
        method["updated_at"] = now_iso()
        save_data(data)
        flash("Payment method status updated.")
    else:
        flash("Payment method not found.")
    return redirect(url_for("payments"))


@app.route("/payments/<method_id>/delete", methods=["POST"])
@login_required
def delete_payment(method_id: str):
    data = load_data()
    methods = data["settings"].setdefault("payment_methods", [])
    remaining = [method for method in methods if method.get("id") != method_id]
    if len(remaining) == len(methods):
        flash("Payment method not found.")
    else:
        data["settings"]["payment_methods"] = remaining
        save_data(data)
        flash("Payment method deleted.")
    return redirect(url_for("payments"))


@app.route("/engagement", methods=["GET", "POST"])
@login_required
def engagement():
    data = load_data()
    settings = data["settings"]
    if request.method == "POST":
        settings["playful_mode"] = request.form.get("playful_mode") == "on"
        settings["language"] = request.form.get("language", "en")
        settings["admin_contact_url"] = request.form.get("admin_contact_url", "").strip()
        settings["community_url"] = request.form.get("community_url", "").strip()
        settings["social_proof_enabled"] = request.form.get("social_proof_enabled") == "on"
        settings["social_proof_attach_receipt"] = request.form.get("social_proof_attach_receipt") == "on"
        settings["warranty_requires_vouch"] = request.form.get("warranty_requires_vouch") == "on"
        settings["abandoned_cart_enabled"] = request.form.get("abandoned_cart_enabled") == "on"
        settings["abandoned_cart_interval_minutes"] = max(1, parse_int("abandoned_cart_interval_minutes", 60))
        settings["abandoned_cart_max_followups"] = max(1, parse_int("abandoned_cart_max_followups", 2))
        raw_urls = request.form.get("meme_gif_urls", "")
        settings["meme_gif_urls"] = [line.strip() for line in raw_urls.splitlines() if line.strip()]
        raw_templates = request.form.get("social_proof_templates", "")
        settings["social_proof_templates"] = [line.strip() for line in raw_templates.splitlines() if line.strip()]
        raw_cart_messages = request.form.get("abandoned_cart_messages", "")
        settings["abandoned_cart_messages"] = [line.strip() for line in raw_cart_messages.splitlines() if line.strip()]
        save_data(data)
        flash("Bot engagement settings updated.")
        return redirect(url_for("engagement"))

    return render_page(
        "Engagement",
        """
        <h1>Bot Engagement</h1>
        <div class="panel">
          <div class="panel-body">
            <form method="post" class="grid">
              <label class="checkbox span-2">
                <input type="checkbox" name="playful_mode" {% if settings.playful_mode %}checked{% endif %}>
                Playful mode with emojis and optional GIFs
              </label>
              <label class="span-2">Bot language for automatic messages
                <select name="language">
                  {% for value, label in languages.items() %}
                  <option value="{{ value }}" {% if settings.language == value %}selected{% endif %}>{{ label }}</option>
                  {% endfor %}
                </select>
              </label>
              <label class="span-2">Contact admin username/link
                <input name="admin_contact_url" value="{{ settings.admin_contact_url or '' }}" placeholder="https://t.me/your_admin_username">
              </label>
              <label class="span-2">Join community invite link
                <input name="community_url" value="{{ settings.community_url or '' }}" placeholder="https://t.me/+invite_code or https://t.me/community">
              </label>
              <label class="span-2">Meme GIF URLs
                <textarea name="meme_gif_urls" placeholder="Paste one direct GIF URL per line. The bot randomly sends one after add-to-cart or order creation.">{{ (settings.meme_gif_urls or [])|join('\n') }}</textarea>
              </label>
              <label class="checkbox span-2">
                <input type="checkbox" name="social_proof_enabled" {% if settings.social_proof_enabled %}checked{% endif %}>
                Auto-send social proof broadcasts to users when orders are completed
              </label>
              <label class="checkbox span-2">
                <input type="checkbox" name="social_proof_attach_receipt" {% if settings.social_proof_attach_receipt is not false %}checked{% endif %}>
                Attach the customer's receipt proof to social proof broadcasts when available
              </label>
              <label class="checkbox span-2">
                <input type="checkbox" name="warranty_requires_vouch" {% if settings.warranty_requires_vouch is not false %}checked{% endif %}>
                Recommend customer vouch to activate warranty tracking
              </label>
              <label class="span-2">Social proof message templates
                <textarea name="social_proof_templates" placeholder="One template per line. The dashboard uses them round-robin.">{{ (settings.social_proof_templates or [])|join('\n') }}</textarea>
              </label>
              <label class="checkbox span-2">
                <input type="checkbox" name="abandoned_cart_enabled" {% if settings.abandoned_cart_enabled %}checked{% endif %}>
                Enable abandoned-cart follow-up sequence
              </label>
              <label>Follow-up interval minutes
                <input type="number" min="1" step="1" name="abandoned_cart_interval_minutes" value="{{ settings.abandoned_cart_interval_minutes or 60 }}">
              </label>
              <label>Max follow-ups per cart
                <input type="number" min="1" step="1" name="abandoned_cart_max_followups" value="{{ settings.abandoned_cart_max_followups or 2 }}">
              </label>
              <label class="span-2">Abandoned-cart follow-up messages
                <textarea name="abandoned_cart_messages" placeholder="One message per line. The bot sends them in order.">{{ (settings.abandoned_cart_messages or [])|join('\n') }}</textarea>
              </label>
              <div class="actions span-2">
                <button class="primary" type="submit">Save Engagement Settings</button>
              </div>
            </form>
          </div>
        </div>
        """,
        settings=settings,
        languages=LANGUAGES,
    )


@app.route("/broadcast", methods=["GET", "POST"])
@login_required
def broadcast():
    data = load_data()
    users = known_user_ids(data)
    preview_users = selected_broadcast_users(data, request.args) if request.args else users
    if request.method == "POST":
        message = request.form.get("message", "").strip()
        media_url = request.form.get("media_url", "").strip()
        media_type = request.form.get("media_type", "animation")
        media_url = cloudinary_media_url(media_url, media_type)
        if not message:
            flash("Bulk message cannot be empty.")
            return redirect(url_for("broadcast"))
        target_ids = selected_broadcast_users(data, request.form)
        sent = 0
        failed = 0
        for user_id in target_ids:
            ok = send_telegram_media(user_id, media_type, media_url, message) if media_url else send_telegram_message(user_id, message)
            if ok:
                sent += 1
            else:
                failed += 1
        flash(f"Bulk message sent to {sent} user(s). Failed: {failed}.")
        return redirect(url_for("broadcast"))

    return render_page(
        "Broadcast",
        """
        <h1>Bulk Message</h1>
        <div class="stats">
          <div class="stat"><span>Known Users</span><strong>{{ users|length }}</strong></div>
          <div class="stat"><span>Selected Audience</span><strong>{{ preview_users|length }}</strong></div>
        </div>
        <h2>Audience Filters</h2>
        <div class="panel" style="margin-bottom:16px">
          <div class="panel-body">
            <form method="get" class="grid">
              <label class="span-2">Specific user IDs
                <input name="specific_user_ids" value="{{ filters.specific_user_ids }}" placeholder="5624385255, 123456789">
              </label>
              <label>Search name or ID
                <input name="q" value="{{ filters.q }}" placeholder="username, name, or ID">
              </label>
              <label>Status
                <select name="status">
                  <option value="">All users</option>
                  <option value="buyer" {% if filters.status == "buyer" %}selected{% endif %}>Buyers</option>
                  <option value="no_purchase" {% if filters.status == "no_purchase" %}selected{% endif %}>No purchase</option>
                  <option value="unread" {% if filters.status == "unread" %}selected{% endif %}>Unread</option>
                </select>
              </label>
              <label>Last order from
                <input type="date" name="date_from" value="{{ filters.date_from }}">
              </label>
              <label>Last order to
                <input type="date" name="date_to" value="{{ filters.date_to }}">
              </label>
              <label>Min avg order USD
                <input type="number" min="0" name="min_aov" value="{{ filters.min_aov }}">
              </label>
              <label>Max avg order USD
                <input type="number" min="0" name="max_aov" value="{{ filters.max_aov }}">
              </label>
              <label>Min lifetime value USD
                <input type="number" min="0" name="min_ltv" value="{{ filters.min_ltv }}">
              </label>
              <label>Max lifetime value USD
                <input type="number" min="0" name="max_ltv" value="{{ filters.max_ltv }}">
              </label>
              <div class="actions span-2">
                <button type="submit">Preview Audience</button>
                <a class="button" href="{{ url_for('broadcast') }}">Clear Filters</a>
              </div>
            </form>
          </div>
        </div>
        <h2>Send Message</h2>
        <div class="panel">
          <div class="panel-body">
            <form method="post" class="grid">
              <input type="hidden" name="specific_user_ids" value="{{ filters.specific_user_ids }}">
              <input type="hidden" name="q" value="{{ filters.q }}">
              <input type="hidden" name="status" value="{{ filters.status }}">
              <input type="hidden" name="date_from" value="{{ filters.date_from }}">
              <input type="hidden" name="date_to" value="{{ filters.date_to }}">
              <input type="hidden" name="min_aov" value="{{ filters.min_aov }}">
              <input type="hidden" name="max_aov" value="{{ filters.max_aov }}">
              <input type="hidden" name="min_ltv" value="{{ filters.min_ltv }}">
              <input type="hidden" name="max_ltv" value="{{ filters.max_ltv }}">
              <label class="span-2">Message
                <textarea name="message" placeholder="Write the message the bot should send to all known users." required></textarea>
              </label>
              <label>Media type
                <select name="media_type">
                  <option value="animation">GIF / Animation</option>
                  <option value="photo">Photo</option>
                  <option value="video">Video</option>
                  <option value="document">Document</option>
                </select>
              </label>
              <label>Optional media URL
                <input name="media_url" placeholder="https://example.com/image-or-gif">
              </label>
              <div class="actions span-2">
                <button class="primary" type="submit">Send Bulk Message</button>
              </div>
            </form>
          </div>
        </div>
        """,
        users=users,
        preview_users=preview_users,
        filters={
            "specific_user_ids": request.args.get("specific_user_ids", ""),
            "q": request.args.get("q", ""),
            "status": request.args.get("status", ""),
            "date_from": request.args.get("date_from", ""),
            "date_to": request.args.get("date_to", ""),
            "min_aov": request.args.get("min_aov", ""),
            "max_aov": request.args.get("max_aov", ""),
            "min_ltv": request.args.get("min_ltv", ""),
            "max_ltv": request.args.get("max_ltv", ""),
        },
    )


@app.route("/orders")
@login_required
def orders():
    data = load_data()
    status_filter = request.args.get("status", "")
    date_from = request.args.get("date_from", "")
    date_to = request.args.get("date_to", "")
    sort = request.args.get("sort", "recent")
    order_list = list(data["orders"].values())
    if status_filter:
        order_list = [order for order in order_list if order.get("status") == status_filter]
    if date_from:
        order_list = [order for order in order_list if order.get("created_at", "")[:10] >= date_from]
    if date_to:
        order_list = [order for order in order_list if order.get("created_at", "")[:10] <= date_to]
    if sort == "oldest":
        order_list = sorted(order_list, key=lambda order: order.get("created_at", ""))
    elif sort == "value_desc":
        order_list = sorted(order_list, key=lambda order: int(order.get("total_credits", 0)), reverse=True)
    else:
        order_list = sorted(order_list, key=lambda order: order.get("created_at", ""), reverse=True)
    order_page = paginate(order_list, page_number(), per_page=10)
    return render_page(
        "Orders",
        """
        <h1>Orders</h1>
        <div class="panel" style="margin-bottom:16px">
          <div class="panel-body">
            <form method="get" class="grid">
              <label>Status
                <select name="status">
                  <option value="">All statuses</option>
                  {% for value, label in statuses.items() %}
                  <option value="{{ value }}" {% if filters.status == value %}selected{% endif %}>{{ label }}</option>
                  {% endfor %}
                </select>
              </label>
              <label>From date
                <input type="date" name="date_from" value="{{ filters.date_from }}">
              </label>
              <label>To date
                <input type="date" name="date_to" value="{{ filters.date_to }}">
              </label>
              <label>Sort
                <select name="sort">
                  <option value="recent" {% if filters.sort == "recent" %}selected{% endif %}>Most recent</option>
                  <option value="oldest" {% if filters.sort == "oldest" %}selected{% endif %}>Oldest first</option>
                  <option value="value_desc" {% if filters.sort == "value_desc" %}selected{% endif %}>Highest value</option>
                </select>
              </label>
              <div class="actions span-2">
                <button class="primary" type="submit">Apply Filters</button>
                <a class="button" href="{{ url_for('orders') }}">Clear</a>
              </div>
            </form>
          </div>
        </div>
        <div class="panel">
          <table>
            <thead><tr><th>Order</th><th>Customer</th><th>Status</th><th>Total</th><th>Created</th><th></th></tr></thead>
            <tbody>
              {% for order in orders["items"] %}
              <tr>
                <td>{{ order.id }}</td>
                <td>{{ order.username or order.user_id }}</td>
                <td><span class="status">{{ statuses.get(order.status, order.status) }}</span></td>
                <td>{{ money(order.total_credits) }}</td>
                <td>{{ order.created_at }}</td>
                <td><a href="{{ url_for('order_detail', order_id=order.id) }}">Open</a></td>
              </tr>
              {% else %}
              <tr><td colspan="6" class="muted">No orders yet.</td></tr>
              {% endfor %}
            </tbody>
          </table>
          {{ pager("page", orders)|safe }}
        </div>
        """,
        orders=order_page,
        statuses=ORDER_STATUSES,
        filters={"status": status_filter, "date_from": date_from, "date_to": date_to, "sort": sort},
        money=money,
        pager=pager,
    )


@app.route("/orders/<order_id>")
@login_required
def order_detail(order_id: str):
    data = load_data()
    order = data["orders"].get(order_id)
    if not order:
        flash("Order not found.")
        return redirect(url_for("orders"))
    proof = order.get("proof") or {}
    receipt_url = proof.get("media_url") or (telegram_file_url(proof.get("value")) if proof.get("type") in {"photo", "document"} else "")
    vouches = []
    for vouch in order.get("vouches", []):
        item = dict(vouch)
        proof_item = item.get("proof") or {}
        item["proof_url"] = proof_item.get("media_url") or (telegram_file_url(proof_item.get("value")) if proof_item.get("type") in {"photo", "document"} else "")
        vouches.append(item)
    return render_page(
        "Order",
        """
        <h1>Order {{ order.id }}</h1>
        <section class="stats">
          <div class="stat"><span>Status</span><strong style="font-size:18px">{{ statuses.get(order.status, order.status) }}</strong></div>
          <div class="stat"><span>Customer</span><strong style="font-size:18px">{{ order.username or order.user_id }}</strong></div>
          <div class="stat"><span>Total</span><strong>{{ money(order.total_credits) }}</strong></div>
          <div class="stat"><span>Payment</span><strong style="font-size:18px">{{ order.payment_method.label }}</strong></div>
        </section>

        <h2>Items</h2>
        <div class="panel">
          <table>
            <thead><tr><th>Product</th><th>Qty</th><th>Price</th><th>Warranty</th><th>Subscription</th></tr></thead>
            <tbody>
              {% for item in order["items"] %}
              <tr>
                <td>{{ item.name }}</td>
                <td>{{ item.qty }}</td>
                <td>{{ money(item.line_total) }}</td>
                <td>{{ item.warranty_days }} days</td>
                <td>{{ item.subscription_days }} days</td>
              </tr>
              {% endfor %}
            </tbody>
          </table>
        </div>

        <h2>Receipt</h2>
        <div class="panel">
          <div class="panel-body">
            {% if proof %}
              <p><strong>Type:</strong> {{ proof.type }}</p>
              {% if proof.type == "text" %}
                <p class="prewrap">{{ proof.value }}</p>
              {% elif receipt_url %}
                <a class="button primary" href="{{ receipt_url }}" target="_blank" rel="noopener">Open Receipt File</a>
              {% else %}
                <p class="muted">Receipt file was uploaded, but the dashboard could not generate a Telegram file link.</p>
              {% endif %}
              <form method="post" action="{{ url_for('order_social_proof', order_id=order.id) }}" style="margin-top:12px">
                <button type="submit">Broadcast This Proof Manually</button>
              </form>
            {% else %}
              <p class="muted">No receipt uploaded yet.</p>
            {% endif %}
          </div>
        </div>

        <h2>Warranty Vouches</h2>
        <div class="panel">
          <div class="panel-body">
            {% if warranty_requires_vouch and has_warranty %}
              <p class="muted">Vouch is optional, but recommended if the customer wants warranty coverage tracked.</p>
            {% endif %}
            {% for item in vouches %}
            <div class="followup">
              <strong>{{ (item.get("from") or "customer").title() }}</strong>
              <span class="muted">{{ item.created_at }}</span>
              <div class="prewrap">{{ item.message }}</div>
              {% if item.proof_url %}
                <div><a href="{{ item.proof_url }}" target="_blank" rel="noopener">{{ item.proof.type or "proof" }}</a></div>
              {% endif %}
              <div class="muted">Warranty active: {{ "Yes" if item.warranty_activated else "No" }}</div>
            </div>
            {% else %}
            <p class="muted">No vouches saved yet.</p>
            {% endfor %}
          </div>
        </div>

        <h2>Process Order</h2>
        <div class="panel">
          <div class="panel-body">
            <p class="muted">To finish an order, send the product or credentials below. That delivery message is sent through the bot and automatically marks the order completed.</p>
            <form method="post" action="{{ url_for('order_status', order_id=order.id) }}" class="grid">
              <label>Status
                <select name="status">
                  {% for value, label in statuses.items() %}
                  <option value="{{ value }}" {% if value == order.status %}selected{% endif %}>{{ label }}</option>
                  {% endfor %}
                </select>
              </label>
              <div class="actions" style="align-items:end">
                <button class="primary" type="submit">Update Status</button>
              </div>
            </form>
          </div>
        </div>

        <h2>Product Delivery</h2>
        <div class="panel">
          <div class="panel-body">
            {% if order.delivery_message %}
              <p><strong>Delivered:</strong> {{ order.delivered_at or "Saved" }}</p>
              <div class="prewrap">{{ order.delivery_message }}</div>
            {% else %}
              <p class="muted">No product delivery message sent yet. The order cannot be completed until this is sent.</p>
            {% endif %}
            <form method="post" action="{{ url_for('order_delivery', order_id=order.id) }}" class="grid" style="margin-top:16px">
              <label class="span-2">Product / credentials message
                <textarea name="delivery_message" placeholder="Paste the digital product, login credentials, license key, download link, or delivery instructions." required>{{ order.delivery_message or "" }}</textarea>
              </label>
              <div class="actions span-2">
                <button class="primary" type="submit">Send Product and Complete Order</button>
              </div>
            </form>
          </div>
        </div>

        <h2>Follow Ups</h2>
        <div class="panel">
          <div class="panel-body">
            {% for item in order.followups or [] %}
            <div class="followup">
              <strong>{{ (item.get("from") or "customer").title() }}</strong>
              <span class="muted">{{ item.created_at }}</span>
              <div>{{ item.message }}</div>
            </div>
            {% else %}
            <p class="muted">No follow-ups yet.</p>
            {% endfor %}

            <form method="post" action="{{ url_for('order_reply', order_id=order.id) }}" class="grid" style="margin-top:16px">
              <label class="span-2">Bot follow-up to customer
                <textarea name="message" placeholder="Type an update, reminder, or answer for this order" required></textarea>
              </label>
              <label class="span-2">Optional GIF URL
                <input name="gif_url" placeholder="https://example.com/funny.gif">
              </label>
              <div class="actions span-2">
                <button class="primary" type="submit">Send Follow-Up</button>
              </div>
            </form>
          </div>
        </div>
        """,
        order=order,
        proof=proof,
        receipt_url=receipt_url,
        vouches=vouches,
        has_warranty=order_has_warranty(order),
        warranty_requires_vouch=data.get("settings", {}).get("warranty_requires_vouch", True),
        statuses=ORDER_STATUSES,
        money=money,
    )


@app.route("/orders/<order_id>/status", methods=["POST"])
@login_required
def order_status(order_id: str):
    status = request.form.get("status", "")
    if update_order_status(order_id, status):
        flash("Order status updated.")
    elif status == "completed":
        flash("To complete this order, send the product or credentials from Product Delivery first.")
    else:
        flash("Unable to update order status.")
    return redirect(url_for("order_detail", order_id=order_id))


@app.route("/orders/<order_id>/delivery", methods=["POST"])
@login_required
def order_delivery(order_id: str):
    delivery_message = request.form.get("delivery_message", "").strip()
    data = load_data()
    order = data["orders"].get(order_id)
    if not order:
        flash("Order not found.")
        return redirect(url_for("orders"))
    if not delivery_message:
        flash("Product delivery message cannot be empty.")
        return redirect(url_for("order_detail", order_id=order_id))

    previous_status = order.get("status")
    order["delivery_message"] = delivery_message
    order["delivered_at"] = now_iso()
    order["status"] = "completed"
    order["updated_at"] = now_iso()
    order.setdefault("followups", []).append(
        {
            "from": "admin",
            "message": f"Product delivered:\n\n{delivery_message}",
            "created_at": now_iso(),
        }
    )

    customer_message = (
        f"Product delivery for order {order_id}:\n\n{delivery_message}\n\n"
        "Your order is now finished. If you want warranty coverage, sending a vouch is optional but recommended. "
        "Open the order in the bot, tap Send Vouch for Warranty, then send a photo/screenshot showing the product is working with a short caption."
    )
    reply_markup = None
    if data.get("settings", {}).get("warranty_requires_vouch", True) and order_has_warranty(order):
        reply_markup = {
            "inline_keyboard": [
                [{"text": "Send Vouch for Warranty", "callback_data": f"vouch:start:{order_id}"}],
                [{"text": "View Order", "callback_data": f"order:view:{order_id}"}],
            ]
        }
    sent = send_telegram_message(int(order["user_id"]), customer_message, reply_markup=reply_markup)
    maybe_send_social_proof(data, order, previous_status)
    save_data(data)
    flash("Product sent and order completed." if sent else "Order completed, but Telegram delivery failed.")
    return redirect(url_for("order_detail", order_id=order_id))


@app.route("/orders/<order_id>/social-proof", methods=["POST"])
@login_required
def order_social_proof(order_id: str):
    data = load_data()
    order = data["orders"].get(order_id)
    if not order:
        flash("Order not found.")
        return redirect(url_for("orders"))
    sent, failed = send_social_proof_for_order(data, order, respect_auto_updates=False)
    save_data(data)
    flash(f"Proof broadcast sent to {sent} user(s). Failed: {failed}.")
    return redirect(url_for("order_detail", order_id=order_id))


@app.route("/orders/<order_id>/reply", methods=["POST"])
@login_required
def order_reply(order_id: str):
    message = request.form.get("message", "").strip()
    gif_url = request.form.get("gif_url", "").strip()
    gif_url = cloudinary_media_url(gif_url, "animation")
    data = load_data()
    order = data["orders"].get(order_id)
    if not order:
        flash("Order not found.")
        return redirect(url_for("orders"))
    if not message:
        flash("Reply cannot be empty.")
        return redirect(url_for("order_detail", order_id=order_id))

    order.setdefault("followups", []).append({"from": "admin", "message": message, "created_at": now_iso()})
    order["updated_at"] = now_iso()
    save_data(data)

    sent = send_telegram_message(int(order["user_id"]), f"{t_user(data, int(order['user_id']), 'followup', order_id=order_id)}\n\n{message}")
    gif_sent = send_telegram_animation(int(order["user_id"]), gif_url) if gif_url else True
    if sent and gif_sent:
        flash("Follow-up saved and sent to Telegram.")
    elif sent:
        flash("Follow-up text sent. GIF send failed.")
    else:
        flash("Follow-up saved. Telegram send failed or BOT_TOKEN is missing.")
    return redirect(url_for("order_detail", order_id=order_id))


if __name__ == "__main__":
    app.run(host=DASHBOARD_HOST, port=DASHBOARD_PORT, debug=False)
