# mb_market_data

Market-data acquisition, historical storage, and feature-generation tools for the MasterBot project.

## Initial objectives

The first development target is historical and current overnight-market data used to help construct the opening ThinkOrSwim Watchlist.

Initial work will investigate data available through the Charles Schwab Trader API, including:

- intraday price-history candles;
- overnight and pre-market volume;
- quote data near the regular-market open;
- previous close and opening-price information;
- market capitalization and share-count information;
- instrument identity and symbol lifecycle information.

## Initial overnight-volume definitions

All human-facing market times use Eastern Time (`America/New_York`).

### OV_DECISION

Volume from:

- 01:00 ET
- through 09:25 ET

This is the current-day overnight-volume value available when constructing the opening Watchlist.

### PREMARKET

Volume from:

- 07:00 ET
- through 09:25 ET

This represents the normal Schwab pre-market window used for the opening Watchlist decision.

### OV_FINAL

Volume from:

- 01:00 ET
- through 09:30 ET

This is the completed overnight-volume value retained for historical calculations.

Historical statistics are expected to include:

- 3-, 5-, 10-, and 30-session median overnight volume;
- 3-, 5-, 10-, and 30-session maximum overnight volume;
- relative-volume measures derived from those values.

## Architecture

`mb_market_data` is intended to own reusable market-data infrastructure, including:

- Schwab data acquisition;
- historical market-data storage;
- instrument and symbol identity;
- corporate-action history;
- fundamental/share-count history;
- trading-session definitions;
- overnight-volume calculations;
- immutable decision-time feature snapshots;
- data-quality checking and reconciliation.

Watchlist ranking and selection logic may eventually become a separate project.

## Repository layout

```text
mb_market_data/
├── probes/
├── src/
│   └── mb_market_data/
├── tests/
├── output/
├── .gitignore
├── pyproject.toml
└── README.md
