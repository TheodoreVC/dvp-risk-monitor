import datetime as dt

import pytest

from dvp.book import BookError, load_book
from dvp.news import CsvEvents, watchlist_events
from tests.conftest import trade


def test_open_and_failed_are_point_in_time():
    t = trade(
        trade_date=dt.date(2026, 9, 21),
        settle_date=dt.date(2026, 9, 22),
        settled_on=dt.date(2026, 9, 24),
    )
    assert not t.is_open(dt.date(2026, 9, 18))  # not traded yet
    assert t.is_open(dt.date(2026, 9, 21)) and not t.is_failed(dt.date(2026, 9, 21))  # pending
    assert t.is_failed(dt.date(2026, 9, 22))  # still open at the end of its settlement date
    assert t.is_failed(dt.date(2026, 9, 23))
    assert not t.is_open(dt.date(2026, 9, 24))  # settled


HEADERS = {
    "securities.csv": "security_id,asset_type,description,currency,country,issuer_group,ticker,"
    "coupon,maturity,benchmark,spread_bp\nRY,equity,Royal Bank,CAD,CA,RBC,RY.TO,,,,\n",
    "counterparties.csv": "counterparty_id,name,kind,country,rating,group,ticker,exchange\n"
    "C01,Royal Bank of Canada,bank,CA,AA,RBC,RY,TSX\n",
}
TRADE_HEADER = "trade_id,counterparty_id,security_id,side,quantity,trade_price,trade_date,settle_date,settled_on\n"  # noqa: E501


def _write(tmp_path, trades: str):
    for name, text in HEADERS.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    (tmp_path / "trades.csv").write_text(TRADE_HEADER + trades, encoding="utf-8")


def test_load_book(tmp_path):
    _write(tmp_path, "T1,C01,RY,buy,100,50,2026-09-28,2026-09-29,\n")
    book = load_book(tmp_path)
    assert book.business_date == dt.date(2026, 9, 28)
    assert book.trades[0].settled_on is None


@pytest.mark.parametrize(
    ("row", "message"),
    [
        ("T1,C99,RY,buy,100,50,2026-09-28,2026-09-29,", "unknown counterparty"),
        ("T1,C01,XX,buy,100,50,2026-09-28,2026-09-29,", "unknown security"),
        ("T1,C01,RY,buy,-5,50,2026-09-28,2026-09-29,", "positive"),
        ("T1,C01,RY,buy,100,50,2026-09-28,2026-09-25,", "settles before"),
        ("T1,C01,RY,hold,100,50,2026-09-28,2026-09-29,", "hold"),
    ],
)
def test_bad_trades_are_rejected_with_the_line(tmp_path, row, message):
    _write(tmp_path, row + "\n")
    with pytest.raises(BookError, match=message) as err:
        load_book(tmp_path)
    assert "line 2" in str(err.value)


def test_csv_events_window_and_categories(tmp_path, counterparties):
    path = tmp_path / "events.csv"
    path.write_text(
        "counterparty_id,date,category,summary,source\n"
        "C11,2026-09-24,legal,Regulator review.,sample\n"
        "C11,2026-08-01,legal,Too old.,sample\n"
        "C11,2026-09-29,legal,After the business date.,sample\n"
        "C01,2026-09-20,product,Not a watch category.,sample\n",
        encoding="utf-8",
    )
    events, errors = watchlist_events(
        [CsvEvents(path)], list(counterparties.values()), dt.date(2026, 9, 28), 30,
        ("legal", "leadership"),
    )  # fmt: skip
    assert [e.summary for e in events] == ["Regulator review."] and errors == []


def test_a_broken_news_source_does_not_stop_the_run(tmp_path, counterparties):
    class Broken:
        def events(self, counterparties, since, until):
            raise ConnectionError("database down")

    events, errors = watchlist_events(
        [Broken()], list(counterparties.values()), dt.date(2026, 9, 28), 30, ("legal",)
    )
    assert events == [] and "database down" in errors[0]
