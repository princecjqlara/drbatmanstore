# Telegram Store Bot

Python Telegram bot with:

- Store button and product catalog
- Website dashboard with store stats, stock counts, order counts, USD revenue, and paginated reporting
- Admin product creation with stock, USD price, warranty days, and subscription days
- Cart and checkout
- Payment method selection with dashboard-managed custom instructions
- Order processing by admins
- Customer follow-up messages and admin replies from the website dashboard
- Dashboard-triggered bot follow-ups to customers, with optional GIF URLs
- Playful bot mode with emojis and configurable meme GIF URLs
- Automatic customer broadcasts for new active products and restocks
- Manual bulk messages to all known bot users or filtered/specific customer segments
- Dashboard analytics for top-selling products, user count, average order value, and lifetime value
- Customer analytics with per-user order history, lifetime value, top customers, and no-purchase rate
- Per-customer automatic update opt-out from product/restock/social-proof broadcasts
- Order filtering by status, date range, and sort order
- Users inbox with a messenger-style customer list, searchable conversations, direct bot replies, and optional media
- Bulk messages with optional photo, GIF/animation, video, or document URL
- Cloudinary media storage for customer receipts, vouches, contact media, and dashboard-sent media
- Optional anonymous social-proof broadcasts when orders are completed, using round-robin message templates
- Warranty vouch flow that asks customers for a proof photo/screenshot after delivery
- Language setting for automatic dashboard-driven bot messages
- Upsell suggestions after add-to-cart and during checkout
- Admin-selected top 2 featured products shown first in the Telegram store

## Setup

1. Create a bot with BotFather and copy the bot token.
2. Copy `.env.example` to `.env`.
3. Fill in `BOT_TOKEN`, `ADMIN_IDS`, and `DASHBOARD_PASSWORD`.
   Add `CLOUDINARY_URL` if you want receipts, vouches, and media replies stored in Cloudinary.
4. Install dependencies:

```powershell
py -m pip install -r requirements.txt
```

5. Run the bot:

```powershell
py bot.py
```

6. Run the website dashboard in another terminal:

```powershell
py web_dashboard.py
```

Then open:

```text
http://127.0.0.1:8080
```

Optional demo data:

```powershell
Copy-Item store_db.example.json store_db.json
```

Use this only for a fresh test store. Do not overwrite a production `store_db.json`.

## Admin ID

`ADMIN_IDS` is your numeric Telegram user ID. It is not your username.

Use it so the bot knows who can receive admin notifications and use bot-side admin commands.

Easy ways to find it:

- Message `@userinfobot` on Telegram.
- Message your bot, then check your Telegram user ID from bot logs if you add logging later.

Example:

```env
ADMIN_IDS=123456789
```

For multiple admins:

```env
ADMIN_IDS=123456789,987654321
```

## Website Dashboard

The dashboard is a website, not a Telegram button. Use it at `http://127.0.0.1:8080` after running `py web_dashboard.py`.

Dashboard actions:

- `Add Product`: creates a new store product.
- `Manage Products`: views products and lets you toggle active status.
- `Payment Methods`: add crypto wallet addresses, bank transfer, or any custom payment instructions.
- `Bot Engagement`: enable playful mode, set language, add GIF URLs, and configure anonymous social-proof templates.
- `Bulk Message`: send one bot message to all known users, specific IDs, or filtered user segments, optionally with media.
- `Users`: messenger-style inbox with customers on the left, conversation history on the right, bot replies, media sending, and per-customer profile links.
- `Users`: filter by name/ID, buyer status, unread messages, last-order date, average order value, lifetime value, most recent conversation, most recent order, or most orders.
- `Dashboard` and `Orders`: filter by date/status/sort and use pagination for long lists.
- `Bot Engagement`: configure language, social proof templates, GIF URLs, and abandoned-cart follow-up sequences.
- `Process Orders`: lists recent orders and lets you mark them paid, processing, completed, or cancelled.
- Order follow-up replies: open an order and send a bot follow-up back to the customer through Telegram, optionally with a GIF URL.

During product creation, the dashboard form includes:

- Name
- Description
- USD price
- Stock count
- Warranty days
- Subscription days
- Store highlight rank (`Top 1` or `Top 2`) for products that should appear first in the bot store
- Upsell product IDs

Upsell IDs are optional. If left blank, the bot suggests other available active products automatically.

Payment methods and wallet addresses are stored in `store_db.json` after first dashboard load. `PAYMENT_METHODS` in `.env` is only a startup fallback/migration source.

New active products and restocks automatically notify known users. Known users are collected when they interact with the bot or when they already have carts/orders in `store_db.json`.

Abandoned-cart follow-ups run inside the bot process. Configure interval, max follow-ups, and message sequence in `Bot Engagement`; customers who have automatic updates disabled are skipped.

Supabase connection values can be stored in `.env`, but this version still uses `store_db.json` for live bot/dashboard data. Treat `SUPABASE_SERVICE_ROLE_KEY` as private server-only secret.

## Customer Flow

Customers can:

- Tap `Store` to browse products
- Add products to cart
- View and edit cart
- Checkout
- Choose payment method
- Upload payment proof
- Wait for admin verification and product delivery through the bot
- Send an optional vouch photo/screenshot after delivery to activate warranty tracking
- View orders
- Send follow-up messages to admins

## Storage

The bot stores data in `store_db.json` by default. Back this file up if you run the bot in production.
