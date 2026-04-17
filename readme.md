# Amazon Deals Telegram Bot

A Python-based bot that automatically scrapes Amazon deals and posts them to a Telegram channel with affiliate links.

## Overview

This bot fetches Amazon deals from multiple sources (web scraping, RapidAPI) and posts them to a Telegram channel with:
- Product title and price information
- Discount percentage
- Affiliate links
- Interactive buttons (Buy, Save, Share)
- Smart deduplication based on historical prices

## Architecture

### Primary Data Sources

1. **Web Scraping (Primary)**
   - Selenium for JavaScript-rendered pages (when available)
   - BeautifulSoup for fallback static HTML parsing
   - Multiple URL strategies to maximize coverage

2. **RapidAPI (Fallback)**
   - Endpoint: real-time-amazon-data
   - Multiple endpoints called per run:
     - deals-v2 (pages 1-2)
     - best-sellers (multiple categories)
     - products-by-category
     - deal-products
   - Handles variable response formats

3. **Database**
   - JSON-based historical price tracking
   - Records kept for 30 days
   - Smart deduplication: posts only new deals or price drops

### Processing Pipeline

```
Fetch Deals
    |
    ├─> Scraping (Selenium + BeautifulSoup)
    |        |
    |        └─> (0+ deals)
    |
    └─> RapidAPI Multi-Endpoint
             |
             └─> (10-20 deals)
                  |
                  v
            Database Lookup
                  |
        ┌─────────┴──────────┐
        v                    v
    New Deal?          Price Drop?
        |                   |
        └─────────┬─────────┘
                  v
        Format + Post to Telegram
```

## Setup

### Prerequisites

- Python 3.8+
- pip (Python package manager)

### Installation

1. Clone the repository:
```bash
git clone <repository-url>
cd project-root
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

3. Create environment file:
```bash
cp .env.example .env
nano .env  # or your preferred editor
```

4. Configure `.env` with required variables (see Configuration section)

### Requirements File

Create `requirements.txt`:
```
requests>=2.28.0
beautifulsoup4>=4.11.0
python-telegram-bot>=20.0
python-dotenv>=0.20.0
selenium>=4.0
```

## Configuration

### Required Environment Variables

```bash
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHANNEL_ID=your_channel_id
AMAZON_AFFILIATE_TAG=your_affiliate_tag
RAPIDAPI_KEY=your_rapidapi_key
```

### Optional Environment Variables

```bash
# Maximum deals to post per run (default: 5)
MAX_DEALS_PER_RUN=5

# Minimum discount percentage to consider (default: 20)
MIN_DISCOUNT_PERCENT=20

# Amazon country code (default: IT)
AMAZON_COUNTRY=IT

# Force posting of all new deals, bypassing deduplication (default: false)
FORCE_POST_NEW_DEALS=false
```

### Getting Required Credentials

**Telegram Bot Token**
- Create bot via BotFather (@BotFather on Telegram)
- Copy the token provided

**Telegram Channel ID**
- Add bot to your channel as administrator
- Send a message and check bot logs for channel ID
- Or use: https://t.me/<your_channel> to find the ID

**Amazon Affiliate Tag**
- Register at Amazon Associates
- Create your associate tag (e.g., "yourname-20")

**RapidAPI Key**
- Register at https://rapidapi.com
- Subscribe to "real-time-amazon-data" API
- Copy your API key from dashboard

## Usage

### Running the Bot

Basic execution:
```bash
python amazon_deals_telegram_bot.py
```

Testing mode (force posting of all deals):
```bash
export FORCE_POST_NEW_DEALS=true
python amazon_deals_telegram_bot.py
```

Adjusting deal thresholds:
```bash
export MAX_DEALS_PER_RUN=10
export MIN_DISCOUNT_PERCENT=15
python amazon_deals_telegram_bot.py
```

### GitHub Actions Setup

1. Create `.github/workflows/amazon-deals.yml`:
```yaml
name: Amazon Deals Bot

on:
  schedule:
    - cron: '0 */6 * * *'  # Every 6 hours

jobs:
  fetch-and-post:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      - run: pip install -r requirements.txt
      - run: python amazon_deals_telegram_bot.py
        env:
          TELEGRAM_BOT_TOKEN: ${{ secrets.TELEGRAM_BOT_TOKEN }}
          TELEGRAM_CHANNEL_ID: ${{ secrets.TELEGRAM_CHANNEL_ID }}
          AMAZON_AFFILIATE_TAG: ${{ secrets.AMAZON_AFFILIATE_TAG }}
          RAPIDAPI_KEY: ${{ secrets.RAPIDAPI_KEY }}
```

2. Add secrets to GitHub repository:
   - Settings > Secrets and variables > Actions
   - Add each required variable

## Data Sources Details

### Scraping Strategy

The bot uses a progressive fallback strategy:

1. **Selenium (JavaScript rendering)**
   - Best for dynamic content
   - Handles Amazon's client-side rendering
   - Requires chromedriver
   - ~3-5 seconds per page

2. **BeautifulSoup (Static HTML)**
   - Fallback for fast parsing
   - Tries multiple URLs and selectors
   - Handles changes in HTML structure
   - ~1-2 seconds per page

### RapidAPI Endpoints

Called in sequence, up to 6 variations:
- deals-v2 page 1
- deals-v2 page 2
- best-sellers (electronics)
- best-sellers (home)
- products-by-category (electronics)
- deal-products

Average response: 30-50 products per call
Cost: 1 call = 1 RapidAPI credit

## Output Format

Messages posted to Telegram contain:

Header (once per batch):
```
Offerte Amazon — 17 April 2026

5 offerte con ≥20% sconto!

Link affiliati
Buono shopping!
```

Deal message:
```
Product Title

100.00 50.00 -50%

Star Fresh from rapidapi_deals-v2
```

Buttons:
- Buy (links to affiliate URL)
- Save (callback_data for future implementation)
- Share (callback_data for future implementation)

## Database

### Storage

File: `deals_history.json`

Format:
```json
{
  "rapidapi_deals-v2_B0FHK7S731": {
    "deal_id": "rapidapi_deals-v2_B0FHK7S731",
    "asin": "B0FHK7S731",
    "title": "Product Title",
    "price_now": 549.0,
    "price_prev": 899.0,
    "timestamp": "2026-04-17T13:45:20.067000",
    "posted": true
  }
}
```

### Deduplication Logic

A deal is posted if:
- It's new (not in database), OR
- Current price < previous price by at least 0.01 EUR

Otherwise, it's skipped with reason "Same price"

### Cleanup

Records older than 30 days are automatically removed on each run.

## Logging

### Log Files

- Console: Real-time output
- `bot.log`: Persistent log file (DEBUG level)

### Log Levels

- DEBUG: API calls, item processing, parsing details
- INFO: Major operations, deal extraction, posts
- WARNING: Fallbacks, proxy errors
- CRITICAL: Configuration errors, complete failures

### Example Log Output

```
2026-04-17 13:45:20 [INFO] FETCH DEALS — Multi-Source Strategy
2026-04-17 13:45:20 [INFO] PRIMARY: Scraping amazon.it…
2026-04-17 13:45:21 [DEBUG] BeautifulSoup: https://www.amazon.it/gp/goldbox…
2026-04-17 13:45:23 [INFO] RapidAPI (/deals-v2): 5 deals
2026-04-17 13:45:23 [INFO] Fetched 10 deals (showing diverse products)
2026-04-17 13:45:23 [INFO] POST: rapidapi_deals-v2_B0FHK7S731 — New deal
2026-04-17 13:45:25 [INFO] Posted 1/5: Product Title
2026-04-17 13:45:27 [INFO] JOB COMPLETE
```

## Troubleshooting

### No deals found

**Symptoms**: Bot runs but no deals posted

**Solutions**:
1. Check if scraped content exists:
   - Verify `amazon_debug.html` file
   - Check if Amazon page structure changed
   - Try with explicit `FORCE_POST_NEW_DEALS=true`

2. Check RapidAPI:
   - Verify API key is correct
   - Check remaining credits
   - Verify country code (IT for Italy)

3. Review logs for specific errors

### "Selenium not available"

**Symptoms**: Logs show "Selenium not available" warning

**Solution**: 
- Not a critical error - bot will fall back to BeautifulSoup
- To enable Selenium: `pip install selenium`
- Requires chromedriver installed and in PATH

### All endpoints returned 0 deals

**Symptoms**: RapidAPI called but no valid deals extracted

**Causes**:
- Discount threshold too high
- API response format changed
- Network connectivity issues

**Solutions**:
1. Lower `MIN_DISCOUNT_PERCENT`: `export MIN_DISCOUNT_PERCENT=15`
2. Check if RapidAPI working: `curl -H "X-RapidAPI-Key: YOUR_KEY" https://real-time-amazon-data.p.rapidapi.com/deals-v2?country=IT`
3. Review DEBUG logs for API response format

### Database corruption

**Symptoms**: Bot fails on database load

**Solution**:
```bash
rm deals_history.json
# Bot will recreate on next run
```

## Performance

### API Call Budget

- RapidAPI free tier: 500 calls/month
- Bot usage: 5-6 calls per run (multi-endpoint)
- Frequency: 4 runs per day (recommended)
- Monthly: 120-144 calls (well within limits)

### Execution Time

- Scraping only: 20-30 seconds
- RapidAPI only: 10-15 seconds
- Full run: 25-40 seconds
- GitHub Actions queue: +30-60 seconds

## Advanced Usage

### Custom Scraping URLs

Edit `urls_to_try` in `fetch_deals_scraping()` function to add custom Amazon URLs for category-specific deals.

### Price Parsing

The bot handles multiple price formats:
- Dict format: `{"amount": 549, "currency": "EUR"}`
- String format: "EUR 549,00"
- Numeric: `549` or `549.99`

### Discount Calculation

If explicit discount not found, calculated as:
```
discount_percent = (price_orig - price_now) / price_orig * 100
```

## File Structure

```
.
├── amazon_deals_telegram_bot.py  # Main bot script
├── requirements.txt              # Python dependencies
├── .env.example                  # Template for environment variables
├── .env                          # Your configuration (gitignored)
├── .github/workflows/            # GitHub Actions configuration
│   └── amazon-deals.yml
├── deals_history.json            # Historical price database (created at runtime)
├── bot.log                       # Log file (created at runtime)
└── amazon_debug.html             # Debug HTML (created at runtime, for troubleshooting)
```

## API Response Examples

### RapidAPI /deals-v2

```json
{
  "data": [
    {
      "deal_id": "041807b5",
      "product_asin": "B0DQPSZZZQ",
      "deal_title": "Product Name",
      "deal_price": 349.99,
      "list_price": 589.99,
      "savings_percentage": "41% di sconto",
      "deal_url": "https://amazon.it/dp/B0DQPSZZZQ",
      "deal_photo": "https://..."
    }
  ]
}
```

### Telegram Message

```
Title
100.00 50.00 -50%
Star New deal

[Buy] [Save]
[Share]
```

## Future Enhancements

- Save button: Store deals to user list
- Share button: Generate shareable link
- Price history graphs
- Email notifications
- Multi-channel distribution
- Machine learning for discount prediction
- Inventory tracking

## License

[Add your license here]

## Support

For issues or questions:
1. Check logs with DEBUG level enabled
2. Verify all environment variables are set correctly
3. Test individual components (scraping, RapidAPI, Telegram)
4. Open issue with relevant logs

## Contributing

Pull requests welcome. Please ensure:
- Code follows existing style
- New features include logging
- Database changes are backward compatible
- Tests pass before submission
