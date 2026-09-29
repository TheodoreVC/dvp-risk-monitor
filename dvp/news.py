"""Counterparty news that puts a name on the credit watchlist.

Two sources, both optional:
- data/counterparty_events.csv: events entered by hand (and the sample events for the fictional
  counterparties, so the repo runs on its own).
- Attention Alerts, a separate news-alert service: major news stories its classifier sorted
  into legal (lawsuits, investigations, fines, regulatory actions), leadership or deal, read
  from its Postgres database for counterparties with a listed parent.
"""

import csv
import datetime as dt
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from dvp.book import Counterparty


@dataclass(frozen=True)
class CounterpartyEvent:
    counterparty_id: str
    date: dt.date
    category: str
    summary: str
    source: str


class EventSource(Protocol):
    def events(
        self, counterparties: list[Counterparty], since: dt.date, until: dt.date
    ) -> list[CounterpartyEvent]: ...


class CsvEvents:
    """counterparty_id, date, category, summary, source"""

    def __init__(self, path: Path) -> None:
        self.path = path

    def events(
        self, counterparties: list[Counterparty], since: dt.date, until: dt.date
    ) -> list[CounterpartyEvent]:
        if not self.path.exists():
            return []
        wanted = {c.counterparty_id for c in counterparties}
        out = []
        with open(self.path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                day = dt.date.fromisoformat(row["date"].strip())
                if row["counterparty_id"] in wanted and since <= day <= until:
                    out.append(
                        CounterpartyEvent(
                            counterparty_id=row["counterparty_id"],
                            date=day,
                            category=row["category"].strip(),
                            summary=row["summary"].strip(),
                            source=row["source"].strip(),
                        )
                    )
        return out


# Major stories about the counterparties' listed parents. A story without a summary falls back
# to the headline of its first article.
_STORIES_SQL = """
SELECT s.ticker, s.exchange, st.published_at::date AS day, st.category,
       COALESCE(st.summary, (SELECT n.headline FROM news n WHERE n.story_id = st.id
                             ORDER BY n.published_at LIMIT 1)) AS summary
FROM stories st
JOIN securities s ON s.id = st.security_id
WHERE st.importance = 'major'
  AND st.published_at >= %(since)s
  AND st.published_at < %(until)s
  AND (s.ticker, s.exchange) IN (SELECT * FROM unnest(%(tickers)s::text[], %(exchanges)s::text[]))
ORDER BY st.published_at
"""


class AttentionAlertsEvents:
    """Reads major stories from an Attention Alerts database (read-only).

    Attention Alerts only fetches news for tickers on a watchlist, so the counterparties' tickers
    must be on one (OWNER_WATCHLIST, or a watchlist in its app) for stories to exist.
    """

    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def events(
        self, counterparties: list[Counterparty], since: dt.date, until: dt.date
    ) -> list[CounterpartyEvent]:
        import psycopg

        listed = {(c.ticker, c.exchange): c for c in counterparties if c.ticker and c.exchange}
        if not listed:
            return []
        params = {
            "since": since,
            "until": until + dt.timedelta(days=1),
            "tickers": [t for t, _ in listed],
            "exchanges": [x for _, x in listed],
        }
        with psycopg.connect(self.dsn, connect_timeout=5) as conn:
            conn.read_only = True
            rows = conn.execute(_STORIES_SQL, params).fetchall()
        return [
            CounterpartyEvent(
                counterparty_id=listed[(ticker, exchange)].counterparty_id,
                date=day,
                category=category,
                summary=summary or "",
                source="Attention Alerts",
            )
            for ticker, exchange, day, category, summary in rows
        ]


def watchlist_events(
    sources: list[EventSource],
    counterparties: list[Counterparty],
    as_of: dt.date,
    lookback_days: int,
    categories: tuple[str, ...],
) -> tuple[list[CounterpartyEvent], list[str]]:
    """Events in the watch categories from the last `lookback_days` days, newest first, and
    one error message per source that couldn't be read (news is optional; the risk numbers
    must come out regardless)."""
    since = as_of - dt.timedelta(days=lookback_days)
    events: list[CounterpartyEvent] = []
    errors: list[str] = []
    for source in sources:
        try:
            found = source.events(counterparties, since, as_of)
        except Exception as e:  # a database or file problem in one source
            errors.append(f"{type(source).__name__}: {e}")
            continue
        events += [e for e in found if e.category in categories]
    return sorted(events, key=lambda e: e.date, reverse=True), errors
