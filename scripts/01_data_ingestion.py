"""
01_data_ingestion.py
--------------------
ETL foundation layer: consolidates all per-asset CSV fragments into a single
clean panel dataset (master_raw_data.csv) ready for downstream cleaning and
event-study analysis. Bundle-sale unit-price correction is intentionally
deferred to 02_cleaning_and_llm.py.

Usage (from project root):
    python scripts/01_data_ingestion.py
"""

import re
import sys
from pathlib import Path

import pandas as pd

# ---------------------------------------------------------------------------
# Path constants — resolved relative to this file so CWD is irrelevant.
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_RAW     = PROJECT_ROOT / "data_raw"
DATA_CLEAN   = PROJECT_ROOT / "data_clean"
CONFIG_PATH  = PROJECT_ROOT / "config_meta.csv"

# ---------------------------------------------------------------------------
# Mercari export schema constants (sourced from data-profiling session).
# Centralised here so any upstream schema change requires only one edit.
# ---------------------------------------------------------------------------
COL_PAGE_URL   = "実サイト商品ページのURL"   # product page URL → item_id extraction
COL_TRANS_DATE = "取引日（日数）"             # e.g. "2025年10月13日(29日)"
COL_PRICE      = "落札価格"                   # clean integer in export, no ¥ symbol
COL_IMAGE      = "サムネイル画像URL"          # CDN thumbnail URL → Vision LLM input
COL_TITLE      = "商品タイトル"               # listing title → keyword pre-filter in 02

# Strips the parenthetical elapsed-days suffix before date parsing;
# handles both half-width "(" and full-width "（" delimiters observed in exports.
_RE_DATE_STRIP = re.compile(r"^(.+?)[\(（]")

# Extracts Mercari item ID from the product page URL path segment.
_RE_ITEM_ID = re.compile(r"/item/(m\w+)")


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _read_csv_with_fallback(path: Path) -> pd.DataFrame:
    """Read a single CSV, falling back to Shift-JIS for pre-2023 Mercari exports."""
    try:
        return pd.read_csv(path, encoding="utf-8")
    except UnicodeDecodeError:
        # Older Mercari bulk exports shipped in Shift-JIS; UTF-8-SIG is also common.
        return pd.read_csv(path, encoding="shift-jis")


def _load_asset_csvs(folder_name: str) -> pd.DataFrame:
    """Glob-merge all CSV fragments under one asset directory into a single frame."""
    asset_dir = DATA_RAW / folder_name
    csv_files = sorted(asset_dir.glob("*.csv"))

    if not csv_files:
        print(f"  [WARN] No CSV files in {asset_dir} — skipping.", file=sys.stderr)
        return pd.DataFrame()

    return pd.concat(
        [_read_csv_with_fallback(f) for f in csv_files],
        ignore_index=True,
    )


def _parse_japanese_dates(series: pd.Series) -> pd.Series:
    """
    Truncate the parenthetical elapsed-days suffix, then parse Japanese-format
    date strings (年月日) to pandas Timestamps. Malformed values → NaT.
    """
    stripped = series.str.extract(_RE_DATE_STRIP, expand=False).fillna(series)
    return pd.to_datetime(stripped, format="%Y年%m月%d日", errors="coerce")


def _extract_item_id(series: pd.Series) -> pd.Series:
    """Pull the Mercari item ID (e.g. 'm14189012135') from the product page URL."""
    return series.str.extract(_RE_ITEM_ID, expand=False)


# ---------------------------------------------------------------------------
# Main ETL pipeline
# ---------------------------------------------------------------------------

def ingest() -> None:
    """
    Orchestrates the full ingestion pipeline:
    config_meta.csv → per-asset CSV merge → field extraction → global dedup →
    master_raw_data.csv.
    """
    DATA_CLEAN.mkdir(exist_ok=True)

    # parse_dates ensures T1/T2 arrive as Timestamps, not raw strings.
    config = pd.read_csv(
        CONFIG_PATH,
        parse_dates=["announce_date", "release_date"],
    )

    all_frames: list[pd.DataFrame] = []
    print("=" * 60)
    print("INGESTION RUN — per-asset raw row counts")
    print("=" * 60)

    for _, row in config.iterrows():
        folder_name: str = row["folder_name"]
        en_name:     str = row["en_name"]
        t1: pd.Timestamp = row["announce_date"]
        t2: pd.Timestamp = row["release_date"]

        raw = _load_asset_csvs(folder_name)
        if raw.empty:
            continue

        # --- Field extraction ---
        raw["item_id"]          = _extract_item_id(raw[COL_PAGE_URL])
        raw["transaction_date"] = _parse_japanese_dates(raw[COL_TRANS_DATE])
        # Int64 (nullable) preserves NaN for rows where price cannot be coerced.
        raw["price_raw"]        = pd.to_numeric(raw[COL_PRICE], errors="coerce").astype("Int64")
        raw["image_url"]        = raw[COL_IMAGE]
        # Title retained for keyword-based bundle pre-filtering in 02_cleaning_and_llm.py.
        raw["title"]            = raw[COL_TITLE].astype(str)

        # --- Asset-level metadata stamp ---
        raw["asset_id"]      = folder_name
        raw["asset_en_name"] = en_name
        raw["T1"]            = t1
        raw["T2"]            = t2

        print(f"  {folder_name:<35} {len(raw):>5} rows")
        all_frames.append(raw)

    if not all_frames:
        print("\n[ERROR] No data loaded. Verify data_raw/ structure.", file=sys.stderr)
        sys.exit(1)

    master = pd.concat(all_frames, ignore_index=True)
    pre_dedup = len(master)

    # Global dedup on item_id — same listing can appear across multiple export
    # batches; keeping first preserves the earliest-scraped record.
    master = master.drop_duplicates(subset=["item_id"], keep="first")
    duplicates_removed = pre_dedup - len(master)

    # Narrow to the exact column set required by downstream scripts.
    output_cols = [
        "item_id",
        "asset_id",
        "asset_en_name",
        "price_raw",
        "transaction_date",
        "title",
        "image_url",
        "T1",
        "T2",
    ]
    master = master[output_cols]

    # Rows with unparseable dates or prices are flagged but retained here;
    # hard filtering is the responsibility of 02_cleaning_and_llm.py.
    n_bad_date  = master["transaction_date"].isna().sum()
    n_bad_price = master["price_raw"].isna().sum()

    output_path = DATA_CLEAN / "master_raw_data.csv"
    master.to_csv(output_path, index=False, encoding="utf-8-sig")

    # --- Summary report ---
    print("=" * 60)
    print("INGESTION SUMMARY")
    print("=" * 60)
    print(f"  Total rows (pre-dedup)  : {pre_dedup:>6}")
    print(f"  Duplicates removed      : {duplicates_removed:>6}")
    print(f"  Final row count         : {len(master):>6}")
    print(f"  Rows with NaT date      : {n_bad_date:>6}  (flagged for cleaning)")
    print(f"  Rows with null price    : {n_bad_price:>6}  (flagged for cleaning)")
    print(f"  Output → {output_path}")
    print("=" * 60)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ingest()
