# main_backtest.py
# Entry point voor de Reverto backtest engine.
#
# Gebruik:
#   python3 main_backtest.py --config config/bots/btc_backtest.yaml
#   python3 main_backtest.py --config config/bots/btc_backtest.yaml --timeframe 4h --limit 500
#
# De backtest haalt historische data op van Bitget en simuleert de strategie.
# Resultaten worden getoond in de terminal.

import argparse
import logging
import sys

logging.basicConfig(
    level=logging.WARNING,  # Quiet — only the report in the terminal
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler()],
)
# Set backtest engine to INFO so deal-logs are visible with --verbose
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(description="Reverto Backtest Engine")
    parser.add_argument(
        "--config",
        default="config/bots/btc_backtest.yaml",
        help="Pad naar bot YAML config",
    )
    parser.add_argument(
        "--timeframe",
        default=None,
        choices=["15m", "1h", "4h", "1d"],
        help="Override bot timeframe (standaard: config.timeframe)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=1000,
        help="Aantal candles om op te halen (standaard: 1000)",
    )
    parser.add_argument(
        "--balance",
        type=float,
        default=0.1,
        help="Beginbalans in BTC (standaard: 0.1)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Toon deals en DCA orders tijdens backtest",
    )
    parser.add_argument(
        "--save",
        type=str,
        default=None,
        help="Sla resultaten op als JSON bestand (bijv. --save results/run1.json)",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger("backtest").setLevel(logging.DEBUG)

    # ── Config laden ──────────────────────────────────────────────────────────
    from config.config_loader import load_bot_config
    config = load_bot_config(args.config)

    # CLI override takes precedence over config.timeframe
    if args.timeframe:
        # We can't mutate config.timeframe (Pydantic frozen? No, but
        # we do want to override it for this run). Pydantic models
        # are mutable by default, so a direct assign works.
        config.timeframe = args.timeframe

    # All timeframes the indicators require + the bot-level timeframe
    from strategies.indicator_engine import IndicatorEngine
    tmp_engine = IndicatorEngine(config)
    required_tfs = sorted(tmp_engine.required_timeframes(config.timeframe))

    print(f"\n🔍 Reverto Backtest — {config.name}")
    print(f"   Paar      : {config.pair}")
    print(f"   Bot TF    : {config.timeframe}")
    print(f"   Alle TFs  : {', '.join(required_tfs)}")
    print(f"   Candles   : {args.limit} per timeframe")
    print(f"   Exchange  : {config.exchange.value}")
    print(f"   Beginbal  : {args.balance} BTC")
    print(f"\n⏳ Historische data ophalen van {config.exchange.value}...")

    # ── Data ophalen per timeframe ───────────────────────────────────────────
    from backtest.backtest_engine import BacktestCandle
    from exchanges.public_exchange import PublicExchange

    exchange = PublicExchange(config.exchange.value)
    candles_per_tf: dict[str, list[BacktestCandle]] = {}

    for tf in required_tfs:
        try:
            raw = exchange.get_ohlcv(config.pair, tf, args.limit)
        except Exception as e:
            print(f"\n❌ Fout bij ophalen {tf} data: {e}")
            sys.exit(1)
        if not raw:
            print(f"\n❌ Geen {tf} candles ontvangen — controleer exchange en symbol.")
            sys.exit(1)
        tf_candles = [
            BacktestCandle(
                timestamp=int(c[0]),
                open=float(c[1]),
                high=float(c[2]),
                low=float(c[3]),
                close=float(c[4]),
                volume=float(c[5]),
            )
            for c in raw
            if c[1] and c[2] and c[3] and c[4]  # filter empty candles
        ]
        candles_per_tf[tf] = tf_candles
        print(
            f"✅ {len(tf_candles)} {tf} candles "
            f"({tf_candles[0].dt.strftime('%Y-%m-%d')} → "
            f"{tf_candles[-1].dt.strftime('%Y-%m-%d')})"
        )

    # ── Run backtest ──────────────────────────────────────────────────────────
    print("\n🚀 Running backtest...\n")

    from backtest.backtest_engine import BacktestEngine
    engine = BacktestEngine(
        config=config,
        candles_per_tf=candles_per_tf,
        initial_balance_btc=args.balance,
    )

    result = engine.run()
    result.print()

    # ── Opslaan (optioneel) ───────────────────────────────────────────────────
    if args.save:
        import json
        from pathlib import Path
        save_path = Path(args.save)
        save_path.parent.mkdir(parents=True, exist_ok=True)
        save_path.write_text(json.dumps(result.to_dict(), indent=2))
        print(f"\n💾 Resultaten opgeslagen: {args.save}")


if __name__ == "__main__":
    main()
