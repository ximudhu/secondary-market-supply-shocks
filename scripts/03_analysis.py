"""
03_analysis.py
--------------
Event-study analysis engine for secondary-market supply-shock research.

Anchors a relative time axis at T2 (release date = Day 0), applies a
Phase-3-only IQR fence, computes daily medians, normalises prices against
a pre-T2 baseline (mean of daily medians), and writes analysis_output.csv.

Input  : data_clean/master_cleaned_data.csv   (439 rows, post-02b merge)
Output : data_clean/analysis_output.csv

Processing flow (§4.3 HANDOVER.md):
    Step 0  Window filter          τ ∈ [tau_T1 − 90, max_post_day]
    Step 1  Phase assignment       Phase 1 / 2 / 3
    Step 2  IQR filter             Phase 3 only (pre-T2 NEVER filtered)
    Step 3  Daily aggregation      median + count per (asset_id, τ)
    Step 4  Baseline mean          mean(daily_median for τ < 0), day-weighted
    Step 5  Normalisation          normalized_price = daily_median / baseline_mean
    Step 6  Output                 CSV + console summary table
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from pathlib import Path

# ─────────────────────────────────────────────────────────────────────────────
# PATHS
# ─────────────────────────────────────────────────────────────────────────────
ROOT       = Path(__file__).parent.parent
DATA_DIR   = ROOT / "data_clean"
INPUT_CSV  = DATA_DIR / "master_cleaned_data.csv"
OUTPUT_CSV = DATA_DIR / "analysis_output.csv"

# ─────────────────────────────────────────────────────────────────────────────
# ASSET CONFIG
# ─────────────────────────────────────────────────────────────────────────────

# Inclusive right-truncation boundary (days relative to T2) per asset
# See §1.3 / §4.3 Table "各资产截断日"
ASSET_MAX_POST: dict[str, int] = {
    "asset_a_action_figure":     120,
    "asset_b_acrylic_stand":      50,   # right-truncated; post clean = 1 row
    "asset_c_plush_mascot":      120,
    "asset_d_holographic_badge": 120,
    "asset_e_classic_badge":     141,
    "asset_f_lottery_plush":      71,   # right-truncated; post clean = 6 rows
}

# Annotation for baseline quality — printed in summary, quoted in report Table 1
BASELINE_NOTES: dict[str, str] = {
    "asset_a_action_figure": (
        "pre = Phase1+Phase2; announce-effect contaminates baseline; "
        "Phase1 sparse (02b recovered 2 rows)"
    ),
    "asset_b_acrylic_stand": (
        "pre = Phase1+Phase2; post = 1 day only (right-truncated); low confidence"
    ),
    "asset_c_plush_mascot": (
        "CLEANEST — T1=T2 so entire pre is Phase1 (zero announce-effect); "
        "02b recovered 8 rows; use as pure scarcity-premium reference"
    ),
    "asset_d_holographic_badge": (
        "Phase1 ≈6 days of data; multiple badge-version caveat (§3.5)"
    ),
    "asset_e_classic_badge": (
        "pre = Phase1+Phase2; baseline reliable"
    ),
    "asset_f_lottery_plush": (
        "post_median > pre_median (anomaly); post = 6 raw rows — not robust"
    ),
}

# ─────────────────────────────────────────────────────────────────────────────
# HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def assign_phase(tau: int, tau_T1: int) -> str:
    """
    Phase 1 (pure scarcity baseline) : tau_T1 − 90 ≤ τ < tau_T1
    Phase 2 (expectation period)     : tau_T1 ≤ τ < 0
    Phase 3 (post-release supply shock): 0 ≤ τ ≤ max_post_day

    Asset C edge case: tau_T1 = 0 → Phase 2 range [0, 0) is empty;
    all pre-T2 rows (τ < 0) fall into Phase 1 automatically.
    """
    if tau >= 0:
        return "Phase 3"
    elif tau >= tau_T1:
        # For Asset C, tau_T1=0 and tau<0, so this branch is never reached
        return "Phase 2"
    else:
        return "Phase 1"


def apply_iqr_filter_phase3(
    df: pd.DataFrame,
    price_col: str = "true_unit_price",
) -> pd.DataFrame:
    """
    Tukey 1.5×IQR bidirectional fence, applied to Phase 3 rows (τ ≥ 0) ONLY.

    Design rationale (§1.5 / §4.3 Step 2):
    - pre-T2 rows (τ < 0) pass through UNCHANGED:
        • scarcity premiums are structurally higher pre-release;
        • the combined-phase fence in script 02 would compress them artificially;
        • 02b-recovered rows must not be re-filtered.
    - Phase-3-only fence is narrower than 02's combined-phase fence, so it
      catches post-release anomalies that slipped through (02b's pre-T2 high
      prices widen the combined fence, reducing its Phase-3 sensitivity).
    - Bidirectional: removes both anomalous highs and lows in Phase 3.
    - Min-rows guard: assets with Phase-3 n < 4 are skipped entirely.
    """
    pre_rows  = df[df["tau"] < 0].copy()
    post_rows = df[df["tau"] >= 0].copy()

    kept_parts: list[pd.DataFrame] = []
    total_removed = 0

    for asset_id, grp in post_rows.groupby("asset_id"):
        n = len(grp)
        if n < 4:
            print(f"    [IQR skip] {asset_id}: Phase-3 n={n} < 4, no filter")
            kept_parts.append(grp)
            continue

        q1  = grp[price_col].quantile(0.25)
        q3  = grp[price_col].quantile(0.75)
        iqr = q3 - q1
        lo  = q1 - 1.5 * iqr
        hi  = q3 + 1.5 * iqr

        mask    = (grp[price_col] >= lo) & (grp[price_col] <= hi)
        removed = int((~mask).sum())
        total_removed += removed

        if removed:
            print(
                f"    [IQR] {asset_id}: removed {removed} Phase-3 rows "
                f"(fence [¥{lo:,.0f} – ¥{hi:,.0f}])"
            )
        kept_parts.append(grp[mask])

    post_clean = (
        pd.concat(kept_parts, ignore_index=True)
        if kept_parts
        else post_rows.iloc[0:0]
    )
    print(f"    → Total Phase-3 rows removed by IQR: {total_removed}")
    return pd.concat([pre_rows, post_clean], ignore_index=True)


# ─────────────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    sep = "─" * 68

    print("=" * 68)
    print("03_analysis.py  —  Secondary Market Supply Shock: Event Study")
    print("=" * 68)

    # ── Load ──────────────────────────────────────────────────────────────────
    df = pd.read_csv(
        INPUT_CSV,
        parse_dates=["transaction_date", "T1", "T2"],
    )
    print(f"\nInput : {INPUT_CSV}")
    print(f"Rows  : {len(df)}")

    # Sanity gate: verify post-02b merge row count
    expected_rows = 439
    if len(df) != expected_rows:
        print(
            f"  ⚠️  Expected {expected_rows} rows (post-02b), "
            f"got {len(df)}. Continuing anyway."
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Step 0 — Window Filter
    # ─────────────────────────────────────────────────────────────────────────
    print(f"\n{sep}")
    print("[Step 0]  τ computation + window filter")
    print(f"          Retain τ ∈ [tau_T1 − 90,  max_post_day]  per asset")
    print(f"          ⚠️  Lower bound = T1−90 (not T1−30) to keep 02b-recovered")
    print(f"              Asset-C rows at τ=−45 and τ=−36")
    print(sep)

    df["tau"]          = (df["transaction_date"] - df["T2"]).dt.days
    df["tau_T1"]       = (df["T1"] - df["T2"]).dt.days
    df["max_post_day"] = df["asset_id"].map(ASSET_MAX_POST)

    before = len(df)
    df = df[
        (df["tau"] >= df["tau_T1"] - 90) &
        (df["tau"] <= df["max_post_day"])
    ].copy().reset_index(drop=True)
    removed_win = before - len(df)

    print(f"\n  Rows before filter : {before}")
    print(f"  Rows after  filter : {len(df)}  ({removed_win} out-of-window removed)")
    print()
    print(f"  {'Asset':<32s} {'n':>4}  τ-range       tau_T1  window")
    print(f"  {'─'*32} {'─'*4}  {'─'*12}  {'─'*6}  {'─'*16}")
    for aid, grp in df.groupby("asset_id"):
        t1v  = grp["tau_T1"].iloc[0]
        print(
            f"  {aid:<32s} {len(grp):>4d}  "
            f"[{grp['tau'].min():4d}, {grp['tau'].max():4d}]   "
            f"{t1v:>5d}   [{t1v - 90:5d}, {ASSET_MAX_POST[aid]:4d}]"
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Step 1 — Phase Assignment
    # ─────────────────────────────────────────────────────────────────────────
    print(f"\n{sep}")
    print("[Step 1]  Phase assignment")
    print(f"          Phase 1: [tau_T1−90, tau_T1)   pure scarcity baseline")
    print(f"          Phase 2: [tau_T1, 0)            announce→release window")
    print(f"          Phase 3: [0, max_post_day]      post-release supply shock")
    print(f"          Asset C: tau_T1=0 → Phase2 empty; all pre-T2 = Phase1")
    print(sep)

    df["phase"] = [
        assign_phase(int(row["tau"]), int(row["tau_T1"]))
        for _, row in df.iterrows()
    ]

    phase_tbl = (
        df.groupby(["asset_id", "phase"])
        .size()
        .unstack(fill_value=0)
        .reindex(columns=["Phase 1", "Phase 2", "Phase 3"], fill_value=0)
    )
    print("\n" + phase_tbl.to_string())

    # ─────────────────────────────────────────────────────────────────────────
    # Step 2 — Phase-3-only IQR Filter
    # ─────────────────────────────────────────────────────────────────────────
    print(f"\n{sep}")
    print("[Step 2]  Phase-3-only IQR filter  (pre-T2 rows UNTOUCHED)")
    print(sep)

    df = apply_iqr_filter_phase3(df, price_col="true_unit_price")
    print(f"\n  Rows remaining: {len(df)}")

    # ─────────────────────────────────────────────────────────────────────────
    # Step 3 — Daily Aggregation by (asset_id, τ)
    # ─────────────────────────────────────────────────────────────────────────
    print(f"\n{sep}")
    print("[Step 3]  Daily aggregation → median + count per (asset_id, τ)")
    print(sep)

    # Group by everything constant within an (asset_id, tau) pair so we
    # can keep metadata columns without a second merge.
    daily = (
        df.groupby(
            ["asset_id", "asset_en_name", "tau", "tau_T1",
             "max_post_day", "T1", "T2", "phase"],
            sort=True,
        )
        .agg(
            n_transactions=("true_unit_price", "count"),
            daily_median=("true_unit_price", "median"),
        )
        .reset_index()
    )

    # Calendar date = T2 + τ days
    daily["date"] = (
        daily["T2"] + pd.to_timedelta(daily["tau"].astype(int), unit="D")
    ).dt.date

    print(f"\n  Daily (asset_id, τ) rows generated: {len(daily)}")
    day_sum = (
        daily.groupby("asset_id")
        .agg(pre_days=("tau", lambda s: (s < 0).sum()),
             post_days=("tau", lambda s: (s >= 0).sum()))
    )
    print("\n" + day_sum.to_string())

    # ─────────────────────────────────────────────────────────────────────────
    # Step 4 — Per-Asset Baseline Mean
    # ─────────────────────────────────────────────────────────────────────────
    print(f"\n{sep}")
    print("[Step 4]  Per-asset baseline mean")
    print(f"          baseline_mean = mean(daily_median for τ < 0)")
    print(f"          → each *day* weighted equally (not each transaction)")
    print(f"          → covers Phase1 + Phase2 combined (all pre-T2 data)")
    print(sep)

    pre_daily = daily[daily["tau"] < 0]

    baseline_stats = (
        pre_daily.groupby("asset_id")
        .agg(
            n_pre_days=("daily_median", "count"),
            baseline_mean=("daily_median", "mean"),
        )
        .reset_index()
    )

    # n_pre_rows: raw transaction count pre-T2 (before daily aggregation)
    pre_row_counts = (
        df[df["tau"] < 0]
        .groupby("asset_id")
        .size()
        .rename("n_pre_rows")
        .reset_index()
    )
    baseline_stats = baseline_stats.merge(pre_row_counts, on="asset_id", how="left")
    baseline_stats["n_pre_rows"] = baseline_stats["n_pre_rows"].fillna(0).astype(int)
    baseline_stats["note"]       = baseline_stats["asset_id"].map(BASELINE_NOTES)

    # ── Print baseline table (→ Report Table 1) ───────────────────────────────
    print("\n  Per-Asset Baseline Summary  (→ Report Table 1)")
    print("  " + "─" * 80)
    hdr = f"  {'Asset ID':<32s} {'n_pre_rows':>10} {'n_pre_days':>10} {'baseline_mean':>14}"
    print(hdr)
    print("  " + "─" * 80)
    for _, row in baseline_stats.iterrows():
        print(
            f"  {row['asset_id']:<32s} "
            f"{row['n_pre_rows']:>10d} "
            f"{row['n_pre_days']:>10d} "
            f"¥{row['baseline_mean']:>13,.1f}"
        )
        print(f"      ↳ {row['note']}")
    print("  " + "─" * 80)

    # Merge baseline_mean (and auxiliary columns) back into daily
    daily = daily.merge(
        baseline_stats[["asset_id", "baseline_mean", "n_pre_rows", "n_pre_days"]],
        on="asset_id",
        how="left",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Step 5 — Normalisation
    # ─────────────────────────────────────────────────────────────────────────
    print(f"\n{sep}")
    print("[Step 5]  Normalisation: normalized_price = daily_median / baseline_mean")
    print(sep)

    daily["normalized_price"] = daily["daily_median"] / daily["baseline_mean"]

    # Sanity: mean normalised price over pre-T2 days must be ≈ 1.0 per asset
    print("\n  Normalised pre-T2 mean per asset (construction guarantee ≈ 1.000):")
    pre_norm = daily[daily["tau"] < 0].groupby("asset_id")["normalized_price"].mean()
    for aid, val in pre_norm.items():
        flag = "" if abs(val - 1.0) < 1e-9 else "  ⚠️ deviation detected"
        print(f"    {aid}: {val:.6f}{flag}")

    # ─────────────────────────────────────────────────────────────────────────
    # Step 6 — Output
    # ─────────────────────────────────────────────────────────────────────────
    print(f"\n{sep}")
    print("[Step 6]  Write analysis_output.csv")
    print(sep)

    # reliable_day: ≥3 transactions → solid line/point in visualisation
    # <3 transactions → hollow point / dashed (sparse/unreliable day)
    daily["reliable_day"] = daily["n_transactions"] >= 3

    out_cols = [
        # Identity
        "asset_id",
        "asset_en_name",
        # Time axis
        "tau",
        "date",
        "phase",
        # Volume & quality
        "n_transactions",
        "reliable_day",
        # Price data
        "daily_median",
        "baseline_mean",
        "normalized_price",
        # Baseline metadata (for report tables)
        "n_pre_rows",
        "n_pre_days",
        # Window config (for visualisation layer)
        "tau_T1",
        "max_post_day",
        "T1",
        "T2",
    ]
    daily_out = (
        daily[out_cols]
        .sort_values(["asset_id", "tau"])
        .reset_index(drop=True)
    )

    daily_out.to_csv(OUTPUT_CSV, index=False)
    print(f"\n  Saved → {OUTPUT_CSV}")
    print(f"  Total rows in output: {len(daily_out)}")

    # ── Final diagnostic summary ──────────────────────────────────────────────
    print(f"\n  {'─'*88}")
    print(
        f"  {'Asset':<32s} {'pre_days':>8} {'post_days':>9} "
        f"{'baseline':>11} {'post_med_norm':>14}  flags"
    )
    print(f"  {'─'*88}")
    for aid, grp in daily_out.groupby("asset_id"):
        pre  = grp[grp["tau"] < 0]
        post = grp[grp["tau"] >= 0]
        bm   = grp["baseline_mean"].iloc[0]
        pmn  = post["normalized_price"].median() if len(post) > 0 else float("nan")
        flags: list[str] = []
        if len(post) <= 5:
            flags.append("⚠️ post_days≤5")
        if not np.isnan(pmn) and pmn > 1.0:
            flags.append("⚠️ post>pre (anomaly)")
        print(
            f"  {aid:<32s} {len(pre):>8d} {len(post):>9d} "
            f"¥{bm:>10,.0f} {pmn:>14.3f}  {'  '.join(flags)}"
        )
    print(f"  {'─'*88}")

    print("\n✅  03_analysis.py complete.\n")


# ─────────────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    main()
