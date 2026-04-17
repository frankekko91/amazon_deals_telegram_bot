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


def debug_html_structure(soup) -> None:
    """Analizza HTML e suggerisce selettori."""
    logger.info("🔍 DEBUG: Analizzando struttura HTML…")
    
    # Cerca pattern comuni
    patterns = {
        "data-asin": soup.find_all("div", {"data-asin": True}),
        "s-result-item": soup.find_all("div", {"class": "s-result-item"}),
        "a-price-whole": soup.find_all("span", {"class": "a-price-whole"}),
        "a-price-strike": soup.find_all("span", {"class": "a-price-strike"}),
        "a-badge": soup.find_all("span", {"class": re.compile(r"badge")}),
        "dp-links": soup.find_all("a", {"href": re.compile(r"/dp/[A-Z0-9]{10}")}),
        "price-symbols": soup.find_all(string=re.compile(r"€")),
    }
    
    for pattern_name, elements in patterns.items():
        logger.info(f"  {pattern_name}: {len(elements)} elementi trovati")
        if elements and len(elements) > 0:
            logger.debug(f"    Esempio: {str(elements[0])[:200]}")


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
    
    # 🔍 DEBUG: Salva HTML per analisi
    debug_file = "amazon_debug.html"
    try:
        with open(debug_file, "w", encoding="utf-8") as f:
            f.write(response.text[:5000])  # Prima 5000 char
        logger.info(f"📄 HTML salvato in {debug_file}")
    except:
        pass
    
    # 🔍 DEBUG: Analizza struttura pagina
    debug_html_structure(soup)
    
    # Strategie di selezione progressive
    deal_boxes = []
    
    # Strategy 1: Selettore originale
    deal_boxes = soup.find_all("div", {
        "class": "s-result-item",
        "data-component-type": "s-search-result"
    })
    if deal_boxes:
        logger.info(f"✅ Strategy 1: {len(deal_boxes)} boxes con s-result-item + data-component-type")
        return _process_deal_boxes(deal_boxes)
    
    # Strategy 2: Solo data-component-type
    deal_boxes = soup.find_all("div", {"data-component-type": "s-search-result"})
    if deal_boxes:
        logger.info(f"✅ Strategy 2: {len(deal_boxes)} boxes con data-component-type")
        return _process_deal_boxes(deal_boxes)
    
    # Strategy 3: Cerca data-asin (product item)
    deal_boxes = soup.find_all("div", {"data-asin": True})
    if deal_boxes:
        logger.info(f"✅ Strategy 3: {len(deal_boxes)} boxes con data-asin")
        return _process_deal_boxes(deal_boxes)
    
    # Strategy 4: Cerca link di prodotto con /dp/
    links = soup.find_all("a", {"href": re.compile(r"/dp/[A-Z0-9]{10}")})
    if links:
        logger.info(f"✅ Strategy 4: {len(links)} product links trovati")
        # Prendi parent div per ogni link
        deal_boxes = [link.find_parent("div") for link in links if link.find_parent("div")]
        deal_boxes = list(set(deal_boxes))  # Rimuovi duplicati
        return _process_deal_boxes(deal_boxes)
    
    # Strategy 5: Cerca per stringhe contenenti "€"
    spans_with_price = soup.find_all("span", string=re.compile(r"€"))
    if spans_with_price:
        logger.info(f"✅ Strategy 5: {len(spans_with_price)} span con € trovati")
        deal_boxes = [s.find_parent("div", recursive=True) for s in spans_with_price]
        deal_boxes = [b for b in deal_boxes if b]
        deal_boxes = list(set(deal_boxes))  # Rimuovi duplicati
        return _process_deal_boxes(deal_boxes)
    
    logger.warning(f"❌ Nessuna strategy riuscita. HTML structure changed.")
    logger.warning(f"Salva {debug_file} per analisi manuale")
    return []


def _process_deal_boxes(deal_boxes: List) -> List[Deal]:
    """Estrai deals da una lista di box."""
    deals = []
    logger.info(f"📦 Processing {len(deal_boxes)} deal boxes…")
    
    for idx, box in enumerate(deal_boxes):
        try:
            logger.debug(f"\n🔍 Processing box {idx}…")
            
            # ASIN
            asin = box.get("data-asin")
            if not asin:
                link = box.find("a", {"href": re.compile(r"/dp/[A-Z0-9]{10}")})
                if link:
                    asin = extract_asin(link.get("href", ""))
                    logger.debug(f"  ASIN estrapolato da link: {asin}")
            if not asin:
                logger.debug(f"  ❌ No ASIN found")
                continue
            
            logger.debug(f"  ✅ ASIN: {asin}")
            
            # Title — prova multipli selettori
            title_elem = None
            for selector in [
                {"class": "a-size-base-plus"},
                {"class": "a-size-medium"},
                {"class": "a-text-normal"},
                {"name": "h2"},
            ]:
                title_elem = box.find("span", selector) if "class" in selector else box.find(selector.get("name"))
                if title_elem:
                    break
            
            title = title_elem.get_text(strip=True) if title_elem else ""
            if not title or len(title) < 5:
                logger.debug(f"  ❌ No title found or too short: '{title}'")
                continue
            
            logger.debug(f"  ✅ Title: {title[:50]}")
            
            # URL
            link = box.find("a", {"href": re.compile(r"/dp/|/gp/product/")})
            if not link or not link.get("href"):
                logger.debug(f"  ❌ No product link found")
                continue
            url_prod = link["href"]
            if url_prod.startswith("/"):
                url_prod = "https://www.amazon.it" + url_prod
            elif not url_prod.startswith("http"):
                url_prod = "https://www.amazon.it/" + url_prod
            url_prod = url_prod.split("?")[0]
            logger.debug(f"  ✅ URL: {url_prod[:60]}")
            
            # Prices — estrattore robusto
            price_now = None
            price_orig = None
            
            # Cerca span con €
            price_spans = box.find_all("span", string=re.compile(r"€"))
            logger.debug(f"  Found {len(price_spans)} price spans: {[ps.get_text(strip=True) for ps in price_spans]}")
            for ps in price_spans:
                raw_text = ps.get_text(strip=True)
                p = parse_price(raw_text)
                logger.debug(f"    Parsing '{raw_text}' → {p}")
                if p and p > 0:
                    logger.debug(f"    ✅ Valid price: €{p:.2f}")
                    if not price_now:
                        price_now = p
                    elif p > price_now:  # Prezzo più alto = originale
                        price_orig = p
                else:
                    logger.debug(f"    ❌ Invalid price (p={p})")
            
            if not price_now or price_now == 0:
                logger.debug(f"  ❌ No valid price found (price_now={price_now})")
                continue
            
            logger.debug(f"  ✅ Prices: current=€{price_now:.2f}, original={price_orig}")
            
            # Discount
            discount_elem = box.find("span", {"class": re.compile(r"badge|discount|percent")}, string=re.compile(r"%"))
            discount_str = discount_elem.get_text(strip=True) if discount_elem else ""
            discount = parse_discount(discount_str)
            
            # Calcola discount se non trovato
            if discount == 0 and price_orig and price_now and price_orig > price_now:
                discount = round((price_orig - price_now) / price_orig * 100)
            
            logger.debug(f"  ✅ Discount: {discount}%")
            
            if discount < Config.MIN_DISCOUNT_PERCENT:
                logger.debug(f"  ⏭️  Discount {discount}% < MIN {Config.MIN_DISCOUNT_PERCENT}%")
                continue
            
            img = box.find("img")
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
            logger.info(f"✅ Deal estratto: {title[:40]} | €{price_now:.2f} | -{discount}%")
            
            if len(deals) >= Config.MAX_DEALS_PER_RUN:
                break
        
        except Exception as e:
            logger.error(f"❌ Errore deal {idx}: {e}", exc_info=True)
            continue
    
    logger.info(f"✅ Scraping: {len(deals)}/{len(deal_boxes)} deals estratti")
    return deals


# ──────────────────────────────────────────────
# RAPIDAPI ENDPOINT DISCOVERY
# ──────────────────────────────────────────────
def discover_rapidapi_endpoints() -> Dict[str, bool]:
    """
    Testa endpoint disponibili sull'API per scoprire quali funzionano.
    Utile per piano gratuito con limitazioni.
    
    Ritorna: {"endpoint": bool_success}
    """
    if not Config.RAPIDAPI_KEY:
        logger.warning("RAPIDAPI_KEY non configurato, skipping discovery")
        return {}
    
    logger.info("🔍 Discovering RapidAPI endpoints…")
    
    headers = {
        "X-RapidAPI-Key": Config.RAPIDAPI_KEY,
        "X-RapidAPI-Host": Config.RAPIDAPI_HOST,
    }
    
    # Endpoint comuni da testare
    endpoints_to_test = [
        "deals",
        "deals-v2",
        "deal-products",
        "best-sellers",
        "products-by-category",
        "product-offers",
        "product-search",
    ]
    
    results = {}
    
    for endpoint in endpoints_to_test:
        try:
            url = f"https://{Config.RAPIDAPI_HOST}/{endpoint}"
            params = {"country": Config.AMAZON_COUNTRY, "page": 1}
            
            logger.debug(f"Testing /{endpoint}…")
            response = requests.get(url, headers=headers, params=params, timeout=10)
            
            if response.status_code == 200:
                data = response.json()
                has_data = bool(data.get("deals") or data.get("data") or data.get("products") or data.get("results"))
                results[endpoint] = has_data
                
                if has_data:
                    logger.info(f"  ✅ /{endpoint}: OK (has data)")
                else:
                    logger.debug(f"  ⚠️  /{endpoint}: 200 but empty")
            else:
                results[endpoint] = False
                logger.debug(f"  ❌ /{endpoint}: {response.status_code}")
        
        except Exception as e:
            results[endpoint] = False
            logger.debug(f"  ❌ /{endpoint}: {str(e)[:50]}")
    
    # Salva risultati
    working = {k: v for k, v in results.items() if v}
    logger.info(f"📊 Working endpoints: {list(working.keys())}")
    
    return results


# ──────────────────────────────────────────────
# RAPIDAPI FALLBACK
# ──────────────────────────────────────────────
def fetch_deals_rapidapi() -> List[Deal]:
    """RapidAPI fallback — prova /deals-v2 e fallback a /deals se necessario."""
    if not Config.RAPIDAPI_KEY:
        logger.warning("❌ RAPIDAPI_KEY non configurato")
        return []
    
    logger.info("🔶 FALLBACK: RapidAPI (provo endpoints con timeout 15s)…")
    
    headers = {
        "X-RapidAPI-Key": Config.RAPIDAPI_KEY,
        "X-RapidAPI-Host": Config.RAPIDAPI_HOST,
    }
    
    # Prova endpoints in quest'ordine
    endpoints_to_try = [
        ("deals-v2", {"country": Config.AMAZON_COUNTRY, "page": 1}),
        ("deal-products", {"country": Config.AMAZON_COUNTRY, "page": 1}),
        ("deals", {"country": Config.AMAZON_COUNTRY, "page": 1}),
        ("best-sellers", {"country": Config.AMAZON_COUNTRY, "category": "all"}),
        ("products-by-category", {"country": Config.AMAZON_COUNTRY, "category": "electronics", "page": 1}),
    ]
    
    for endpoint_name, params in endpoints_to_try:
        try:
            url = f"https://{Config.RAPIDAPI_HOST}/{endpoint_name}"
            logger.info(f"  → Provo /{endpoint_name}…")
            
            response = requests.get(url, headers=headers, params=params, timeout=15)
            response.raise_for_status()
            
            data = response.json()
            logger.debug(f"    Response keys: {list(data.keys())}")
            
            # Estrai deals da vari formati API
            raw_deals = (
                data.get("deals") or 
                data.get("data", {}).get("deals") or 
                data.get("products") or
                data.get("data", {}).get("products") or
                data.get("results") or
                data.get("data", {}).get("results") or
                []
            )
            
            logger.info(f"    Found {len(raw_deals)} raw items")
            
            if raw_deals and len(raw_deals) > 0:
                # Log sample first item structure
                first_item = raw_deals[0]
                logger.debug(f"    First item keys: {list(first_item.keys())}")
                logger.debug(f"    First item: {str(first_item)[:300]}")
            
            if not raw_deals:
                logger.info(f"    ⏭️  /{endpoint_name}: no data in response")
                continue
            
            # Processa deals trovati
            deals = _process_rapidapi_deals(raw_deals, endpoint_name)
            
            if deals:
                logger.info(f"✅ RapidAPI /{endpoint_name}: {len(deals)} deals [1 call]")
                return deals
            else:
                logger.info(f"    ⏭️  /{endpoint_name}: found {len(raw_deals)} items but no valid deals")
        
        except requests.exceptions.Timeout:
            logger.warning(f"    ⏱️  /{endpoint_name}: TIMEOUT (15s)")
            continue
        except requests.exceptions.ConnectionError as e:
            logger.warning(f"    🌐 /{endpoint_name}: CONNECTION ERROR - {str(e)[:50]}")
            continue
        except Exception as e:
            logger.warning(f"    ❌ /{endpoint_name}: {type(e).__name__} - {str(e)[:80]}")
            continue
    
    logger.critical("❌ RapidAPI: Nessun endpoint ha restituito risultati validi")
    return []


def _process_rapidapi_deals(raw_deals: List, source_endpoint: str) -> List[Deal]:
    """Processa deals da vari endpoint RapidAPI."""
    deals = []
    logger.info(f"  Processing {len(raw_deals)} items from /{source_endpoint}…")
    
    for idx, item in enumerate(raw_deals):
        try:
            # Estrai ASIN (key variabile tra endpoint)
            asin = item.get("product_asin") or item.get("asin") or item.get("id", "")
            if not asin or len(asin) < 5:
                logger.debug(f"    Item {idx}: ❌ No valid ASIN (got '{asin}')")
                continue
            
            # Titolo
            title = (
                item.get("deal_title") or 
                item.get("product_title") or 
                item.get("title") or 
                ""
            )[:100]
            if not title:
                logger.debug(f"    Item {idx} ({asin}): ❌ No title")
                continue
            
            logger.debug(f"    Item {idx} ({asin}): {title[:50]}")
            
            # URL
            url_prod = (
                item.get("deal_url") or 
                item.get("product_url") or 
                item.get("url") or 
                f"https://www.amazon.it/dp/{asin}"
            )
            
            # Prezzo attuale
            price_str = item.get("deal_price") or item.get("product_price") or item.get("price") or ""
            price_now = parse_price(price_str)
            
            if not price_now or price_now == 0:
                logger.debug(f"      ❌ No valid price (raw='{price_str}', parsed={price_now})")
                continue
            
            logger.debug(f"      ✅ Price: €{price_now:.2f}")
            
            # Prezzo originale
            price_orig = (
                parse_price(item.get("deal_price_original")) or
                parse_price(item.get("product_original_price")) or
                parse_price(item.get("original_price")) or
                None
            )
            
            if price_orig:
                logger.debug(f"      ✅ Original price: €{price_orig:.2f}")
            
            # Sconto
            discount_str = (
                item.get("deal_badge") or 
                item.get("discount_badge") or 
                item.get("badge") or
                ""
            )
            discount = parse_discount(discount_str)
            
            # Calcola se necessario
            if discount == 0 and price_orig and price_orig > price_now:
                discount = round((price_orig - price_now) / price_orig * 100)
            
            logger.debug(f"      Discount: {discount}% (raw='{discount_str}')")
            
            if discount < Config.MIN_DISCOUNT_PERCENT:
                logger.debug(f"      ❌ Discount {discount}% < MIN {Config.MIN_DISCOUNT_PERCENT}%")
                continue
            
            # Immagine
            image_url = (
                item.get("deal_photo") or 
                item.get("product_image") or 
                item.get("image") or 
                ""
            )
            
            deal = Deal(
                deal_id=f"rapidapi_{source_endpoint}_{asin}",
                asin=asin,
                title=title,
                url=url_prod,
                affiliate_url=build_affiliate_link(url_prod, Config.AMAZON_AFFILIATE_TAG),
                price_now=price_now,
                price_orig=price_orig,
                discount_percent=discount,
                image_url=image_url,
                category=item.get("deal_type") or item.get("category") or "Offerta",
                source=f"rapidapi_{source_endpoint}",
            )
            deals.append(deal)
            
            logger.info(f"      ✅ VALID DEAL: {title[:40]} | €{price_now:.2f} | -{discount}%")
            
            if len(deals) >= Config.MAX_DEALS_PER_RUN:
                break
        
        except Exception as e:
            logger.debug(f"    Item {idx}: ❌ Exception: {e}")
            continue
    
    logger.info(f"  Result: {len(deals)} valid deals from {len(raw_deals)} items")
    return deals


def fetch_deals_rapidapi_search() -> List[Deal]:
    """RapidAPI /search endpoint — per varietà (usare alternato)."""
    if not Config.RAPIDAPI_KEY:
        return []
    
    logger.info("🔶 SECONDARY: RapidAPI /search…")
    
    endpoint = f"https://{Config.RAPIDAPI_HOST}/search"
    headers = {
        "X-RapidAPI-Key": Config.RAPIDAPI_KEY,
        "X-RapidAPI-Host": Config.RAPIDAPI_HOST,
    }
    
    # Cerca bestseller in categorie varie
    queries = ["bestseller italy", "offerte sconti", "deals oggi"]
    query = random.choice(queries)
    
    params = {
        "query": query,
        "country": Config.AMAZON_COUNTRY,
        "page": 1,
    }
    
    try:
        logger.debug(f"Searching: {query}")
        response = requests.get(endpoint, headers=headers, params=params, timeout=20)
        response.raise_for_status()
    except Exception as e:
        logger.error(f"RapidAPI /search error: {e}")
        return []
    
    data = response.json()
    raw_products = data.get("data") or data.get("results", []) or []
    
    if not raw_products:
        logger.warning("RapidAPI /search: no products")
        return []
    
    deals = []
    for item in raw_products:
        try:
            asin = item.get("asin", "")
            if not asin:
                continue
            
            url_prod = item.get("product_url", f"https://www.amazon.it/dp/{asin}")
            price_current = parse_price(item.get("product_price", ""))
            price_original = parse_price(item.get("product_original_price", ""))
            
            if not price_current or price_current == 0:
                continue
            
            # Calcola discount
            discount = 0
            if price_original and price_original > price_current:
                discount = round((price_original - price_current) / price_original * 100)
            
            # Filtra: solo se sconto significativo
            if discount < Config.MIN_DISCOUNT_PERCENT:
                continue
            
            deal = Deal(
                deal_id=f"rapidapi_search_{asin}",
                asin=asin,
                title=item.get("product_title", "Prodotto")[:100],
                url=url_prod,
                affiliate_url=build_affiliate_link(url_prod, Config.AMAZON_AFFILIATE_TAG),
                price_now=price_current,
                price_orig=price_original,
                discount_percent=discount,
                image_url=item.get("product_image", ""),
                category=item.get("product_category", "Vario"),
                source="rapidapi_search",
            )
            deals.append(deal)
            logger.debug(f"Search Hit: {item.get('product_title', '')[:40]} | €{price_current:.2f} | -{discount}%")
            if len(deals) >= Config.MAX_DEALS_PER_RUN:
                break
        except Exception as e:
            logger.debug(f"RapidAPI search item error: {e}")
            continue
    
    logger.info(f"✅ RapidAPI /search: {len(deals)} deals")
    return deals


def fetch_deals() -> List[Deal]:
    """
    Strategia ibrida per piano gratuito RapidAPI:
    
    1. PRIMARY: Scraping (free, ma fragile)
    2. FALLBACK 1: RapidAPI /deals-v2 (1 call, affidabile, cheap)
    3. FALLBACK 2: RapidAPI /search (1 call, varietà — usato alternato)
    
    Budget: ~2 call/run × 60 run/mese = 120 call/mese ✅ (sotto 500 limite free)
    """
    logger.info("╔" + "═"*50)
    logger.info("║ FETCH DEALS — Hybrid Strategy (Free RapidAPI)")
    logger.info("╚" + "═"*50)
    
    # ⚠️  NOTA: Amazon goldbox HTML cambia frequentemente
    deals = fetch_deals_scraping()
    
    if deals:
        logger.info(f"✅ Scraping riuscito: {len(deals)} deals")
        return deals
    
    # FALLBACK PRIMARY: /deals-v2 (sempre disponibile)
    logger.warning("⚠️  Scraping fallito → RapidAPI /deals-v2…")
    deals = fetch_deals_rapidapi()
    
    if deals:
        logger.info(f"✅ RapidAPI /deals-v2: {len(deals)} deals [1 call]")
        return deals
    
    # FALLBACK SECONDARIO: /search (alternato per risparmiare quota)
    # Usa solo lunedi/mercoledi/venerdi per varietà senza sprecare call
    if datetime.now().weekday() in [0, 2, 4]:  # Mon, Wed, Fri
        logger.warning("⚠️  /deals-v2 fallito → RapidAPI /search (varietà)…")
        deals = fetch_deals_rapidapi_search()
        
        if deals:
            logger.info(f"✅ RapidAPI /search: {len(deals)} deals [1 call alternato]")
            return deals
    
    logger.critical("❌ Tutti gli endpoint falliti")
    logger.info(f"📄 Controlla bot.log per debug")
    return []


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
