"""DuckDB helpers over the Parquet corpus, plus the stats behind docs/ml/corpus_card.md.

    python -m datasets.duckdb_stats data/corpus/v1            # markdown stats tables
    python -m datasets.duckdb_stats data/corpus/v1 --bench    # + DuckDB vs pandas timing

:func:`connect` exposes the feature tables only. Label tables are attached
only when asked for (``labels=True``) and always under a ``label_`` prefix, so
a feature query can't pick up a label column by accident.
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import duckdb

FEATURE_VIEWS = ("transactions", "companies")
LABEL_VIEWS = ("transactions", "customers", "companies", "flags")


def _q(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"


def connect(corpus_dir: Path, *, labels: bool = False) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    for name in FEATURE_VIEWS:
        con.execute(f"CREATE VIEW {name} AS SELECT * FROM read_parquet({_q(corpus_dir / 'features' / f'{name}.parquet')})")
    if labels:
        for name in LABEL_VIEWS:
            con.execute(f"CREATE VIEW label_{name} AS SELECT * FROM read_parquet({_q(corpus_dir / 'labels' / f'{name}.parquet')})")
    return con


def _rows(con: duckdb.DuckDBPyConnection, sql: str) -> list[tuple[Any, ...]]:
    return con.execute(sql).fetchall()


def corpus_stats(corpus_dir: Path) -> dict[str, Any]:
    con = connect(corpus_dir, labels=True)
    s: dict[str, Any] = {}
    s["manifest"] = json.loads((corpus_dir / "manifest.json").read_text(encoding="utf-8"))
    s["totals"] = _rows(con, """
        SELECT count(*) AS rows, count(DISTINCT company_id) AS companies,
               min(date), max(date), count(DISTINCT description) AS distinct_narrations
        FROM transactions""")[0]
    s["profile_bank"] = _rows(con, """
        SELECT profile, bank, count(*) FROM label_companies GROUP BY ALL ORDER BY ALL""")
    s["months"] = _rows(con, """
        SELECT min(n_months), median(n_months), max(n_months),
               min(n_customers) FILTER (WHERE n_customers > 0),
               median(n_customers) FILTER (WHERE n_customers > 0),
               max(n_customers), min(revenue_scale), median(revenue_scale), max(revenue_scale)
        FROM label_companies""")[0]
    s["rows_per_profile"] = _rows(con, """
        SELECT c.profile, count(*) AS rows, round(count(*) / count(DISTINCT t.company_id), 1)
        FROM label_transactions t JOIN label_companies c USING (company_id)
        GROUP BY ALL ORDER BY ALL""")
    s["category_balance"] = _rows(con, """
        SELECT true_category, count(*) AS n, round(100.0 * count(*) / sum(count(*)) OVER (), 2)
        FROM label_transactions GROUP BY ALL ORDER BY n DESC""")
    s["category_by_bank"] = _rows(con, """
        SELECT true_category,
               count(*) FILTER (WHERE f.bank = 'hdfc'), count(*) FILTER (WHERE f.bank = 'icici')
        FROM label_transactions l JOIN transactions f USING (txn_id)
        GROUP BY ALL ORDER BY 1""")
    s["anomalies"] = _rows(con, """
        SELECT anomaly_kind, anomaly_type, count(*) AS rows, count(DISTINCT company_id) AS companies
        FROM label_transactions WHERE anomaly_type IS NOT NULL GROUP BY ALL ORDER BY 1, 3 DESC""")
    s["anomaly_rate"] = _rows(con, """
        SELECT round(100.0 * avg((anomaly_type IS NOT NULL)::INT), 3),
               round(100.0 * avg(coalesce(anomaly_kind = 'ml_only', false)::INT), 3)
        FROM label_transactions""")[0]
    s["ml_selected_vs_injected"] = _rows(con, """
        SELECT flag_type, count(*) FROM label_flags WHERE kind = 'ml_only' GROUP BY ALL ORDER BY 1""")
    s["churn"] = _rows(con, """
        SELECT c.profile, count(*) AS customers,
               count(*) FILTER (WHERE churn_month IS NOT NULL) AS churned,
               round(100.0 * avg((churn_month IS NOT NULL)::INT), 1),
               round(100.0 * avg(signaller::INT) FILTER (WHERE churn_month IS NOT NULL), 1),
               round(avg(c.n_months), 1)
        FROM label_customers k JOIN label_companies c USING (company_id)
        GROUP BY ALL ORDER BY 1""")
    s["health_vs_churn"] = _rows(con, """
        SELECT CASE WHEN health < -1 THEN '1: < -1' WHEN health < 0 THEN '2: -1..0'
                    WHEN health < 1 THEN '3: 0..1' ELSE '4: >= 1' END AS band,
               count(*), round(100.0 * avg((churn_month IS NOT NULL)::INT), 1)
        FROM label_customers GROUP BY ALL ORDER BY 1""")
    s["templates"] = _rows(con, """
        SELECT count(DISTINCT template_id),
               count(DISTINCT template_id) FILTER (WHERE template_id LIKE 'hdfc:%'),
               count(DISTINCT template_id) FILTER (WHERE template_id LIKE 'icici:%'),
               round(100.0 * avg((template_id LIKE '%:inline:%')::INT), 1),
               count(DISTINCT counterparty_key)
        FROM label_transactions""")[0]
    s["narration_len"] = _rows(con, """
        SELECT bank, min(length(description)), median(length(description)), max(length(description))
        FROM transactions GROUP BY ALL ORDER BY 1""")
    s["night_rows"] = _rows(con, """
        SELECT coalesce(l.anomaly_type = 'ml_off_hours_burst', false) AS burst, count(*),
               round(100.0 * avg((hour(f.txn_time) < 6)::INT), 2)
        FROM transactions f JOIN label_transactions l USING (txn_id) GROUP BY ALL ORDER BY 1""")
    return s


def precursor_stats(corpus_dir: Path) -> list[tuple[Any, ...]]:
    """How visible the churn precursors are in observable rows.

    For each customer payment: the gap since that customer's previous payment
    (> 45 days = late or skipped) and whether the amount dropped > 5% vs the
    previous payment. Groups: payments in the 90 days before a churner's
    churn month, all other churner payments, and non-churners' payments.
    Labels are used only to form the groups."""
    con = connect(corpus_dir, labels=True)
    return _rows(con, """
        WITH pay AS (
            SELECT l.company_id, l.true_customer_id AS cid, f.date, f.credit,
                   lag(f.credit) OVER w AS prev_credit, lag(f.date) OVER w AS prev_date
            FROM label_transactions l JOIN transactions f USING (txn_id)
            WHERE l.true_category = 'REVENUE' AND l.true_customer_id LIKE 'cust_%'
              AND l.anomaly_type IS NULL
            WINDOW w AS (PARTITION BY l.company_id, l.true_customer_id ORDER BY f.date, f.txn_time)
        )
        SELECT CASE WHEN k.churn_month IS NULL THEN '3 non-churner'
                    WHEN p.date >= k.churn_month_start - INTERVAL 90 DAY THEN '1 churner, last 90d'
                    ELSE '2 churner, earlier' END AS grp,
               count(*) AS payments,
               round(100.0 * avg((p.date - p.prev_date > 45)::INT), 1) AS pct_gap_gt45,
               round(100.0 * avg((p.credit < 0.95 * p.prev_credit)::INT), 1) AS pct_drop_gt5
        FROM pay p JOIN label_customers k ON k.company_id = p.company_id AND k.customer_id = p.cid
        WHERE p.prev_date IS NOT NULL
        GROUP BY ALL ORDER BY 1""")


def bench(corpus_dir: Path, repeats: int = 3) -> dict[str, float]:
    """Wall-clock for one aggregation (monthly debit/credit per company and
    true category, a join of features and labels) in DuckDB vs pandas."""
    import pandas as pd

    sql = """
        SELECT f.company_id, date_trunc('month', f.date) AS m, l.true_category,
               sum(f.debit) AS dr, sum(f.credit) AS cr, count(*) AS n
        FROM transactions f JOIN label_transactions l USING (txn_id) GROUP BY ALL"""
    out: dict[str, float] = {}
    best = float("inf")
    for _ in range(repeats):
        con = connect(corpus_dir, labels=True)
        t0 = time.perf_counter()
        con.execute(sql).fetchall()
        best = min(best, time.perf_counter() - t0)
    out["duckdb_s"] = best
    best = float("inf")
    for _ in range(repeats):
        t0 = time.perf_counter()
        f = pd.read_parquet(corpus_dir / "features" / "transactions.parquet",
                            columns=["txn_id", "company_id", "date", "debit", "credit"])
        lab = pd.read_parquet(corpus_dir / "labels" / "transactions.parquet",
                              columns=["txn_id", "true_category"])
        df = f.merge(lab, on="txn_id")
        df["m"] = pd.to_datetime(df["date"]).dt.to_period("M")
        df["debit"] = df["debit"].astype(float)
        df["credit"] = df["credit"].astype(float)
        df.groupby(["company_id", "m", "true_category"]).agg(
            dr=("debit", "sum"), cr=("credit", "sum"), n=("txn_id", "size"))
        best = min(best, time.perf_counter() - t0)
    out["pandas_s"] = best
    return out


def _md(rows: list[tuple[Any, ...]], header: list[str]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    for r in rows:
        lines.append("| " + " | ".join(f"{v:,}" if isinstance(v, int) else str(v) for v in r) + " |")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("corpus_dir", type=Path)
    p.add_argument("--bench", action="store_true")
    a = p.parse_args(argv)
    s = corpus_stats(a.corpus_dir)
    m = s["manifest"]
    print(f"corpus {m['corpus_version']} generator v{m['generator_version']} "
          f"args={m['args']} hash={m['corpus_hash']}")
    print("totals (rows, companies, min date, max date, distinct narrations):", s["totals"])
    print("months/customers/scale (min, median, max ...):", s["months"])
    print("\n" + _md(s["profile_bank"], ["profile", "bank", "companies"]))
    print("\n" + _md(s["rows_per_profile"], ["profile", "rows", "rows/company"]))
    print("\n" + _md(s["category_balance"], ["true_category", "rows", "%"]))
    print("\n" + _md(s["category_by_bank"], ["true_category", "hdfc", "icici"]))
    print("\n" + _md(s["anomalies"], ["kind", "anomaly_type", "rows", "companies"]))
    print("anomaly row rate % (any, ml_only):", s["anomaly_rate"])
    print("\n" + _md(s["churn"], ["profile", "customers", "churned", "% churned",
                                  "% churners that signal", "avg months"]))
    print("\n" + _md(s["health_vs_churn"], ["health band", "customers", "% churned"]))
    print("\n" + _md(precursor_stats(a.corpus_dir),
                     ["group", "payments", "% gap > 45 days", "% amount drop > 5%"]))
    print("\ntemplates (all, hdfc, icici, % inline rows, distinct counterparties):", s["templates"])
    print("\n" + _md(s["narration_len"], ["bank", "min len", "median len", "max len"]))
    print("\n" + _md(s["night_rows"], ["off-hours burst row", "rows", "% before 06:00"]))
    if a.bench:
        print("\nbench (best of 3, seconds):", bench(a.corpus_dir))


if __name__ == "__main__":
    main()
