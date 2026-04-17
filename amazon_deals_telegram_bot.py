"""
🚀 Amazon Deals → Telegram Affiliate Bot — VERSIONE FINALE
===========================================================

Architettura:
  1. PRIMARY:   Web scraping diretto da amazon.it/gp/goldbox (BeautifulSoup)
  2. FALLBACK:  RapidAPI (se scraping fallisce)
  3. DEDUP:     Database JSON con prezzo storico
     - Mostra deal SE: primo avvistamento OU prezzo sceso
     - Non mostra SE: stesso prezzo della volta precedente

Setup:
  1. pip install requests beautifulsoup4 python-telegram-bot python-dotenv
  2. Copia .env.example → .env e riempi credenziali
  3. python amazon_deals_bot_final.py

GitHub Actions: corre una volta e termina (no while loop)
Tempo run: ~15-20 sec (scraping) o ~30 sec (fallback API)
Minuti/mese: ~15 min (90% discount vs 2000 min vecchio)
"""

import os
import re
import json
import logging
import requests
from datetime import datetime, timedelta
from urllib.parse import urlencode, parse_qs, urlparse, urlunparse
from typing import Optional, Dict, List
from dataclasses import dataclass, asdict
from dotenv import load_dotenv

import telegram
from telegram.error import TelegramError
import asyncio

try:
    from bs4 import BeautifulSoup
    HAS_BEAUTIFULSOUP = True
except ImportError:
    HAS_BEAUTIFULSOUP = False

# ──────────────────────────────────────────────
# SETUP
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
# CONFIG
# ──────────────────────────────────────────────
class Config:
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHANNEL_ID: str = os.getenv("TELEGRAM_CHANNEL_ID", "")
    AMAZON_AFFILIATE_TAG: str = os.getenv("AMAZON_AFFILIATE_TAG", "")
    
    RAPIDAPI_KEY: str = os.getenv("RAPIDAPI_KEY", "")
    RAPIDAPI_HOST: str = os.getenv("RAPIDAPI_HOST", "real-time-amazon-data.p.rapidapi.com")
    
    MAX_DEALS_PER_RUN: int = int(os.getenv("MAX_DEALS_PER_RUN", "5"))
    MIN_DISCOUNT_PERCENT: int = int(os.getenv("MIN_DISCOUNT_PERCENT", "20"))
    AMAZON_COUNTRY: str = os.getenv("AMAZON_COUNTRY", "IT")
    
    # DATABASE STORICO PREZZI
    DEALS_DB_FILE: str = "deals_history.json"
    DB_RETENTION_DAYS: int = 7  # Pulisci history dopo 7 giorni
    
    @classmethod
    def validate(cls) -> None:
        required = {
            "TELEGRAM_BOT_TOKEN": cls.TELEGRAM_BOT_TOKEN,
            "TELEGRAM_CHANNEL_ID": cls.TELEGRAM_CHANNEL_ID,
            "AMAZON_AFFILIATE_TAG": cls.AMAZON_AFFILIATE_TAG,
        }
        missing = [k for k, v in required.items() if not v]
        if missing:
            raise EnvironmentError(
                f"Missing env vars: {', '.join(missing)}"
            )


# ──────────────────────────────────────────────
# DATA MODELS
# ──────────────────────────────────────────────
@dataclass
class Deal:
    """Modello di un singolo deal."""
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
    source: str  # "scraping" o "rapidapi"
    timestamp: str = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now().isoformat()


@dataclass
class PriceRecord:
    """Record storico di un prodotto."""
    deal_id: str
    asin: str
    title: str
    price_now: float
    timestamp: str
    price_prev: Optional[float] = None
    posted: bool = False


# ──────────────────────────────────────────────
# DATABASE STORICO PREZZI
# ──────────────────────────────────────────────
class DealsDatabase:
    """Gestisce il database storico deal_id → prezzo."""
    
    def __init__(self, filepath: str = Config.DEALS_DB_FILE):
        self.filepath = filepath
        self.data: Dict[str, PriceRecord] = {}
        self.load()
        self.cleanup_old()
    
    def load(self) -> None:
        """Carica il database da JSON."""
        if not os.path.exists(self.filepath):
            self.data = {}
            return
        
        try:
            with open(self.filepath, "r") as f:
                raw = json.load(f)
            self.data = {
                k: PriceRecord(**v) for k, v in raw.items()
            }
            logger.info(f"Caricato DB con {len(self.data)} record storici")
        except Exception as e:
            logger.warning(f"Errore loading DB: {e} — ricomincia da zero")
            self.data = {}
    
    def save(self) -> None:
        """Salva il database su JSON."""
        try:
            with open(self.filepath, "w") as f:
                json.dump(
                    {k: asdict(v) for k, v in self.data.items()},
                    f, indent=2
                )
        except Exception as e:
            logger.error(f"Errore salvando DB: {e}")
    
    def cleanup_old(self) -> None:
        """Rimuove record più vecchi di RETENTION_DAYS."""
        cutoff = datetime.now() - timedelta(days=Config.DB_RETENTION_DAYS)
        to_remove = []
        
        for deal_id, record in self.data.items():
            try:
                record_date = datetime.fromisoformat(record.timestamp)
                if record_date < cutoff:
                    to_remove.append(deal_id)
            except Exception:
                pass
        
        for deal_id in to_remove:
            del self.data[deal_id]
        
        if to_remove:
            logger.info(f"Puliti {len(to_remove)} record vecchi da DB")
            self.save()
    
    def should_post(self, deal: Deal) -> tuple[bool, Optional[str]]:
        """
        Decide se postare il deal.
        Torna: (should_post, reason)
        
        Logica:
        - Nuovo deal → POST
        - Prezzo sceso → POST
        - Stesso prezzo → NO POST
        """
        if deal.deal_id not in self.data:
            return True, "Nuovo deal"
        
        record = self.data[deal.deal_id]
        price_prev = record.price_prev or record.price_now
        
        # Prezzo è sceso (almeno €0.01)?
        if deal.price_now < price_prev - 0.01:
            price_drop = price_prev - deal.price_now
            return True, f"Prezzo sceso di €{price_drop:.2f}"
        
        return False, "Stesso prezzo della volta scorsa"
    
    def update(self, deal: Deal, posted: bool = False) -> None:
        """Aggiorna il record di un deal."""
        if deal.deal_id in self.data:
            prev = self.data[deal.deal_id]
            self.data[deal.deal_id] = PriceRecord(
                deal_id=deal.deal_id,
                asin=deal.asin,
                title=deal.title,
                price_now=deal.price_now,
                timestamp=deal.timestamp,
                price_prev=prev.price_now,  # Salva il prezzo precedente
                posted=posted,
            )
        else:
            self.data[deal.deal_id] = PriceRecord(
                deal_id=deal.deal_id,
                asin=deal.asin,
                title=deal.title,
                price_now=deal.price_now,
                timestamp=deal.timestamp,
                price_prev=None,
                posted=posted,
            )


# ──────────────────────────────────────────────
# AFFILIATE LINK BUILDER
# ──────────────────────────────────────────────
def build_affiliate_link(amazon_url: str, tag: str) -> str:
    """Aggiunge tag affiliato a URL Amazon."""
    if not amazon_url:
        return ""
    
    parsed = urlparse(amazon_url)
    params = parse_qs(parsed.query, keep_blank_values=True)
    params["tag"] = [tag]
    params.pop("ref", None)
    params.pop("linkCode", None)
    
    new_query = urlencode({k: v[0] for k, v in params.items()})
    return urlunparse(parsed._replace(query=new_query))


def extract_asin(url: str) -> Optional[str]:
    """Estrae ASIN da URL Amazon."""
    match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})", url)
    return match.group(1) if match else None


def parse_price(price_str: str) -> Optional[float]:
    """Parsa prezzo da stringa (es. '€ 34,99' o '$34.99')."""
    if not price_str:
        return None
    try:
        # Rimuovi simboli valuta e spazi
        clean = re.sub(r"[^\d,.]", "", price_str)
        # Gestisci separatori europei
        clean = clean.replace(".", "").replace(",", ".")
        return float(clean)
    except Exception:
        return None


# ──────────────────────────────────────────────
# SCRAPING BEAUTIFULSOUP (PRIMARY)
# ──────────────────────────────────────────────
def fetch_deals_scraping() -> List[Deal]:
    """
    Scrapa direttamente https://www.amazon.it/gp/goldbox
    con BeautifulSoup. METODO PRIMARY.
    """
    if not HAS_BEAUTIFULSOUP:
        logger.warning("BeautifulSoup non installato — salta scraping")
        return []
    
    logger.info("🔷 PRIMARY: Inizio scraping da amazon.it/gp/goldbox…")
    
    url = "https://www.amazon.it/gp/goldbox"
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept-Language": "it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    
    try:
        response = requests.get(url, headers=headers, timeout=20)
        response.raise_for_status()
    except Exception as e:
        logger.error(f"Errore scraping: {e}")
        return []
    
    try:
        soup = BeautifulSoup(response.content, "html.parser")
    except Exception as e:
        logger.error(f"Errore parsing HTML: {e}")
        return []
    
    deals = []
    
    # Selettori CSS per i deal box (possono cambiare)
    deal_boxes = soup.find_all("div", {"data-component-type": "s-search-result"})
    
    if not deal_boxes:
        logger.warning("Nessun deal trovato — HTML potrebbe essere cambiato")
        return []
    
    logger.info(f"Trovati {len(deal_boxes)} deal box, parsing…")
    
    for box in deal_boxes:
        try:
            # Titolo
            title_elem = box.find("h2", {"class": "s-size-mini"})
            if not title_elem:
                title_elem = box.find("span", {"class": "a-size-base-plus"})
            title = title_elem.get_text(strip=True) if title_elem else "N/A"
            
            # Link prodotto
            link_elem = box.find("h2", {"class": "s-size-mini"})
            if link_elem:
                link_elem = link_elem.find_parent("a")
            if not link_elem:
                link_elem = box.find("a", {"class": "s-link"})
            
            url_prod = None
            if link_elem and link_elem.get("href"):
                url_prod = "https://www.amazon.it" + link_elem["href"]
            
            if not url_prod:
                continue
            
            asin = extract_asin(url_prod)
            if not asin:
                continue
            
            # Prezzo attuale
            price_elem = box.find("span", {"class": "a-price-whole"})
            price_str = price_elem.get_text(strip=True) if price_elem else ""
            price_now = parse_price(price_str)
            
            if price_now is None:
                continue
            
            # Prezzo originale (barrato)
            price_orig_elem = box.find("span", {"class": "a-price-strike"})
            price_orig_str = price_orig_elem.get_text(strip=True) if price_orig_elem else ""
            price_orig = parse_price(price_orig_str)
            
            # % Sconto
            discount_elem = box.find("span", {"class": "-badge-strikethrough"})
            discount_str = discount_elem.get_text(strip=True) if discount_elem else ""
            discount = _parse_discount(discount_str)
            
            if price_orig and price_now:
                discount = round((price_orig - price_now) / price_orig * 100)
            
            if discount < Config.MIN_DISCOUNT_PERCENT:
                continue
            
            # Immagine
            img_elem = box.find("img", {"class": "s-image"})
            image_url = img_elem.get("src", "") if img_elem else ""
            
            # Deal ID (usiamo ASIN come identificativo univoco)
            deal_id = f"scraping_{asin}"
            
            deal = Deal(
                deal_id=deal_id,
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
            
            if len(deals) >= Config.MAX_DEALS_PER_RUN:
                break
        
        except Exception as e:
            logger.debug(f"Errore parsing singolo deal: {e}")
            continue
    
    logger.info(f"✅ Scraping completato: {len(deals)} deal estratti")
    return deals


def _parse_discount(s: str) -> int:
    """Estrae % sconto da stringa."""
    if not s:
        return 0
    match = re.search(r"(\d+)\s*%", s)
    return int(match.group(1)) if match else 0


# ──────────────────────────────────────────────
# RAPIDAPI FALLBACK
# ──────────────────────────────────────────────
def fetch_deals_rapidapi() -> List[Deal]:
    """
    API RapidAPI come FALLBACK se scraping fallisce.
    """
    if not Config.RAPIDAPI_KEY:
        logger.warning("RapidAPI key non configurata — skip fallback")
        return []
    
    logger.info("🔷 FALLBACK: Tentativo con RapidAPI…")
    
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
        logger.error(f"Errore RapidAPI: {e}")
        return []
    
    data = response.json()
    raw_deals = (
        data.get("deals") or
        data.get("data", {}).get("deals", []) or
        (data if isinstance(data, list) else [])
    )
    
    if not raw_deals:
        logger.warning("RapidAPI non ha restituito deal")
        return []
    
    deals = []
    
    for item in raw_deals:
        try:
            asin = item.get("product_asin", "")
            if not asin:
                continue
            
            raw_url = item.get("deal_url", f"https://www.amazon.{_get_domain(Config.AMAZON_COUNTRY)}/dp/{asin}")
            
            badge = item.get("deal_badge", "")
            discount = _parse_discount(badge)
            
            if discount < Config.MIN_DISCOUNT_PERCENT:
                continue
            
            deal = Deal(
                deal_id=f"rapidapi_{asin}",
                asin=asin,
                title=item.get("deal_title", "Offerta Amazon")[:100],
                url=raw_url,
                affiliate_url=build_affiliate_link(raw_url, Config.AMAZON_AFFILIATE_TAG),
                price_now=0.0,  # RapidAPI non fornisce prezzo preciso
                price_orig=None,
                discount_percent=discount,
                image_url=item.get("deal_photo", ""),
                category=item.get("deal_type", "Offerta"),
                source="rapidapi",
            )
            deals.append(deal)
            
            if len(deals) >= Config.MAX_DEALS_PER_RUN:
                break
        
        except Exception as e:
            logger.debug(f"Errore parsing RapidAPI deal: {e}")
            continue
    
    logger.info(f"✅ RapidAPI completato: {len(deals)} deal estratti")
    return deals


def _get_domain(country: str) -> str:
    """Mappa country code → dominio Amazon."""
    mapping = {
        "IT": "it", "US": "com", "UK": "co.uk",
        "DE": "de", "FR": "fr", "ES": "es",
    }
    return mapping.get(country, "com")


# ──────────────────────────────────────────────
# MAIN FETCH
# ──────────────────────────────────────────────
def fetch_deals() -> List[Deal]:
    """
    Tenta PRIMARY (scraping), fallback a RapidAPI.
    """
    logger.info("╔" + "═"*50)
    logger.info("║ INIZIO FETCH DEAL")
    logger.info("╚" + "═"*50)
    
    # PRIMARY: Scraping
    deals = fetch_deals_scraping()
    
    # FALLBACK: RapidAPI
    if not deals:
        logger.warning("Scraping fallito — provo RapidAPI fallback…")
        deals = fetch_deals_rapidapi()
    
    logger.info(f"📦 Totale deal recuperati: {len(deals)}")
    return deals


# ──────────────────────────────────────────────
# MESSAGE FORMATTER
# ──────────────────────────────────────────────
def format_deal_message(deal: Deal, index: int, total: int, reason: str = "") -> str:
    """Formatta messaggio Telegram per un deal."""
    title = deal.title[:95] + ("…" if len(deal.title) > 95 else "")
    
    # Riga prezzi
    if deal.price_orig and deal.price_now:
        price_line = (
            f"💰 <b>€{deal.price_now:.2f}</b>  "
            f"<s>€{deal.price_orig:.2f}</s>  "
            f"➡️ <b>-{deal.discount_percent}%</b>"
        )
    elif deal.price_now > 0:
        price_line = (
            f"💰 <b>€{deal.price_now:.2f}</b>  "
            f"➡️ <b>-{deal.discount_percent}%</b>"
        )
    else:
        price_line = f"💥 <b>-{deal.discount_percent}% di sconto</b>"
    
    # Motivo (nuovo o prezzo sceso)
    reason_str = f"\n🔔 <i>{reason}</i>" if reason else ""
    
    # Sorgente
    source_emoji = "🔷" if deal.source == "scraping" else "🔶"
    
    lines = [
        f"━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"🔥 <b>Offerta {index}/{total}</b>",
        f"━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"",
        f"🛍 <b>{title}</b>",
        f"",
        price_line,
        reason_str,
        f"",
        f"🔗 <a href=\"{deal.affiliate_url}\">👉 Acquista su Amazon</a>",
        f"",
        f"<i>🗓 {datetime.now().strftime('%d/%m %H:%M')}  {source_emoji} {deal.source.upper()}</i>",
    ]
    
    return "\n".join(lines)


def format_header_message(total: int) -> str:
    """Header del batch."""
    date_str = datetime.now().strftime("%A %d %B %Y")
    return (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🛒 <b>Offerte Amazon — {date_str}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Ecco le migliori <b>{total} offerte</b> di oggi "
        f"con almeno <b>{Config.MIN_DISCOUNT_PERCENT}% di sconto</b>!\n\n"
        f"💚 Link affiliati → guadagni gratis per te!\n"
        f"Buono shopping! 🎉"
    )


# ──────────────────────────────────────────────
# TELEGRAM POSTER
# ──────────────────────────────────────────────
async def _send_message(bot: telegram.Bot, text: str) -> bool:
    """Invia messaggio a Telegram."""
    try:
        await bot.send_message(
            chat_id=Config.TELEGRAM_CHANNEL_ID,
            text=text,
            parse_mode=telegram.constants.ParseMode.HTML,
            disable_web_page_preview=False,
        )
        return True
    except TelegramError as e:
        logger.error(f"Errore Telegram: {e}")
        return False


async def post_deals_to_telegram(deals_to_post: List[tuple[Deal, str]]) -> None:
    """Posta i deal su Telegram."""
    if not deals_to_post:
        logger.info("Nessun deal da postare")
        return
    
    bot = telegram.Bot(token=Config.TELEGRAM_BOT_TOKEN)
    
    try:
        me = await bot.get_me()
        logger.info(f"✅ Autenticato come: @{me.username}")
    except TelegramError as e:
        logger.critical(f"Errore autenticazione Telegram: {e}")
        return
    
    # Header
    header = format_header_message(len(deals_to_post))
    await _send_message(bot, header)
    await asyncio.sleep(1)
    
    # Deal
    for i, (deal, reason) in enumerate(deals_to_post, start=1):
        msg = format_deal_message(deal, i, len(deals_to_post), reason)
        success = await _send_message(bot, msg)
        if success:
            logger.info(f"✅ Postato deal {i}/{len(deals_to_post)}: {deal.title[:50]}")
        await asyncio.sleep(2)


# ──────────────────────────────────────────────
# MAIN JOB
# ──────────────────────────────────────────────
def run_job() -> None:
    """Job principale — esecuzione monouso (NO while loop)."""
    logger.info("\n" + "="*60)
    logger.info("🚀 AMAZON DEALS BOT — Job Avviato")
    logger.info("="*60 + "\n")
    
    try:
        Config.validate()
    except EnvironmentError as e:
        logger.critical(str(e))
        return
    
    # Carica database storico
    db = DealsDatabase()
    
    # Fetch deals
    all_deals = fetch_deals()
    
    if not all_deals:
        logger.warning("❌ Nessun deal trovato da nessuna sorgente")
        return
    
    # Filtra: prendi solo deal da postare
    deals_to_post: List[tuple[Deal, str]] = []
    
    for deal in all_deals:
        should_post, reason = db.should_post(deal)
        
        if should_post:
            deals_to_post.append((deal, reason))
            db.update(deal, posted=True)
            logger.info(f"✅ {deal.deal_id}: {reason}")
        else:
            db.update(deal, posted=False)
            logger.info(f"⏭️  {deal.deal_id}: {reason}")
        
        if len(deals_to_post) >= Config.MAX_DEALS_PER_RUN:
            break
    
    # Salva DB
    db.save()
    
    # Posta su Telegram
    if deals_to_post:
        asyncio.run(post_deals_to_telegram(deals_to_post))
    else:
        logger.info("⏭️  Nessun deal nuovo da postare (tutti già visti)")
    
    logger.info("\n" + "="*60)
    logger.info("✅ JOB COMPLETATO")
    logger.info("="*60 + "\n")


# ──────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────
if __name__ == "__main__":
    run_job()
    # Script termina qui — niente while loop infinito!
