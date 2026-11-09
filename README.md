# trading-prices

Daily closing prices for Eric's AI Trading Agents Competition (paper trading, personal use).

A GitHub Actions job (`.github/workflows/prices.yml`) runs `fetch_prices.py` every weekday after the market close and again overnight. It downloads daily closes from Yahoo Finance (via the unofficial `yfinance` library) for:

- S&P 500 and Nasdaq-100 companies
- S&P/TSX Composite companies
- a list of popular US and Canadian ETFs (in `fetch_prices.py`)
- anything listed in `extra_symbols.txt`

…and the USD/CAD exchange rate. Results are saved in `data/`:

| File | What's in it |
|---|---|
| `data/latest.json` | Newest close for every symbol, plus the latest USD/CAD |
| `data/closes/YYYY-MM.json` | Every daily close, by date then symbol |
| `data/fx/YYYY-MM.json` | Daily USD/CAD |
| `data/universe.json` | The symbol list and where each came from |
| `data/splits.json` | Stock splits by symbol and date (4.0 = 4-for-1). Divide earlier prices by the ratio to compare across a split |

Symbols use Yahoo's format: TSX ends in `.TO` (`RY.TO`), class shares use a hyphen (`BRK-B`, `BBD-B.TO`).

To add a symbol, put it on its own line in `extra_symbols.txt`. To rerun by hand: Actions tab → "Daily closing prices" → Run workflow.

Prices are the actual daily closes (not adjusted for splits or dividends); `data/splits.json` lists splits. Data comes from Yahoo Finance and is for personal, non-commercial use.
