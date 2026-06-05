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
   Add `CLOUDINARY_URL` if you want receipts, vouches, and media replies stored in Cloudinary. You can also use `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, and `CLOUDINARY_API_SECRET` instead.
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

## Vercel Hobby Notes

The current `bot.py` runs Telegram long polling, which needs an always-on Python process. Vercel Functions are request-based and scale down when idle, so use a Telegram webhook endpoint for Vercel deployments or run the bot worker on an always-on host.

Vercel Hobby cron jobs are useful for daily maintenance tasks only. They are not a keep-alive solution for a polling Telegram bot.

`store_db.json` is used only when Supabase is not configured. For Vercel production, set Supabase env vars so live orders, customers, messages, receipts, and products persist outside the function filesystem.

This repo includes `app.py` as the Vercel Flask entrypoint. After deployment, the dashboard should open at your Vercel domain root.

For Telegram webhook mode on Vercel, set these environment variables in Vercel:

- `BOT_TOKEN`
- `ADMIN_IDS`
- `DASHBOARD_PASSWORD`
- `DASHBOARD_SECRET_KEY`
- `TELEGRAM_WEBHOOK_PATH_SECRET`
- `TELEGRAM_WEBHOOK_SECRET`
- `CLOUDINARY_CLOUD_NAME`
- `CLOUDINARY_API_KEY`
- `CLOUDINARY_API_SECRET`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `SUPABASE_STORE_STATE_ID`

Create this table in the Supabase SQL editor:

```sql
create table if not exists public.store_state (
  id text primary key,
  data jsonb not null default '{}'::jsonb,
  updated_at timestamptz not null default now()
);

create or replace function public.touch_store_state_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists touch_store_state_updated_at on public.store_state;

create trigger touch_store_state_updated_at
before update on public.store_state
for each row
execute function public.touch_store_state_updated_at();
```

Then set the webhook URL:

```text
https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=https://your-project.vercel.app/telegram/webhook/<TELEGRAM_WEBHOOK_PATH_SECRET>&secret_token=<TELEGRAM_WEBHOOK_SECRET>
```

Do not run `bot.py` polling and webhook mode against the same bot token at the same time.

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

Payment methods, wallet addresses, contact links, community links, products, customer messages, orders, receipts, and vouches are stored in Supabase when `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are set.

New active products and restocks automatically notify known users. Known users are collected when they interact with the bot or when they already have carts/orders in storage.

Abandoned-cart follow-ups run inside the bot process. Configure interval, max follow-ups, and message sequence in `Bot Engagement`; customers who have automatic updates disabled are skipped.

Treat `SUPABASE_SERVICE_ROLE_KEY` as a private server-only secret. Do not expose it in client-side code.

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

The bot stores data in Supabase when Supabase env vars are set. Without Supabase, it falls back to `store_db.json` for local testing.
