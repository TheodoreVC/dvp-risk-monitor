"""Command line.

python -m dvp run                       # report for the book's business date
python -m dvp run --as-of 2026-09-24    # replay an earlier day
python -m dvp make-book --seed 7        # regenerate the synthetic book
"""

import argparse
import datetime as dt
import os
import sys
from pathlib import Path

from dvp.book import load_book
from dvp.config import DATA_DIR, REPORTS_DIR, Settings
from dvp.engine import measure_book
from dvp.limits import load_grid
from dvp.market import LiveMarketData
from dvp.news import AttentionAlertsEvents, CsvEvents, EventSource, watchlist_events
from dvp.report import build_report, read_history, render, write_history, write_outputs


def run(args: argparse.Namespace) -> int:
    settings = Settings()
    data_dir = Path(args.data_dir)
    book = load_book(data_dir)
    grid = load_grid(data_dir / "limit_grid.csv")
    as_of = args.as_of or book.business_date
    if as_of > book.business_date:
        # The book doesn't know which of its pending trades settled after its snapshot date.
        print(
            f"ERROR: the book is a snapshot as of {book.business_date}; for a later date, "
            f"regenerate it with: python -m dvp make-book --as-of {as_of}",
            file=sys.stderr,
        )
        return 1

    missing = sorted({c.rating for c in book.counterparties.values()} - set(grid.ratings))
    if missing:
        print(f"ERROR: ratings missing from limit_grid.csv: {', '.join(missing)}", file=sys.stderr)
        return 1

    market = LiveMarketData(as_of, refresh=args.refresh)
    result = measure_book(book, market, as_of, settings)

    sources: list[EventSource] = [CsvEvents(data_dir / "counterparty_events.csv")]
    dsn = args.attention_alerts_dsn or os.environ.get("ATTENTION_ALERTS_DSN")
    if dsn:
        sources.append(AttentionAlertsEvents(dsn))
    events, errors = watchlist_events(
        sources,
        list(book.counterparties.values()),
        as_of,
        settings.news_lookback_days,
        settings.news_categories,
    )
    result.warnings += [f"counterparty news unavailable from {e}" for e in errors]

    history_path = REPORTS_DIR / "history.csv"
    report = build_report(
        result,
        grid,
        events,
        read_history(history_path),
        settings,
        dt.datetime.now(dt.UTC),
    )
    text = render(report)
    print(text)
    out_dir = REPORTS_DIR / as_of.isoformat()
    write_outputs(out_dir, report, text)
    write_history(history_path, report)
    print(f"\nSaved {out_dir / 'report.txt'} (+ trades.csv, counterparties.csv)")
    return 0


def make_book(args: argparse.Namespace) -> int:
    from dvp.make_book import generate

    generate(Path(args.data_dir), args.as_of, args.seed, args.days)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m dvp", description=__doc__.split("\n")[0])
    parser.add_argument("--data-dir", default=str(DATA_DIR))
    commands = parser.add_subparsers(dest="command", required=True)

    run_parser = commands.add_parser("run", help="measure the book and print the daily report")
    run_parser.add_argument(
        "--as-of", type=dt.date.fromisoformat, help="business date (default: the book's)"
    )
    run_parser.add_argument("--refresh", action="store_true", help="re-fetch all market data")
    run_parser.add_argument(
        "--attention-alerts-dsn",
        help="Postgres URL of an Attention Alerts database, for counterparty news "
        "(default: $ATTENTION_ALERTS_DSN)",
    )
    run_parser.set_defaults(func=run)

    book_parser = commands.add_parser("make-book", help="generate the synthetic trade book")
    book_parser.add_argument(
        "--as-of", type=dt.date.fromisoformat, help="business date (default: last close)"
    )
    book_parser.add_argument("--seed", type=int, default=7)
    book_parser.add_argument("--days", type=int, default=40, help="business days of history")
    book_parser.set_defaults(func=make_book)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
