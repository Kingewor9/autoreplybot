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
- Set `ADMIN_TELEGRAM_ID` to your numeric Telegram user ID to enable the private `/admin` panel.
  The panel shows user and plan counts and supports confirmed text/photo broadcasts with CTA links.
- Use an always-on instance: a sleeping free instance delays or drops auto-replies.

## BotFather setup
1. Create the bot, then in @BotFather open Bot Settings > Business Mode and enable it.
2. Customers need Telegram Premium to connect it (Settings > Telegram Business > Chatbots).
