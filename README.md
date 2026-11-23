# Telegram Auto-Reply Bot (Business mode)

Python 3.11+ · aiogram 3 · FastAPI webhook · MongoDB (PyMongo async)

## Run locally (polling)
    python -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt
    cp .env.example .env      # fill BOT_TOKEN and MONGODB_URI
    python run_polling.py

## Deploy on Render
- Build: `pip install -r requirements.txt`
- Start: `uvicorn app.main:app --host 0.0.0.0 --port $PORT`
- Set the env vars from `.env.example` (RENDER_EXTERNAL_URL is picked up automatically).
- Add your Paystack secret key as `PAYSTACK_SECRET_KEY`. Pro costs ₦7,500 and Growth ₦15,000 by default;
  override these with `PRO_NAIRA_PRICE` and `GROWTH_NAIRA_PRICE` if needed.
- Naira checkout initializes a Paystack transaction and provides an **I've Paid** button that verifies
  the transaction through Paystack's API. This works with `python run_polling.py` and does not require
  a public URL or webhook. Naira payments grant 30 days and are not automatically recurring; users
  renew manually. Telegram Stars subscriptions continue to renew automatically.
- If you deploy the FastAPI app with a public URL, you can optionally configure Paystack's webhook URL
  as `https://<your-app-domain>/paystack/webhook`; webhook signatures and transaction details are verified.
- Set `ADMIN_TELEGRAM_ID` to your numeric Telegram user ID to enable the private `/admin` panel.
  The panel shows user and plan counts and supports confirmed text/photo broadcasts with CTA links.
- Use an always-on instance: a sleeping free instance delays or drops auto-replies.

## BotFather setup
1. Create the bot, then in @BotFather open Bot Settings > Business Mode and enable it.
2. Customers need Telegram Premium to connect it (Settings > Telegram Business > Chatbots).
