"""The daily report: exposure by counterparty, limit utilization, excesses, fails, watchlist.

Printed as text, saved under reports/<business date>/ with CSVs of the trade and counterparty
tables, and appended to reports/history.csv so an excess can be tracked from the day it started.
"""

import csv
import datetime as dt
import math
from dataclasses import dataclass
from pathlib import Path

from dvp.config import REPORT_CURRENCY, Settings
from dvp.engine import Run, TradeExposure
from dvp.fails import Fail, aged_fails, bucket_label
from dvp.limits import CounterpartyExposure, LimitGrid, LimitStatus, aggregate
from dvp.news import CounterpartyEvent

WIDTH = 104
HISTORY_FIELDS = [
    "business_date",
    "counterparty_id",
    "name",
    "rating",
    "kind",
    "pfe_cad",
    "limit_cad",
    "utilization",
    "status",
]


@dataclass(frozen=True)
class DailyReport:
    run: Run
    settings: Settings
    counterparties: list[CounterpartyExposure]
    fails: list[Fail]
    events: list[CounterpartyEvent]
    excess_since: dict[str, dt.date]  # counterparty_id -> first business date of the excess
    run_time: dt.datetime


def build_report(
    run: Run,
    grid: LimitGrid,
    events: list[CounterpartyEvent],
    history: list[dict[str, str]],
    settings: Settings,
    run_time: dt.datetime,
) -> DailyReport:
    watch = {e.counterparty_id for e in events}
    counterparties = aggregate(run.exposures, grid, watch, settings.amber_utilization)
    excess_since = {
        c.counterparty.counterparty_id: _excess_start(
            c.counterparty.counterparty_id, run.as_of, history
        )
        for c in counterparties
        if c.status is LimitStatus.EXCESS
    }
    return DailyReport(
        run=run,
        settings=settings,
        counterparties=counterparties,
        fails=aged_fails(run.exposures),
        events=events,
        excess_since=excess_since,
        run_time=run_time,
    )


def _excess_start(counterparty_id: str, as_of: dt.date, history: list[dict[str, str]]) -> dt.date:
    """First business date of the current run of consecutive excesses in the history."""
    earlier = sorted(
        (
            (dt.date.fromisoformat(r["business_date"]), r["status"])
            for r in history
            if r["counterparty_id"] == counterparty_id
            and dt.date.fromisoformat(r["business_date"]) < as_of
        ),
        reverse=True,
    )
    start = as_of
    for day, status in earlier:
        if status != LimitStatus.EXCESS:
            break
        start = day
    return start


# ---------------------------------------------------------------------------------------------
# Text rendering


def money(x: float) -> str:
    return f"{x:,.0f}"


def pct(x: float) -> str:
    return "no limit" if math.isinf(x) else f"{x:.1%}"


def _section(title: str) -> list[str]:
    return ["", title, "-" * WIDTH]


def render(report: DailyReport) -> str:
    run = report.run
    exposures = run.exposures
    ccy = REPORT_CURRENCY
    failed = sum(e.failed for e in exposures)
    total_value = sum(abs(e.value_cad) for e in exposures)
    total_pfe = sum(e.pfe_cad for e in exposures)
    excesses = [c for c in report.counterparties if c.status is LimitStatus.EXCESS]
    ambers = [c for c in report.counterparties if c.status is LimitStatus.AMBER]

    lines = ["=" * WIDTH, "DVP COUNTERPARTY SETTLEMENT RISK - DAILY REPORT"]
    lines.append(
        f"Business date: {run.as_of.isoformat()}   "
        f"Run: {report.run_time.strftime('%Y-%m-%d %H:%M UTC')}   Currency: {ccy}"
    )
    lines.append("=" * WIDTH)
    lines.append(
        f"Open trades: {len(exposures)} ({len(exposures) - failed} pending, {failed} failed) "
        f"across {len(report.counterparties)} counterparties"
    )
    if total_value:
        lines.append(
            f"Settlement value {money(total_value)} vs. exposure (PFE) {money(total_pfe)}: "
            f"{total_pfe / total_value:.2%} of it is at risk (DVP removes principal risk)"
        )
    lines.append(
        f"Limit excesses: {len(excesses)}   Amber (>= {report.settings.amber_utilization:.0%}): "
        f"{len(ambers)}   Fails: {failed}   Watchlist names: "
        f"{len({e.counterparty_id for e in report.events})}"
    )
    if run.unmeasured:
        lines.append(f"NOT MEASURED (no market data): {len(run.unmeasured)} trades, see warnings")

    lines += _counterparty_table(report)
    lines += _excesses(report)
    lines += _fails(report.fails)
    lines += _wrong_way(exposures)
    lines += _watchlist(report)
    lines += _trade_detail(exposures)
    lines += _market_notes(report)
    lines.append("=" * WIDTH)
    lines.append(
        f"PFE = max(0, replacement cost + add-on), one-sided {_confidence(report.settings)}, "
        f"summed per counterparty with no netting."
    )
    lines.append("Synthetic trade book; internal ratings and limits are illustrative.")
    return "\n".join(lines)


def _confidence(settings: Settings) -> str:
    from statistics import NormalDist

    return f"{NormalDist().cdf(settings.z):.0%}"


def _counterparty_table(report: DailyReport) -> list[str]:
    lines = _section("LIMIT UTILIZATION BY COUNTERPARTY")
    lines.append(
        f"{'Counterparty':<31}{'Rtg':<5}{'Type':<14}{'Trd':>4}{'Fail':>5}"
        f"{'Settle value':>14}{'Curr exp':>11}{'PFE':>11}{'Limit':>11}{'Util':>9}  Status"
    )
    for c in report.counterparties:
        flags = []
        if c.on_watch:
            flags.append("WATCH")
        if c.wrong_way:
            flags.append("WWR")
        flag_text = f" [{', '.join(flags)}]" if flags else ""
        lines.append(
            f"{c.counterparty.name[:30]:<31}{c.counterparty.rating:<5}"
            f"{c.counterparty.kind.replace('_', ' '):<14}{c.trades:>4}{c.failed:>5}"
            f"{money(c.settlement_value):>14}{money(c.current_exposure):>11}"
            f"{money(c.pfe):>11}{money(c.limit):>11}{pct(c.utilization):>9}  "
            f"{c.status}{flag_text}"
        )
    lines.append("-" * WIDTH)
    lines.append(
        f"{'TOTAL':<54}"
        f"{money(sum(c.settlement_value for c in report.counterparties)):>14}"
        f"{money(sum(c.current_exposure for c in report.counterparties)):>11}"
        f"{money(sum(c.pfe for c in report.counterparties)):>11}"
    )
    return lines


def _excesses(report: DailyReport) -> list[str]:
    lines = _section("LIMIT EXCESSES AND EARLY WARNINGS")
    flagged = [c for c in report.counterparties if c.status is not LimitStatus.OK]
    if not flagged:
        return [*lines, "  none"]
    for c in flagged:
        name = c.counterparty.name
        grid_cell = f"{c.counterparty.rating} {c.counterparty.kind.replace('_', ' ')}"
        if c.status is LimitStatus.EXCESS:
            since = report.excess_since[c.counterparty.counterparty_id]
            started = "new today" if since == report.run.as_of else f"since {since.isoformat()}"
            limit_text = (
                f"limit {money(c.limit)} ({grid_cell})"
                if c.limit
                else f"no limit for {grid_cell} in the grid"
            )
            lines.append(
                f"  EXCESS {name}: PFE {money(c.pfe)} vs {limit_text}, "
                f"excess {money(c.excess)} ({pct(c.utilization)}), {started}"
            )
        else:
            lines.append(
                f"  AMBER  {name}: PFE {money(c.pfe)} is {pct(c.utilization)} of "
                f"limit {money(c.limit)} ({grid_cell})"
            )
    return lines


def _fails(fails: list[Fail]) -> list[str]:
    lines = _section("SETTLEMENT FAILS (aged in business days, Basel failed-trade factors)")
    if not fails:
        return [*lines, "  none"]
    lines.append(
        f"{'Trade':<9}{'Counterparty':<27}{'Security':<10}{'Side':<5}{'Due':>11}"
        f"{'Age':>5}{'Bucket':>8}{'Factor':>8}{'Curr exp':>12}{'Capital':>10}"
    )
    for f in fails:
        e = f.exposure
        lines.append(
            f"{e.trade.trade_id:<9}{e.counterparty.name[:26]:<27}{e.security.security_id:<10}"
            f"{e.trade.side:<5}{e.trade.settle_date.isoformat():>11}{f.age:>5}"
            f"{bucket_label(f.age):>8}{f.factor:>8.0%}{money(e.ce_cad):>12}"
            f"{money(f.capital_charge):>10}"
        )
    lines.append(
        f"  Capital held against fails: {money(sum(f.capital_charge for f in fails))} "
        f"(positive current exposure x factor; 0% under 5 days)"
    )
    return lines


def _wrong_way(exposures: list[TradeExposure]) -> list[str]:
    lines = _section("WRONG-WAY RISK (selling a counterparty securities its own group issued)")
    wrong_way = [e for e in exposures if e.wrong_way]
    if not wrong_way:
        return [*lines, "  none"]
    for e in wrong_way:
        lines.append(
            f"  {e.trade.trade_id} sell {e.trade.quantity:,.0f} {e.security.security_id} to "
            f"{e.counterparty.name}: a default would hit the price too. Exposure taken at "
            f"jump-to-default {money((e.jump_to_default or 0) * e.fx)} "
            f"(vs. {money(max(0.0, e.mtm + e.add_on) * e.fx)} from price volatility)"
        )
    return lines


def _watchlist(report: DailyReport) -> list[str]:
    lines = _section(
        f"COUNTERPARTY WATCHLIST (news in the last {report.settings.news_lookback_days} days: "
        f"{', '.join(report.settings.news_categories)})"
    )
    if not report.events:
        return [*lines, "  none"]
    names = {c.counterparty.counterparty_id: c.counterparty.name for c in report.counterparties}
    for e in report.events:
        name = names.get(e.counterparty_id, e.counterparty_id)
        lines.append(f"  {e.date.isoformat()}  {name} ({e.category}, {e.source})")
        lines.append(f"      {e.summary}")
    return lines


def _trade_detail(exposures: list[TradeExposure], top: int = 15) -> list[str]:
    lines = _section(f"LARGEST TRADE EXPOSURES (top {top} by PFE, {REPORT_CURRENCY})")
    lines.append(
        f"{'Trade':<9}{'Counterparty':<24}{'Security':<10}{'Side':<5}{'Quantity':>12}"
        f"{'Trade px':>10}{'Price':>10}{'MtM':>11}{'Add-on':>10}{'PFE':>11}{'Hzn':>4}"
    )
    for e in sorted(exposures, key=lambda e: e.pfe_cad, reverse=True)[:top]:
        state = "F" if e.failed else ""
        lines.append(
            f"{e.trade.trade_id:<9}{e.counterparty.name[:23]:<24}{e.security.security_id:<10}"
            f"{e.trade.side:<5}{e.trade.quantity:>12,.0f}{e.trade.trade_price:>10.2f}"
            f"{e.price:>10.2f}{money(e.mtm_cad):>11}{money(e.add_on * e.fx):>10}"
            f"{money(e.pfe_cad):>11}{e.horizon_days:>3}d{state}"
        )
    lines.append(
        "  MtM > 0: the trade has moved against us. Hzn: business days to settlement (F: failed)."
    )
    return lines


def _market_notes(report: DailyReport) -> list[str]:
    run = report.run
    lines = _section("MARKET DATA AND WARNINGS")
    for ccy, (rate, day) in sorted(run.fx_rates.items()):
        lines.append(f"  {ccy}{REPORT_CURRENCY} {rate:.4f} (Bank of Canada, {day.isoformat()})")
    models: dict[str, int] = {}
    for model in run.vol_models.values():
        models[model] = models.get(model, 0) + 1
    lines.append(
        "  Volatility models: " + ", ".join(f"{m} x{n}" for m, n in sorted(models.items()))
    )
    fallbacks = sorted(s for s, m in run.vol_models.items() if m != "GARCH(1,1)")
    if fallbacks:
        lines.append(f"  EWMA fallback (GARCH not fitted): {', '.join(fallbacks)}")
    for w in run.warnings:
        lines.append(f"  WARNING {w}")
    for t in run.unmeasured:
        lines.append(f"  NOT MEASURED {t.trade_id} ({t.security_id}, {t.counterparty_id})")
    return lines


# ---------------------------------------------------------------------------------------------
# Files


def read_history(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def write_history(path: Path, report: DailyReport) -> None:
    """Replace this business date's rows, so a re-run doesn't duplicate them."""
    as_of = report.run.as_of.isoformat()
    rows = [r for r in read_history(path) if r["business_date"] != as_of]
    for c in report.counterparties:
        rows.append(
            {
                "business_date": as_of,
                "counterparty_id": c.counterparty.counterparty_id,
                "name": c.counterparty.name,
                "rating": c.counterparty.rating,
                "kind": c.counterparty.kind,
                "pfe_cad": f"{c.pfe:.2f}",
                "limit_cad": f"{c.limit:.2f}",
                "utilization": "" if math.isinf(c.utilization) else f"{c.utilization:.4f}",
                "status": str(c.status),
            }
        )
    rows.sort(key=lambda r: (r["business_date"], r["counterparty_id"]))
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=HISTORY_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def write_outputs(out_dir: Path, report: DailyReport, text: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.txt").write_text(text, encoding="utf-8")

    with open(out_dir / "trades.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "trade_id", "counterparty_id", "security_id", "side", "quantity",
                "trade_price", "price", "price_date", "currency", "fx_to_cad", "status",
                "fail_age", "horizon_days", "vol_model", "horizon_sigma", "value_cad",
                "mtm_cad", "current_exposure_cad", "add_on_cad", "pfe_cad", "wrong_way",
            ]
        )  # fmt: skip
        for e in report.run.exposures:
            writer.writerow(
                [
                    e.trade.trade_id, e.counterparty.counterparty_id, e.security.security_id,
                    e.trade.side, e.trade.quantity, e.trade.trade_price, round(e.price, 4),
                    e.price_date.isoformat(), e.security.currency, round(e.fx, 6),
                    "failed" if e.failed else "pending", e.fail_age if e.failed else "",
                    e.horizon_days, e.vol_model, round(e.horizon_sigma, 6),
                    round(e.value_cad, 2), round(e.mtm_cad, 2), round(e.ce_cad, 2),
                    round(e.add_on * e.fx, 2), round(e.pfe_cad, 2), e.wrong_way,
                ]
            )  # fmt: skip

    with open(out_dir / "counterparties.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "counterparty_id", "name", "rating", "kind", "trades", "failed", "wrong_way",
                "settlement_value_cad", "current_exposure_cad", "pfe_cad", "limit_cad",
                "utilization", "status", "on_watch",
            ]
        )  # fmt: skip
        for c in report.counterparties:
            writer.writerow(
                [
                    c.counterparty.counterparty_id, c.counterparty.name, c.counterparty.rating,
                    c.counterparty.kind, c.trades, c.failed, c.wrong_way,
                    round(c.settlement_value, 2), round(c.current_exposure, 2), round(c.pfe, 2),
                    round(c.limit, 2),
                    "" if math.isinf(c.utilization) else round(c.utilization, 4),
                    c.status, c.on_watch,
                ]
            )  # fmt: skip
