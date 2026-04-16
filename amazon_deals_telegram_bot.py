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

def _parse_discount(badge: str) -> int:
    """
    Estrae la percentuale di sconto da stringhe come '26% off' o 'Up to 40% off'.
    Restituisce 0 se non riesce a parsare.
    """
    if not badge:
        return 0
    match = re.search(r"(\d+)\s*%", badge)
    return int(match.group(1)) if match else 0


def fetch_deals() -> list[dict]:
    """
    Chiama l'endpoint /deals dell'API Real-Time Amazon Data su RapidAPI.

    Struttura reale della risposta:
        {
          "deal_id": "8325275f",
          "deal_type": "BEST_DEAL",
          "deal_title": "Sony 77 Inch OLED...",
          "deal_photo": "https://...",
          "deal_state": "AVAILABLE",
          "deal_url": "https://www.amazon.com/.../dp/B0CVQGRW9F",
          "canonical_deal_url": "https://www.amazon.com/deal/8325275f",
          "deal_badge": "26% off",
          "product_asin": "B0CVQGRW9F"
        }

    Returns:
        Lista di dict con i deal qualificanti.
    """
    logger.info("Fetching Amazon deals from RapidAPI…")

    # Endpoint corretto per i Today's Deals
    endpoint = f"https://{Config.RAPIDAPI_HOST}/deals"
    headers = {
        "X-RapidAPI-Key": Config.RAPIDAPI_KEY,
        "X-RapidAPI-Host": Config.RAPIDAPI_HOST,
    }
    params = {
        "country": Config.AMAZON_COUNTRY,
        "deal_state": "AVAILABLE",   # solo offerte attive
    }

    try:
        response = requests.get(endpoint, headers=headers, params=params, timeout=20)
        response.raise_for_status()
    except requests.exceptions.HTTPError as e:
        logger.error(f"HTTP error fetching deals: {e} — Response: {response.text[:300]}")
        return []
    except requests.exceptions.ConnectionError:
        logger.error("Connection error — controlla la connessione internet.")
        return []
    except requests.exceptions.Timeout:
        logger.error("Timeout — l'API non ha risposto in tempo.")
        return []

    data = response.json()

    # Log della risposta grezza per debug (solo primi 500 char)
    logger.debug(f"Raw API response: {str(data)[:500]}")

    # La risposta può avere la lista sotto "deals" oppure direttamente come lista
    raw_deals = (
        data.get("deals")
        or data.get("data", {}).get("deals", [])
        or (data if isinstance(data, list) else [])
    )

    if not raw_deals:
        logger.warning(f"Nessun deal ricevuto dall'API. Risposta: {str(data)[:300]}")
        return []

    logger.info(f"Ricevuti {len(raw_deals)} deal totali dall'API, filtro in corso…")

    domain = BASE_DOMAIN_MAP.get(Config.AMAZON_COUNTRY, "amazon.com")
    deals = []

    for item in raw_deals:
        try:
            # Campi reali dell'API Today's Deals
            asin = item.get("product_asin", "")
            deal_url = item.get("deal_url", "")
            deal_state = item.get("deal_state", "")

            # Salta se non disponibile
            if deal_state and deal_state != "AVAILABLE":
                continue

            # Costruisce URL prodotto: usa deal_url se disponibile, altrimenti ASIN
            if deal_url and "/dp/" in deal_url:
                raw_url = deal_url.split("?")[0]   # rimuove parametri extra
            elif asin:
                raw_url = f"https://www.{domain}/dp/{asin}"
            else:
                continue   # niente URL valido, salta

            # Estrae ASIN dall'URL se non c'è nel campo diretto
            if not asin:
                asin = extract_asin(raw_url) or ""

            # Percentuale di sconto da "deal_badge" (es. "26% off")
            badge = item.get("deal_badge", "")
            discount = _parse_discount(badge)

            # Filtra per sconto minimo
            if discount < Config.MIN_DISCOUNT_PERCENT:
                continue

            deal = {
                "title": item.get("deal_title", "Offerta Amazon"),
                "asin": asin,
                "url": raw_url,
                "affiliate_url": build_affiliate_link(raw_url, Config.AMAZON_AFFILIATE_TAG),
                "price": "Vedi su Amazon",          # non fornito nell'endpoint deals
                "original_price": "",
                "discount_percent": discount,
                "rating": "N/A",                    # non fornito nell'endpoint deals
                "image_url": item.get("deal_photo", ""),
                "category": item.get("deal_type", "General"),
                "canonical_url": item.get("canonical_deal_url", raw_url),
                "ends_at": item.get("deal_ends_at", ""),
            }
            deals.append(deal)

            if len(deals) >= Config.MAX_DEALS_PER_RUN:
                break

        except (KeyError, ValueError, TypeError) as e:
            logger.warning(f"Skipping malformed deal item: {e} — item: {str(item)[:100]}")
            continue

    logger.info(f"Parsed {len(deals)} qualifying deals (sconto >= {Config.MIN_DISCOUNT_PERCENT}%).")
    return deals


# ──────────────────────────────────────────────
# 4. MESSAGE FORMATTER
# ──────────────────────────────────────────────
def format_deal_message(deal: dict, index: int, total: int) -> str:
    """
    Costruisce un messaggio HTML per Telegram per una singola offerta.

    Args:
        deal:   Dict deal da fetch_deals().
        index:  Posizione 1-based nel batch.
        total:  Totale deal nel batch.

    Returns:
        Stringa HTML pronta per Telegram sendMessage.
    """
    title = deal["title"][:90] + ("…" if len(deal["title"]) > 90 else "")

    # Categoria leggibile in italiano
    category_map = {
        "BEST_DEAL": "Best Deal 🏆",
        "LIGHTNING_DEAL": "Offerta Lampo ⚡",
        "DEAL_OF_THE_DAY": "Offerta del Giorno 🌟",
    }
    category = category_map.get(deal["category"], deal["category"])

    # Scadenza offerta (se disponibile)
    ends_str = ""
    if deal.get("ends_at"):
        try:
            ends_dt = datetime.fromisoformat(deal["ends_at"].replace("Z", "+00:00"))
            ends_str = f"\n⏰ <i>Scade: {ends_dt.strftime('%d/%m/%Y %H:%M')}</i>"
        except Exception:
            pass

    lines = [
        f"🔥 <b>Offerta {index}/{total}</b>",
        "",
        f"🛍 <b>{title}</b>",
        "",
        f"💥 <b>-{deal['discount_percent']}% di SCONTO</b>",
        "",
        f"🔗 <a href=\"{deal['affiliate_url']}\">👉 Vai all'Offerta</a>",
        "",
        f"<i>📦 {category}</i>",
        f"<i>🗓 {datetime.now().strftime('%d/%m/%Y %H:%M')}</i>",
    ]

    if ends_str:
        lines.append(ends_str)

    return "\n".join(lines)


def format_header_message(total: int) -> str:
    """Messaggio introduttivo prima del batch di offerte."""
    date_str = datetime.now().strftime("%A %d %B %Y")
    return (
        f"🛒 <b>Offerte Amazon — {date_str}</b>\n\n"
        f"Ecco le migliori <b>{total} offerte</b> di oggi "
        f"con almeno <b>{Config.MIN_DISCOUNT_PERCENT}% di sconto</b>!\n\n"
        f"I link qui sotto sono link affiliati. Buono shopping! 🎉"
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
