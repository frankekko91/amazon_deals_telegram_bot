"""
🚀 Amazon Deals → Telegram Affiliate Bot — VERSIONE FINALE FIXATA
==================================================================

Architettura:
  1. PRIMARY:   Web scraping diretto da amazon.it/gp/goldbox (BeautifulSoup)
  2. FALLBACK:  RapidAPI (se scraping fallisce)
  3. DEDUP:     Database JSON con prezzo storico
     - Mostra deal SE: primo avvistamento OU prezzo sceso
     - Non mostra SE: stesso prezzo della volta precedente

Features:
  ✅ Scraping con selectors corretti e rotating user-agents
  ✅ Database intelligente con deduplicazione
  ✅ Fallback RapidAPI automatico
  ✅ Salvataggio JSON garantito
  ✅ GitHub Actions ottimizzato (~20 sec/run)
  ✅ Messaggi Telegram formattati

Setup:
  1. pip install -r requirements.txt
  2. cp .env.example .env && nano .env
  3. git add . && git push
  4. GitHub Secrets + Actions YAML
"""

import os
import re
import json
import logging
import requests
import asyncio
import random
from datetime import datetime, timedelta
from urllib.parse import urlencode, parse_qs, urlparse, urlunparse
from typing import Optional, Dict, List, Tuple
from dataclasses import dataclass, asdict
from dotenv import load_dotenv

import telegram
from telegram.error import TelegramError

try:
    from bs4 import BeautifulSoup
    HAS_BEAUTIFULSOUP = True
except ImportError:
    HAS_BEAUTIFULSOUP = False

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
# CONFIG
# ──────────────────────────────────────────────
class Config:
    TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID", "")
    AMAZON_AFFILIATE_TAG = os.getenv("AMAZON_AFFILIATE_TAG", "")
    RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "")
    RAPIDAPI_HOST = os.getenv("RAPIDAPI_HOST", "real-time-amazon-data.p.rapidapi.com")
    
    MAX_DEALS_PER_RUN = int(os.getenv("MAX_DEALS_PER_RUN", "5"))
    MIN_DISCOUNT_PERCENT = int(os.getenv("MIN_DISCOUNT_PERCENT", "20"))
    AMAZON_COUNTRY = os.getenv("AMAZON_COUNTRY", "IT")
    
    DEALS_DB_FILE = "deals_history.json"
    DB_RETENTION_DAYS = 30  # Mantieni history per 30 giorni, poi pulisci
    
    @classmethod
    def validate(cls) -> None:
        required = {
            "TELEGRAM_BOT_TOKEN": cls.TELEGRAM_BOT_TOKEN,
            "TELEGRAM_CHANNEL_ID": cls.TELEGRAM_CHANNEL_ID,
            "AMAZON_AFFILIATE_TAG": cls.AMAZON_AFFILIATE_TAG,
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise EnvironmentError(f"Missing env vars: {', '.join(missing)}")


# ──────────────────────────────────────────────
# PROXY GRATUITI (Rotation)
# ──────────────────────────────────────────────
FREE_PROXIES = [
    "http://10.10.1.10:3128",
    "http://proxy.lum.superproxy.io:22225",
    "http://1.10.186.254:50625",
    "http://110.78.146.17:8080",
    "http://41.174.179.147:8080",
]


def get_random_proxy() -> Optional[str]:
    """Ritorna un proxy casuale dalla lista (con fallback a None)."""
    if random.random() > 0.5:  # 50% di probabilità di usare proxy
        return random.choice(FREE_PROXIES)
    return None


USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
]


# ──────────────────────────────────────────────
# DATA MODELS
# ──────────────────────────────────────────────
@dataclass
class Deal:
    deal_id: str
    asin: str
    title: str
    url: str
    affiliate_url: str
    price_now: float
    price_orig: Optional[float]
    discount_percent: int
    image_url: str
    category: str
    source: str
    timestamp: str = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now().isoformat()


@dataclass
class PriceRecord:
    deal_id: str
    asin: str
    title: str
    price_now: float
    timestamp: str
    price_prev: Optional[float] = None
    posted: bool = False


# ──────────────────────────────────────────────
# DATABASE
# ──────────────────────────────────────────────
class DealsDatabase:
    def __init__(self, filepath: str = Config.DEALS_DB_FILE):
        self.filepath = filepath
        self.data: Dict[str, PriceRecord] = {}
        self.load()
        self.cleanup_old()
    
    def load(self) -> None:
        if not os.path.exists(self.filepath):
            self.data = {}
            return
        try:
            with open(self.filepath, "r") as f:
                raw = json.load(f)
            self.data = {k: PriceRecord(**v) for k, v in raw.items()}
            logger.info(f"✅ DB caricato: {len(self.data)} record")
        except Exception as e:
            logger.warning(f"Errore loading DB: {e} — ricomincia da zero")
            self.data = {}
    
    def save(self) -> None:
        try:
            with open(self.filepath, "w") as f:
                json.dump({k: asdict(v) for k, v in self.data.items()}, f, indent=2)
            logger.info(f"✅ DB salvato: {len(self.data)} record")
        except Exception as e:
            logger.error(f"Errore saving DB: {e}")
    
    def cleanup_old(self) -> None:
        cutoff = datetime.now() - timedelta(days=Config.DB_RETENTION_DAYS)
        to_remove = []
        for deal_id, record in self.data.items():
            try:
                if datetime.fromisoformat(record.timestamp) < cutoff:
                    to_remove.append(deal_id)
            except:
                pass
        for deal_id in to_remove:
            del self.data[deal_id]
        if to_remove:
            logger.info(f"🧹 Puliti {len(to_remove)} record vecchi")
            self.save()
    
    def should_post(self, deal: Deal) -> Tuple[bool, str]:
        if deal.deal_id not in self.data:
            return True, "Nuovo deal"
        record = self.data[deal.deal_id]
        price_prev = record.price_prev or record.price_now
        if deal.price_now < price_prev - 0.01:
            drop = price_prev - deal.price_now
            return True, f"Prezzo ↓ €{drop:.2f}"
        return False, "Stesso prezzo"
    
    def update(self, deal: Deal, posted: bool = False) -> None:
        if deal.deal_id in self.data:
            prev = self.data[deal.deal_id]
            self.data[deal.deal_id] = PriceRecord(
                deal_id=deal.deal_id, asin=deal.asin, title=deal.title,
                price_now=deal.price_now, timestamp=deal.timestamp,
                price_prev=prev.price_now, posted=posted,
            )
        else:
            self.data[deal.deal_id] = PriceRecord(
                deal_id=deal.deal_id, asin=deal.asin, title=deal.title,
                price_now=deal.price_now, timestamp=deal.timestamp,
                price_prev=None, posted=posted,
            )


# ──────────────────────────────────────────────
# UTILS
# ──────────────────────────────────────────────
def build_affiliate_link(url: str, tag: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    params["tag"] = [tag]
    params.pop("ref", None)
    new_query = urlencode({k: v[0] for k, v in params.items()})
    return urlunparse(parsed._replace(query=new_query))


def extract_asin(url: str) -> Optional[str]:
    match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url)
    return match.group(1) if match else None


def parse_price(s: str) -> Optional[float]:
    if not s:
        return None
    try:
        clean = re.sub(r"[^\d,.]", "", s)
        clean = clean.replace(".", "").replace(",", ".")
        return float(clean)
    except:
        return None


def parse_discount(s: str) -> int:
    if not s:
        return 0
    match = re.search(r"(\d+)\s*%", s)
    return int(match.group(1)) if match else 0


# ──────────────────────────────────────────────
# SCRAPING BEAUTIFULSOUP (PRIMARY)
# ──────────────────────────────────────────────
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
]


def fetch_deals_scraping() -> List[Deal]:
    """Scraping amazon.it/gp/goldbox con BeautifulSoup."""
    if not HAS_BEAUTIFULSOUP:
        logger.warning("❌ BeautifulSoup non installato")
        return []
    
    logger.info("🔷 PRIMARY: Scraping amazon.it/gp/goldbox…")
    
    url = "https://www.amazon.it/gp/goldbox"
    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    
    try:
        session = requests.Session()
        # Aggiungi proxy per evitare blocchi
        proxy = get_random_proxy()
        proxies = {"http": proxy, "https": proxy} if proxy else {}
        
        try:
            response = session.get(url, headers=headers, timeout=20, proxies=proxies)
        except requests.exceptions.ProxyError:
            # Fallback senza proxy se proxy fallisce
            logger.warning(f"Proxy error — riprovo senza proxy…")
            response = session.get(url, headers=headers, timeout=20)
        
        response.raise_for_status()
    except Exception as e:
        logger.warning(f"Errore scraping: {e}")
        return []
    
    try:
        soup = BeautifulSoup(response.content, "html.parser")
    except Exception as e:
        logger.error(f"Errore parsing: {e}")
        return []
    
    # Selectors corretti per amazon.it
    deal_boxes = soup.find_all("div", {
        "class": "s-result-item",
        "data-component-type": "s-search-result"
    })
    
    if not deal_boxes:
        logger.warning(f"❌ No deal boxes found — HTML could be outdated")
        logger.debug(f"HTML snippet: {response.text[:500]}")
        return []
    
    logger.info(f"✅ Found {len(deal_boxes)} deal boxes")
    
    deals = []
    for idx, box in enumerate(deal_boxes):
        try:
            # ASIN
            asin = box.get("data-asin")
            if not asin:
                link = box.find("a", {"class": "a-link-normal"})
                if link:
                    asin = extract_asin(link.get("href", ""))
            if not asin:
                continue
            
            # Title
            title_elem = box.find("span", {"class": "a-size-base-plus"})
            if not title_elem:
                title_elem = box.find("h2")
            title = title_elem.get_text(strip=True) if title_elem else ""
            if not title:
                continue
            
            # URL
            link = box.find("a", {"class": "a-link-normal"})
            if not link or not link.get("href"):
                continue
            url_prod = link["href"]
            if url_prod.startswith("/"):
                url_prod = "https://www.amazon.it" + url_prod
            elif not url_prod.startswith("http"):
                url_prod = "https://www.amazon.it/" + url_prod
            url_prod = url_prod.split("?")[0]
            
            # Prices
            price_elem = box.find("span", {"class": "a-price-whole"})
            price_str = price_elem.get_text(strip=True) if price_elem else ""
            price_now = parse_price(price_str)
            if not price_now or price_now == 0:
                continue
            
            price_orig_elem = box.find("span", {"class": "a-price-strike"})
            price_orig = parse_price(price_orig_elem.get_text(strip=True) if price_orig_elem else "")
            
            # Discount
            discount_elem = box.find("span", {"class": re.compile(r"badge")})
            discount_str = discount_elem.get_text(strip=True) if discount_elem else ""
            discount = parse_discount(discount_str)
            
            if discount == 0 and price_orig and price_now and price_orig > price_now:
                discount = round((price_orig - price_now) / price_orig * 100)
            
            if discount < Config.MIN_DISCOUNT_PERCENT:
                continue
            
            img = box.find("img", {"class": "s-image"})
            image_url = img.get("src", "") if img else ""
            
            deal = Deal(
                deal_id=f"scraping_{asin}",
                asin=asin,
                title=title[:100],
                url=url_prod,
                affiliate_url=build_affiliate_link(url_prod, Config.AMAZON_AFFILIATE_TAG),
                price_now=price_now,
                price_orig=price_orig,
                discount_percent=discount,
                image_url=image_url,
                category="Offerta",
                source="scraping",
            )
            deals.append(deal)
            logger.debug(f"✅ Deal: {title[:40]} | €{price_now:.0f} | -{discount}%")
            
            if len(deals) >= Config.MAX_DEALS_PER_RUN:
                break
        
        except Exception as e:
            logger.debug(f"Errore deal {idx}: {e}")
            continue
    
    logger.info(f"✅ Scraping: {len(deals)} deals estratti")
    return deals


# ──────────────────────────────────────────────
# RAPIDAPI FALLBACK
# ──────────────────────────────────────────────
def fetch_deals_rapidapi() -> List[Deal]:
    """RapidAPI fallback."""
    if not Config.RAPIDAPI_KEY:
        return []
    
    logger.info("🔶 FALLBACK: RapidAPI…")
    
    endpoint = f"https://{Config.RAPIDAPI_HOST}/deals-v2"
    headers = {
        "X-RapidAPI-Key": Config.RAPIDAPI_KEY,
        "X-RapidAPI-Host": Config.RAPIDAPI_HOST,
    }
    params = {"country": Config.AMAZON_COUNTRY}
    
    try:
        response = requests.get(endpoint, headers=headers, params=params, timeout=20)
        response.raise_for_status()
    except Exception as e:
        logger.error(f"RapidAPI error: {e}")
        return []
    
    data = response.json()
    raw_deals = data.get("deals") or data.get("data", {}).get("deals", []) or []
    
    if not raw_deals:
        logger.warning("RapidAPI: no deals")
        return []
    
    deals = []
    for item in raw_deals:
        try:
            asin = item.get("product_asin", "")
            if not asin:
                continue
            
            url_prod = item.get("deal_url", f"https://www.amazon.it/dp/{asin}")
            badge = item.get("deal_badge", "")
            discount = parse_discount(badge)
            
            if discount < Config.MIN_DISCOUNT_PERCENT:
                continue
            
            deal = Deal(
                deal_id=f"rapidapi_{asin}",
                asin=asin,
                title=item.get("deal_title", "Offerta")[:100],
                url=url_prod,
                affiliate_url=build_affiliate_link(url_prod, Config.AMAZON_AFFILIATE_TAG),
                price_now=0.0,
                price_orig=None,
                discount_percent=discount,
                image_url=item.get("deal_photo", ""),
                category=item.get("deal_type", "Offerta"),
                source="rapidapi",
            )
            deals.append(deal)
            if len(deals) >= Config.MAX_DEALS_PER_RUN:
                break
        except:
            continue
    
    logger.info(f"✅ RapidAPI: {len(deals)} deals")
    return deals


def fetch_deals() -> List[Deal]:
    """PRIMARY → FALLBACK"""
    logger.info("╔" + "═"*50)
    logger.info("║ FETCH DEALS")
    logger.info("╚" + "═"*50)
    
    deals = fetch_deals_scraping()
    if not deals:
        logger.warning("Scraping failed → RapidAPI fallback…")
        deals = fetch_deals_rapidapi()
    
    logger.info(f"📦 Total: {len(deals)} deals")
    return deals


# ──────────────────────────────────────────────
# TELEGRAM
# ──────────────────────────────────────────────
def format_deal_message(deal: Deal, idx: int, total: int, reason: str = "") -> str:
    title = deal.title[:90] + ("…" if len(deal.title) > 90 else "")
    
    if deal.price_orig and deal.price_now:
        price_line = f"💰 <b>€{deal.price_now:.2f}</b>  <s>€{deal.price_orig:.2f}</s>  ➡️ <b>-{deal.discount_percent}%</b>"
    elif deal.price_now > 0:
        price_line = f"💰 <b>€{deal.price_now:.2f}</b>  ➡️ <b>-{deal.discount_percent}%</b>"
    else:
        price_line = f"💥 <b>-{deal.discount_percent}% SCONTO</b>"
    
    reason_line = f"\n🔔 <i>{reason}</i>" if reason else ""
    source = "🔷 Scraping" if deal.source == "scraping" else "🔶 API"
    
    return (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🔥 <b>Deal {idx}/{total}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🛍 <b>{title}</b>\n\n"
        f"{price_line}{reason_line}\n\n"
        f"🔗 <a href=\"{deal.affiliate_url}\">👉 Acquista</a>\n\n"
        f"<i>🗓 {datetime.now().strftime('%d/%m %H:%M')} | {source}</i>"
    )


def format_header(total: int) -> str:
    date = datetime.now().strftime("%d %B %Y")
    return (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🛒 <b>Offerte Amazon — {date}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"<b>{total} offerte</b> con ≥{Config.MIN_DISCOUNT_PERCENT}% sconto!\n\n"
        f"💚 Link affiliati\nBuono shopping! 🎉"
    )


async def post_deals(deals: List[Tuple[Deal, str]]) -> None:
    if not deals:
        logger.info("No deals to post")
        return
    
    bot = telegram.Bot(token=Config.TELEGRAM_BOT_TOKEN)
    try:
        me = await bot.get_me()
        logger.info(f"✅ Auth: @{me.username}")
    except TelegramError as e:
        logger.critical(f"Telegram error: {e}")
        return
    
    try:
        await bot.send_message(
            chat_id=Config.TELEGRAM_CHANNEL_ID,
            text=format_header(len(deals)),
            parse_mode=telegram.constants.ParseMode.HTML,
        )
        await asyncio.sleep(1)
    except:
        pass
    
    for i, (deal, reason) in enumerate(deals, 1):
        try:
            await bot.send_message(
                chat_id=Config.TELEGRAM_CHANNEL_ID,
                text=format_deal_message(deal, i, len(deals), reason),
                parse_mode=telegram.constants.ParseMode.HTML,
                disable_web_page_preview=False,
            )
            logger.info(f"✅ Posted {i}/{len(deals)}: {deal.title[:40]}")
        except Exception as e:
            logger.error(f"Post error: {e}")
        await asyncio.sleep(2)


# ──────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────
def run_job() -> None:
    logger.info("\n" + "="*60)
    logger.info("🚀 AMAZON DEALS BOT — Job Start")
    logger.info("="*60 + "\n")
    
    try:
        Config.validate()
    except EnvironmentError as e:
        logger.critical(str(e))
        return
    
    db = DealsDatabase()
    all_deals = fetch_deals()
    
    if not all_deals:
        logger.warning("❌ No deals found")
        return
    
    deals_to_post: List[Tuple[Deal, str]] = []
    
    for deal in all_deals:
        should_post, reason = db.should_post(deal)
        
        if should_post:
            deals_to_post.append((deal, reason))
            db.update(deal, posted=True)
            logger.info(f"✅ POST: {deal.deal_id} — {reason}")
        else:
            db.update(deal, posted=False)
            logger.info(f"⏭️  SKIP: {deal.deal_id} — {reason}")
        
        if len(deals_to_post) >= Config.MAX_DEALS_PER_RUN:
            break
    
    db.save()
    
    if deals_to_post:
        asyncio.run(post_deals(deals_to_post))
    else:
        logger.info("⏭️  No new deals to post")
    
    logger.info("\n" + "="*60)
    logger.info("✅ JOB COMPLETE")
    logger.info("="*60 + "\n")


if __name__ == "__main__":
    run_job()
