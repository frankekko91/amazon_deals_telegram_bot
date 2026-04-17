# Amazon Deals Telegram Bot

A Python-based bot that automatically fetches Amazon deals from RapidAPI and posts them to a Telegram channel with affiliate links.

## Overview

This bot fetches Amazon deals from multiple RapidAPI endpoints and posts them to a Telegram channel with:
- Product title and price information
- Discount percentage
- Affiliate links
- Interactive buttons (Buy, Save, Share)
- Smart deduplication based on historical prices
- Multi-source product diversity (15+ products per run)

## Architecture

### Data Source

**RapidAPI (real-time-amazon-data)**
- Primary and only data source
- Multiple endpoints called per run to maximize product diversity:
  - deals-v2 (latest discounted products)
  - best-sellers (electronics category)
  - best-sellers (home category)
  - best-sellers (sports category)
  - deal-products (featured deals)
  - products-by-category (electronics)
- Eliminates duplicate products via ASIN deduplication
- Collects up to 15 products per run from diverse sources

### Processing Pipeline

```
RapidAPI Multi-Endpoint Collection
    |
    ├─> deals-v2: 5 products
    ├─> best-sellers (electronics): 5 products
    ├─> best-sellers (home): 5 products
    └─> (stops at 15 products total)
         |
         v
    Deduplication by ASIN
         |
         v
    Database Lookup
         |
    ┌────────┴──────────┐
    v                   v
New Deal?        Price Drop?
    |                   |
    └────────┬──────────┘
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

4. Configure `.env` with required credentials

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

Testing mode (force posting of all new deals):
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

### RapidAPI Endpoints (Multi-Source Strategy)

Bot calls 6 different endpoints/parameters to gather diverse products:

| Endpoint | Parameter | Purpose |
|----------|-----------|---------|
| deals-v2 | page=1 | Latest Amazon deals |
| best-sellers | category=electronics | Top electronics |
| best-sellers | category=home | Top home products |
| best-sellers | category=sports | Top sports items |
| deal-products | page=1 | Featured deal products |
| products-by-category | category=electronics | Electronics catalog |

**Collection Logic:**
- Calls endpoints sequentially
- Extracts up to 5 products from each endpoint
- Deduplicates by ASIN to prevent duplicate posts
- Stops at 15 unique products total
- Takes ~6 seconds total (4-6 API calls × timeout)

**Response Handling:**
- Accepts multiple response formats
- Handles both string and dict price formats
- Flexible field mapping (deal_price, product_price, price, etc.)

## Output Format

Messages posted to Telegram contain:

**Header (once per batch):**
```
═══════════════════════════╗
    Offerte Amazon — 17 April 2026

5 offerte con ≥20% sconto!

Link affiliati
Buono shopping!
═══════════════════════════╝
```

**Individual Deal Messages:**
```
Product Title

100.00 50.00 -50%

Star New deal
```

**Interactive Buttons:**
- Buy (direct link to Amazon affiliate URL)
- Save (callback for future implementation)
- Share (callback for future implementation)

## Database

### Storage

File: `deals_history.json`

Sample format:
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

Note: Deduplication by ASIN happens during collection to prevent fetching identical products from multiple endpoints.

### Cleanup

Records older than 30 days are automatically removed on each run.

## Logging

### Log Files

- Console: Real-time output
- `bot.log`: Persistent log file (DEBUG level)

### Log Levels

- DEBUG: API calls, item processing, parsing details
- INFO: Major operations, deal extraction, posts
- WARNING: Fallbacks, parse errors
- CRITICAL: Configuration errors, complete failures

### Example Log Output

```
2026-04-17 13:51:49 [INFO] FETCH DEALS — RapidAPI Only Strategy
2026-04-17 13:51:49 [INFO] PRIMARY: RapidAPI (multi-endpoint)…
2026-04-17 13:51:53 [INFO] deals-v2: 5 deals (5 total unique)
2026-04-17 13:51:55 [INFO] best-sellers: 5 deals (10 total unique)
2026-04-17 13:51:56 [INFO] RapidAPI total: 15 unique deals
2026-04-17 13:51:56 [INFO] Fetched 15 deals (showing diverse products)
2026-04-17 13:51:56 [INFO] POST: rapidapi_deals-v2_B0FHK7S731 — New deal
2026-04-17 13:51:58 [INFO] Posted 1/5: Product Title
2026-04-17 13:52:04 [INFO] JOB COMPLETE
```

## Performance

### API Call Budget

- RapidAPI free tier: 500-1000 calls/month
- Bot usage: 5-6 calls per run (multi-endpoint)
- Frequency: 4 runs per day (recommended)
- Monthly: 120-144 calls (well within limits)

### Execution Time

- RapidAPI collection: 10-15 seconds
- Database operations: 1-2 seconds
- Telegram posting (5 messages): 10-12 seconds
- Full run: 25-40 seconds
- GitHub Actions queue: +30-60 seconds

## Troubleshooting

### No deals found

**Symptoms:** Bot runs but no deals posted

**Solutions:**
1. Verify RapidAPI key is correct
2. Check remaining RapidAPI credits
3. Verify country code (IT for Italy)
4. Review DEBUG logs for API response format
5. Try with explicit `FORCE_POST_NEW_DEALS=true`

### "All deals skipped with same price"

**Symptoms:** Deals fetched but none posted

**Solutions:**
1. This is normal behavior - bot only posts new deals or price drops
2. To force posting: `export FORCE_POST_NEW_DEALS=true`
3. To reset: delete `deals_history.json` (loses price history)

### Unable to parse price

**Symptoms:** Deals extracted but price parsing fails

**Solutions:**
1. Check if price format is string or dict in API response
2. Parser handles multiple formats automatically
3. Review DEBUG logs for specific field names
4. Verify MIN_DISCOUNT_PERCENT setting

### Database corruption

**Symptoms:** Bot fails on database load

**Solution:**
```bash
rm deals_history.json
# Bot will recreate on next run
```

## Advanced Usage

### Price Parsing

The bot handles multiple price formats:
- Dict: `{"amount": 549, "currency": "EUR"}`
- String: "EUR 549,00"
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
├── README_BOT.md                 # This file
├── .github/workflows/            # GitHub Actions configuration
│   └── amazon-deals.yml
├── deals_history.json            # Historical price database (created at runtime)
└── bot.log                       # Log file (created at runtime)
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
Product Title

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
- Category-specific filtering
- Inventory tracking

## Support

For issues or questions:
1. Check logs with DEBUG level enabled
2. Verify all environment variables are set correctly
3. Test RapidAPI endpoint manually with curl
4. Verify Telegram credentials and permissions
5. Review troubleshooting section

## Contributing

Pull requests welcome. Please ensure:
- Code follows existing style
- New features include logging
- Database changes are backward compatible
- Tests pass before submission

## License

Add your license here
