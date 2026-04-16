"""
Amazon Deals → Telegram Affiliate Bot
======================================
Fetches deals from Amazon via RapidAPI, converts them into affiliate links,
and posts formatted messages to a Telegram channel on a daily schedule.

Dependencies:
    pip install requests python-telegram-bot schedule python-dotenv

Setup:
    1. Copy .env.example to .env and fill in your credentials.
    2. Run:  python amazon_deals_telegram_bot.py
"""

import os
import re
import time
import logging
import schedule
import requests
from datetime import datetime
from urllib.parse import urlparse, urlencode, parse_qs, urlunparse
from dotenv import load_dotenv
import telegram
from telegram.error import TelegramError
import asyncio

# ──────────────────────────────────────────────
# 0. ENVIRONMENT & LOGGING SETUP
# ──────────────────────────────────────────────
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
# 1. CONFIGURATION  (loaded from .env)
# ──────────────────────────────────────────────
class Config:
    """Central config — all values come from environment variables."""

    # Telegram
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHANNEL_ID: str = os.getenv("TELEGRAM_CHANNEL_ID", "")   # e.g. @YourChannel or -100xxxxxxx

    # Amazon affiliate tag (Associates ID)
    AMAZON_AFFILIATE_TAG: str = os.getenv("AMAZON_AFFILIATE_TAG", "")  # e.g. yourtag-20

    # RapidAPI – Amazon Deals endpoint
    RAPIDAPI_KEY: str = os.getenv("RAPIDAPI_KEY", "")
    RAPIDAPI_HOST: str = os.getenv("RAPIDAPI_HOST", "real-time-amazon-data.p.rapidapi.com")

    # Bot behaviour
    MAX_DEALS_PER_RUN: int = int(os.getenv("MAX_DEALS_PER_RUN", "5"))
    MIN_DISCOUNT_PERCENT: int = int(os.getenv("MIN_DISCOUNT_PERCENT", "20"))
    SCHEDULE_TIME: str = os.getenv("SCHEDULE_TIME", "09:00")   # 24-h HH:MM
    AMAZON_COUNTRY: str = os.getenv("AMAZON_COUNTRY", "US")    # US | UK | DE | IN …

    @classmethod
    def validate(cls) -> None:
        required = {
            "TELEGRAM_BOT_TOKEN": cls.TELEGRAM_BOT_TOKEN,
            "TELEGRAM_CHANNEL_ID": cls.TELEGRAM_CHANNEL_ID,
            "AMAZON_AFFILIATE_TAG": cls.AMAZON_AFFILIATE_TAG,
            "RAPIDAPI_KEY": cls.RAPIDAPI_KEY,
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise EnvironmentError(
                f"Missing required environment variables: {', '.join(missing)}\n"
                "Please check your .env file."
            )


# ──────────────────────────────────────────────
# 2. AFFILIATE LINK BUILDER
# ──────────────────────────────────────────────
def build_affiliate_link(amazon_url: str, tag: str) -> str:
    """
    Appends (or replaces) the affiliate tag query-param on any Amazon URL.
    Works for both long URLs and short  amzn.to / a.co  links.

    Args:
        amazon_url: Raw Amazon product URL.
        tag:        Your Associates tag (e.g. 'yourtag-20').

    Returns:
        Clean affiliate URL with tag injected.
    """
    if not amazon_url:
        return ""

    parsed = urlparse(amazon_url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    params["tag"] = [tag]           # overwrite any existing tag
    params.pop("ref", None)         # strip referrer noise
    params.pop("linkCode", None)

    new_query = urlencode({k: v[0] for k, v in params.items()})
    affiliate_url = urlunparse(parsed._replace(query=new_query))
    return affiliate_url


def extract_asin(url: str) -> str | None:
    """Extracts the Amazon ASIN from a product URL."""
    match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url)
    return match.group(1) if match else None


# ──────────────────────────────────────────────
# 3. AMAZON DEALS FETCHER  (via RapidAPI)
# ──────────────────────────────────────────────
BASE_DOMAIN_MAP = {
    "US": "amazon.com",
    "UK": "amazon.co.uk",
    "DE": "amazon.de",
    "IN": "amazon.in",
    "CA": "amazon.ca",
    "FR": "amazon.fr",
    "IT": "amazon.it",
    "ES": "amazon.es",
}

def fetch_deals() -> list[dict]:
    """
    Calls the Real-Time Amazon Data API on RapidAPI to fetch today's deals.

    Returns:
        List of deal dicts with keys: title, asin, url, price, original_price,
        discount_percent, rating, image_url.
    """
    logger.info("Fetching Amazon deals from RapidAPI…")

    endpoint = f"https://{Config.RAPIDAPI_HOST}/deals-v2"
    headers = {
        "X-RapidAPI-Key": Config.RAPIDAPI_KEY,
        "X-RapidAPI-Host": Config.RAPIDAPI_HOST,
    }
    params = {
        "country": Config.AMAZON_COUNTRY,
        "min_product_star_rating": "ALL",
        "price_range": "ALL",
        "discount_range": "ALL",        # ← valori accettati: ALL, 1, 2, 3, 4
        "sort_by": "HIGHEST_DISCOUNT",  # filtro sconto minimo applicato manualmente sotto
        "page": "1",
    }

    try:
        response = requests.get(endpoint, headers=headers, params=params, timeout=15)
        response.raise_for_status()
    except requests.exceptions.HTTPError as e:
        logger.error(f"HTTP error fetching deals: {e} — Response: {response.text[:300]}")
        return []
    except requests.exceptions.ConnectionError:
        logger.error("Connection error — check your internet connection.")
        return []
    except requests.exceptions.Timeout:
        logger.error("Request timed out while fetching deals.")
        return []

    data = response.json()
    raw_deals = data.get("data", {}).get("deals", [])

    if not raw_deals:
        logger.warning("No deals returned from API.")
        return []

    domain = BASE_DOMAIN_MAP.get(Config.AMAZON_COUNTRY, "amazon.com")
    deals = []

    for item in raw_deals[: Config.MAX_DEALS_PER_RUN * 3]:  # fetch 3× buffer for filtering
        try:
            asin = item.get("asin", "")
            if not asin:
                continue

            raw_url = f"https://www.{domain}/dp/{asin}"
            discount = int(item.get("discount_percentage", 0) or 0)

            if discount < Config.MIN_DISCOUNT_PERCENT:
                continue

            deal = {
                "title": item.get("product_title", "Amazon Product"),
                "asin": asin,
                "url": raw_url,
                "affiliate_url": build_affiliate_link(raw_url, Config.AMAZON_AFFILIATE_TAG),
                "price": item.get("product_price", "N/A"),
                "original_price": item.get("product_original_price", "N/A"),
                "discount_percent": discount,
                "rating": item.get("product_star_rating", "N/A"),
                "image_url": item.get("product_photo", ""),
                "category": item.get("deal_type", "General"),
            }
            deals.append(deal)

            if len(deals) >= Config.MAX_DEALS_PER_RUN:
                break

        except (KeyError, ValueError, TypeError) as e:
            logger.warning(f"Skipping malformed deal item: {e}")
            continue

    logger.info(f"Parsed {len(deals)} qualifying deals.")
    return deals


# ──────────────────────────────────────────────
# 4. MESSAGE FORMATTER
# ──────────────────────────────────────────────
def format_deal_message(deal: dict, index: int, total: int) -> str:
    """
    Builds a Telegram-compatible HTML message for one deal.

    Args:
        deal:   Deal dict from fetch_deals().
        index:  1-based position in the batch.
        total:  Total deals in this batch.

    Returns:
        HTML-formatted string ready for Telegram sendMessage.
    """
    title = deal["title"][:80] + ("…" if len(deal["title"]) > 80 else "")
    stars = "⭐" * round(float(deal["rating"])) if deal["rating"] not in ("N/A", "", None) else ""

    lines = [
        f"🔥 <b>Deal {index}/{total}</b>",
        "",
        f"🛍 <b>{title}</b>",
        "",
        f"💰 <b>{deal['price']}</b>  "
        f"<s>{deal['original_price']}</s>  "
        f"➡️  <b>-{deal['discount_percent']}% OFF</b>",
    ]

    if stars:
        lines.append(f"⭐ Rating: {deal['rating']} {stars}")

    lines += [
        "",
        f"🔗 <a href=\"{deal['affiliate_url']}\">👉 Grab This Deal</a>",
        "",
        f"<i>📦 Category: {deal['category']}</i>",
        f"<i>🗓 Posted: {datetime.now().strftime('%d %b %Y, %H:%M')}</i>",
    ]

    return "\n".join(lines)


def format_header_message(total: int) -> str:
    """Returns a daily-intro message posted before the deal batch."""
    date_str = datetime.now().strftime("%A, %d %B %Y")
    return (
        f"🛒 <b>Amazon Deals — {date_str}</b>\n\n"
        f"Here are today's top <b>{total} deals</b> with "
        f"at least <b>{Config.MIN_DISCOUNT_PERCENT}% off</b>!\n\n"
        f"All links below are affiliate links. Happy shopping! 🎉"
    )


# ──────────────────────────────────────────────
# 5. TELEGRAM POSTER
# ──────────────────────────────────────────────
async def _send_message(bot: telegram.Bot, text: str) -> bool:
    """Internal coroutine — sends one HTML message to the channel."""
    try:
        await bot.send_message(
            chat_id=Config.TELEGRAM_CHANNEL_ID,
            text=text,
            parse_mode=telegram.constants.ParseMode.HTML,
            disable_web_page_preview=False,
        )
        return True
    except TelegramError as e:
        logger.error(f"Telegram send error: {e}")
        return False


async def _post_deals_async(deals: list[dict]) -> None:
    """Posts header + all deals to Telegram asynchronously."""
    bot = telegram.Bot(token=Config.TELEGRAM_BOT_TOKEN)

    # Validate the bot token on first use
    try:
        me = await bot.get_me()
        logger.info(f"Authenticated as Telegram bot: @{me.username}")
    except TelegramError as e:
        logger.critical(f"Telegram authentication failed: {e}")
        return

    total = len(deals)

    # Post intro header
    header = format_header_message(total)
    await _send_message(bot, header)
    await asyncio.sleep(1)

    # Post each deal
    for i, deal in enumerate(deals, start=1):
        msg = format_deal_message(deal, i, total)
        success = await _send_message(bot, msg)
        if success:
            logger.info(f"Posted deal {i}/{total}: {deal['title'][:50]}")
        await asyncio.sleep(2)    # rate-limit: stay well below 30 msg/s


def post_deals_to_telegram(deals: list[dict]) -> None:
    """Sync wrapper around the async poster — safe to call from scheduler."""
    asyncio.run(_post_deals_async(deals))


# ──────────────────────────────────────────────
# 6. MAIN JOB
# ──────────────────────────────────────────────
def run_job() -> None:
    """
    Full pipeline:
      1. Validate config
      2. Fetch deals from Amazon (RapidAPI)
      3. Post to Telegram
    """
    logger.info("=" * 60)
    logger.info("Amazon Deals Bot — job started")

    try:
        Config.validate()
    except EnvironmentError as e:
        logger.critical(str(e))
        return

    deals = fetch_deals()

    if not deals:
        logger.warning("No deals found — skipping Telegram post.")
        return

    post_deals_to_telegram(deals)
    logger.info(f"Job complete — {len(deals)} deals posted.")
    logger.info("=" * 60)


# ──────────────────────────────────────────────
# 7. SCHEDULER
# ──────────────────────────────────────────────
def start_scheduler() -> None:
    """
    Schedules run_job() daily at the time set in SCHEDULE_TIME (HH:MM, 24-h).
    Also runs once immediately on startup so you can verify everything works.
    """
    Config.validate()   # fail fast before waiting hours

    logger.info(f"Scheduler started — daily run at {Config.SCHEDULE_TIME}")

    # Immediate first run
    run_job()

    # Daily recurring job
    schedule.every().day.at(Config.SCHEDULE_TIME).do(run_job)

    while True:
        schedule.run_pending()
        time.sleep(30)


# ──────────────────────────────────────────────
# 8. ENTRY POINT
# ──────────────────────────────────────────────
if __name__ == "__main__":
    start_scheduler()
