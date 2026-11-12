# Adaptive Multi-Factor Regime Strategy Backtester

M0–M2 repairs and execution assumptions are documented in [docs/M0-M2.md](docs/M0-M2.md). The original source is preserved under snapshots/original. Run financial/timing regression checks with `python -m unittest discover -s tests -v`. These checks use synthetic fixtures, not historical strategy validation.

A deterministic implementation of the strategy designed in this conversation. It is intentionally separated from any LLM: the backtester evaluates the measurable trading rules first. Historical sentiment/news/order-book/funding/OI can be supplied as optional columns; if they are absent, the engine uses neutral values and clearly reports that those layers were not tested.

## Strategy
- 4H macro regime
- 1H primary trend
- 15m setup
- 5m execution
- Trend, momentum, volume, breakout/retest and range mean-reversion engines
- Funding/OI/positioning and liquidity hooks
- Composite confidence gate
- ATR stop
- 1.5R half exit / 2.5R remainder, with protective stop adjustment
- 0.25% risk per trade by default
- 1.5% daily loss stop, 3% weekly loss stop, 5% hard strategy drawdown stop
- Fees + slippage
- No look-ahead: signals use completed higher-timeframe candles and entries execute on the next 5m open

## Data
The cleanest input is a single 5m CSV with columns:
`timestamp,open,high,low,close,volume`

Optional columns:
`funding_rate,open_interest,orderbook_imbalance,spread_bps,news_sentiment`

The engine resamples the 5m data to 15m/1h/4h. For a rigorous test of the full strategy, provide the optional historical columns rather than allowing neutral defaults.

## Run
```bash
python backtest.py --csv data/BTCUSDT_5m.csv --start 2021-01-01 --end 2026-01-01 --capital 2000 --out runs/btc_5m
```

The runner writes `summary.json`, `trades.csv`, `equity.csv`, and `signals.csv`.

## Important
This is a research backtester, not proof of future profitability. It does not simulate exchange-specific queue position, liquidation, or historical order-book fills. Supplying optional columns does not enable those execution models. Funding costs require explicit funding_event_rate settlement observations; funding_rate alone is only a strategy feature.
