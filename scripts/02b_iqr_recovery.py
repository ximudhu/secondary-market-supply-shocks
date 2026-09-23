"""
02b_iqr_recovery.py
-------------------
Supplemental LLM re-classification for rows that were removed by the IQR
outlier filter in 02_cleaning_and_llm.py.

Background
~~~~~~~~~~
02's per-asset IQR filter computes a Tukey fence (Q3 + 1.5 × IQR) over the
*entire* price distribution of each asset — all phases combined.  Because
Phase 3 (post-release) typically has many cheap transactions, the fence can
be set artificially low, inadvertently cutting legitimate high-priced rows in
Phase 1 (pre-announcement baseline period).

For example:
  • Asset A: fence ≈ ¥7,168 → cut τ=−127 @ ¥8,400 (only Phase 1 row)
  • Asset C: fence ≈ ¥5,554 → cut τ=−14 to −3 @ ¥6,600–¥9,800 (6 Phase 1 rows)
  • Asset D: fence ≈ ¥772  → cut many pre-T1 rows (¥880–¥1,800) that are
             plausible single-badge prices

Candidate selection
~~~~~~~~~~~~~~~~~~~
This script targets rows satisfying ALL of the following:
  1.  Present in master_raw_data.csv but NOT in master_cleaned_data.csv
  2.  price_raw > per-asset IQR upper fence (re-computed from clean data)
  3.  τ < 0  — pre-release only (Phase 1 + Phase 2 baseline period)
  4.  τ ≥ tau_T1 − PHASE1_PRE_DAYS  — within the extended pre-T1 window

These candidates are run through the same dual-call LLM pipeline as 02
(Call 1: purity check; Call 2 if pure: unit count), but WITHOUT the final
IQR step.  Only LLM-pure rows are written to the output file.

Output
~~~~~~
  data_clean/iqr_recovery_candidates.csv
  — Same column schema as master_cleaned_data.csv so it can be directly
    concatenated: pd.concat([master_cleaned_data, iqr_recovery_candidates])

Usage
~~~~~
  # From project root with .venv activated:
  python scripts/02b_iqr_recovery.py

  # After reviewing output, merge manually:
  python - <<'EOF'
  import pandas as pd
  clean = pd.read_csv("data_clean/master_cleaned_data.csv")
  rec   = pd.read_csv("data_clean/iqr_recovery_candidates.csv")
  combined = pd.concat([clean, rec], ignore_index=True)
  combined.to_csv("data_clean/master_cleaned_data.csv", index=False, encoding="utf-8-sig")
  print(f"Merged: {len(combined)} rows total")
  EOF
"""

import base64
import json
import os
import re
import sys
import time
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv
from openai import OpenAI, APIError, APITimeoutError, RateLimitError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Extended Phase 1 pre-announcement window.
# Rows with τ ∈ [tau_T1 − PHASE1_PRE_DAYS, 0) will be included as candidates.
# 90 days provides more pre-T1 data while limiting temporal-drift risk.
# The Phase 1 boundary in 03_analysis.py can be set independently of this value.
PHASE1_PRE_DAYS: int = 90

# TEST_MODE: processes only TEST_SAMPLE_SIZE rows (chosen to cover A/C/D).
TEST_MODE: bool = False
TEST_SAMPLE_SIZE: int = 4  # 4 rows across different assets to verify pipeline

MODEL: str = "gpt-4o-mini"

# API reliability settings (identical to 02)
MAX_RETRIES: int = 3
RETRY_DELAY_BASE: float = 2.0
REQUEST_TIMEOUT: int = 30

# Inter-row sleep.  This batch is ~58 rows (much smaller than 02's 954-row run),
# so 10s is sufficient to stay well below the 200K TPM limit.
# Estimated TPM at 10s sleep: ~50–80K (safe).
SLEEP_BETWEEN_ROWS: float = 10.0

BUNDLE_KEYWORDS_HARD: tuple[str, ...] = (
    "詰め合わせ", "詰合せ",
    "アソート",
    "福袋",
    "色々",
    "各キャラ",
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT  = Path(__file__).resolve().parent.parent
DATA_RAW      = PROJECT_ROOT / "data_raw"
DATA_CLEAN    = PROJECT_ROOT / "data_clean"
INPUT_RAW_CSV = DATA_CLEAN / "master_raw_data.csv"
INPUT_CLN_CSV = DATA_CLEAN / "master_cleaned_data.csv"
OUTPUT_CSV    = DATA_CLEAN / "iqr_recovery_candidates.csv"

# ---------------------------------------------------------------------------
# Prompts (identical copies from 02 — kept standalone for portability)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a SKU-level purity classifier for a collectibles secondary-market price research project.
Your task: determine whether a listing contains ONLY the exact specific product shown in the reference image.

You will receive two images and three text fields in the user message:
  • Image A — REFERENCE: one specific collectible SKU (a particular series badge, figure, plush, etc.).
  • Image B — LISTING: a Mercari marketplace thumbnail to classify.
  • Reference product name: the Japanese product name that precisely identifies the SKU in Image A.
  • Reference product type: the product category of the reference SKU (e.g. ぬいぐるみ / plush mascot).
  • Listing title: the seller's own title for the item(s) shown in Image B.

Use the reference product name and reference product type as anchors to understand exactly what you are
looking for. Use the listing title as supplementary context about what the seller claims to be selling.

IMPORTANT — keyword inflation: sellers sometimes include extra product-type keywords in titles for search
visibility even when only one product type is physically present. If the listing title mentions a product
type that differs from the Reference product type, do NOT classify as IMPURE based on the title text alone.
Instead, rely on VISUAL EVIDENCE in Image B to determine whether a different product type actually appears.

CLASSIFICATION RULES:

  PURE (is_pure: true):
    Image B contains ONLY units of the EXACT SAME specific product as Image A —
    same character, same series, same design/artwork.
    • Multiple units of that exact item = PURE (e.g. three identical badges from the same series).
    • Slight visual variation due to holographic sheen, lighting, angle, or packaging = PURE.

  IMPURE (is_pure: false) — ANY of the following:
    • A DIFFERENT SERIES or DIFFERENT DESIGN of merchandise appears, even if featuring the same character.
    • A DIFFERENT CHARACTER appears anywhere in the listing.
    • A DIFFERENT PRODUCT TYPE appears (badge mixed with figure, plush, keychain, etc.).
    • Items from a different franchise or IP.
    • Assorted lots, mystery bags, or bulk miscellaneous goods.

  WHEN IN DOUBT → classify as IMPURE. Conservative exclusion protects price-series integrity.

Respond with ONLY valid JSON — no markdown, no text outside the JSON object:
{"is_pure": <true|false>, "reason": "<one concise English sentence>"}
"""

COUNT_SYSTEM_PROMPT = """\
You are a unit counter for a collectibles secondary-market price research project.
You will receive ONE image of a Mercari listing and a product-type label.
Count the number of COMPLETE, INDIVIDUALLY SELLABLE copies of that product type
that are physically present in the image.

Rules:
  • Count only distinct, complete items — not components, accessories, or packaging.
  • Multiple items must be clearly and physically separated to count as more than 1.
  • A single item (even with integral accessories or inside a bag) = 1 unit.
  • WHEN IN DOUBT → default to 1.  Minimum is always 1.

Respond with ONLY valid JSON — no markdown, no text outside the JSON object:
{"quantity": <integer>}
"""

# ---------------------------------------------------------------------------
# Helper functions (identical to 02 — standalone copies)
# ---------------------------------------------------------------------------

def _title_suggests_mixed_bundle(title: str) -> bool:
    return any(kw in title for kw in BUNDLE_KEYWORDS_HARD)


def _find_ref_image(asset_dir: Path) -> Path | None:
    for ext in ("ref.png", "ref.jpg", "ref.jpeg"):
        candidate = asset_dir / ext
        if candidate.exists():
            return candidate
    return None


def _encode_local_image(path: Path) -> str | None:
    if not path.exists():
        return None
    suffix = path.suffix.lower().lstrip(".")
    mime = "jpeg" if suffix in ("jpg", "jpeg") else suffix
    with open(path, "rb") as f:
        b64 = base64.standard_b64encode(f.read()).decode("utf-8")
    return f"data:image/{mime};base64,{b64}"


def _encode_remote_image(url: str) -> str | None:
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "image/jpeg").split(";")[0]
        b64 = base64.standard_b64encode(resp.content).decode("utf-8")
        return f"data:{content_type};base64,{b64}"
    except requests.RequestException as exc:
        print(f"    [WARN] Failed to download image {url}: {exc}", file=sys.stderr)
        return None


def _thumbnail_to_orig_url(thumb_url: str) -> str:
    m = re.search(r"/(m\w+)_(\d+)\.jpg", thumb_url)
    if not m:
        return thumb_url
    item_id, photo_num = m.group(1), m.group(2)
    return f"https://static.mercdn.net/item/detail/orig/photos/{item_id}_{photo_num}.jpg"


def _call_vision_api(
    client: OpenAI,
    ref_b64: str,
    listing_b64: str,
    jp_name: str = "",
    product_type: str = "",
    title: str = "",
) -> dict:
    error_sentinel = {"is_pure": None, "reason": "api_error"}

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (
                                    "Image A is the reference product. "
                                    "Image B is the listing to classify.\n"
                                    + (f"Reference product name: {jp_name}\n" if jp_name else "")
                                    + (f"Reference product type: {product_type}\n" if product_type else "")
                                    + (f"Listing title: {title}" if title else "")
                                ).strip(),
                            },
                            {"type": "image_url", "image_url": {"url": ref_b64, "detail": "low"}},
                            {"type": "image_url", "image_url": {"url": listing_b64, "detail": "auto"}},
                        ],
                    },
                ],
                max_tokens=150,
                temperature=0,
            )
            raw_json = response.choices[0].message.content.strip()
            parsed   = json.loads(raw_json)
            if not {"is_pure", "reason"}.issubset(parsed.keys()):
                raise ValueError(f"Missing keys in LLM response: {parsed}")
            return parsed

        except (RateLimitError, APITimeoutError) as exc:
            wait = RETRY_DELAY_BASE ** attempt
            print(f"    [RETRY {attempt}/{MAX_RETRIES}] Rate-limit/timeout: {exc}. Waiting {wait:.1f}s…",
                  file=sys.stderr)
            time.sleep(wait)

        except APIError as exc:
            print(f"    [ERROR] OpenAI API error: {exc}", file=sys.stderr)
            return error_sentinel

        except (json.JSONDecodeError, ValueError) as exc:
            print(f"    [ERROR] JSON parse failure: {exc}", file=sys.stderr)
            return error_sentinel

    print(f"    [ERROR] Max retries reached for this row.", file=sys.stderr)
    return error_sentinel


def _count_items_api(client: OpenAI, listing_b64: str, product_type: str = "") -> int:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": COUNT_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": (f"Product type: {product_type}\n" if product_type else "")
                                        + "Count the individually sellable items of this type in the image.",
                            },
                            {"type": "image_url", "image_url": {"url": listing_b64, "detail": "auto"}},
                        ],
                    },
                ],
                max_tokens=50,
                temperature=0,
            )
            raw = response.choices[0].message.content.strip()
            qty = int(json.loads(raw).get("quantity", 1))
            return max(1, qty)

        except (RateLimitError, APITimeoutError) as exc:
            wait = RETRY_DELAY_BASE ** attempt
            print(f"    [RETRY {attempt}/{MAX_RETRIES}] Count rate-limit/timeout: {exc}. Waiting {wait:.1f}s…",
                  file=sys.stderr)
            time.sleep(wait)

        except (APIError, json.JSONDecodeError, ValueError, KeyError) as exc:
            print(f"    [ERROR] Count failure: {exc}", file=sys.stderr)
            return 1

    print(f"    [ERROR] Count max retries reached.", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# Candidate selection
# ---------------------------------------------------------------------------

def _select_candidates(raw_df: pd.DataFrame, clean_df: pd.DataFrame) -> pd.DataFrame:
    """
    Return the subset of raw_df that are:
      1. Not already in master_cleaned_data (by item_id)
      2. price_raw > per-asset IQR upper fence (re-computed from clean data)
      3. τ < 0 (pre-T2 only — baseline period)
      4. τ ≥ tau_T1 − PHASE1_PRE_DAYS (within extended pre-T1 window)
    """
    # τ and tau_T1 columns
    raw_df = raw_df.copy()
    raw_df["tau"]    = (raw_df["transaction_date"] - raw_df["T2"]).dt.days
    raw_df["tau_T1"] = (raw_df["T1"] - raw_df["T2"]).dt.days

    # --- Filter 1: not already clean ---
    clean_ids = set(clean_df["item_id"].dropna().astype(str))
    raw_df = raw_df[~raw_df["item_id"].astype(str).isin(clean_ids)].copy()

    # --- Filter 2: price above per-asset IQR fence ---
    fences: dict[str, float] = {}
    for asset, grp in clean_df.groupby("asset_id"):
        col = grp["true_unit_price"].dropna()
        if len(col) >= 4:
            q1, q3 = col.quantile(0.25), col.quantile(0.75)
            fences[asset] = q3 + 1.5 * (q3 - q1)

    raw_df["iqr_fence"] = raw_df["asset_id"].map(fences)
    raw_df = raw_df[raw_df["price_raw"] > raw_df["iqr_fence"]].copy()

    # --- Filter 3: pre-T2 only ---
    raw_df = raw_df[raw_df["tau"] < 0].copy()

    # --- Filter 4: within extended Phase 1 / Phase 2 window ---
    raw_df = raw_df[raw_df["tau"] >= raw_df["tau_T1"] - PHASE1_PRE_DAYS].copy()

    return raw_df.drop(columns=["tau", "tau_T1", "iqr_fence"])


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def recover() -> None:
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("[ERROR] OPENAI_API_KEY not found in environment / .env file.", file=sys.stderr)
        sys.exit(1)
    client = OpenAI(api_key=api_key)

    # --- Load data ---
    raw_df   = pd.read_csv(INPUT_RAW_CSV, parse_dates=["transaction_date", "T1", "T2"])
    clean_df = pd.read_csv(INPUT_CLN_CSV, parse_dates=["transaction_date", "T1", "T2"])
    print(f"Loaded raw: {len(raw_df)} rows  |  clean: {len(clean_df)} rows")

    # --- Select candidates ---
    candidates = _select_candidates(raw_df, clean_df)
    print(f"\nCandidates selected (above IQR fence, pre-T2, within T1−{PHASE1_PRE_DAYS}d window):")
    for asset, grp in candidates.groupby("asset_id"):
        print(f"  {asset:<35} {len(grp):>3} rows  "
              f"τ ∈ [{grp['tau'].min() if 'tau' in grp else '?'}, "
              f"{grp['tau'].max() if 'tau' in grp else '?'}]  "
              f"price ¥{candidates[candidates['asset_id']==asset]['price_raw'].min():.0f}–"
              f"¥{candidates[candidates['asset_id']==asset]['price_raw'].max():.0f}")

    # Re-compute tau for display (was dropped from candidates; recompute inline)
    candidates = candidates.copy()
    candidates["_tau"]    = (candidates["transaction_date"] - candidates["T2"]).dt.days
    candidates["_tau_T1"] = (candidates["T1"] - candidates["T2"]).dt.days

    # Show per-asset tau info
    print()
    for asset, grp in candidates.groupby("asset_id"):
        print(f"  {asset:<35} τ ∈ [{grp['_tau'].min()}, {grp['_tau'].max()}]")

    candidates = candidates.drop(columns=["_tau", "_tau_T1"])

    print(f"\nTotal candidates: {len(candidates)}")

    if TEST_MODE:
        print(f"\n{'='*60}")
        print(f"  TEST_MODE = True  |  Sampling up to {TEST_SAMPLE_SIZE} rows")
        print(f"  Set TEST_MODE = False for the full {len(candidates)}-row run")
        print(f"{'='*60}\n")
        candidates = candidates.sample(n=min(TEST_SAMPLE_SIZE, len(candidates)), random_state=42)

    # --- Load config meta ---
    config_meta    = pd.read_csv(PROJECT_ROOT / "config_meta.csv")
    jp_name_map    = dict(zip(config_meta["folder_name"], config_meta["jp_name"]))
    product_type_map = dict(zip(config_meta["folder_name"], config_meta["product_type"]))

    # --- Pre-load reference images ---
    ref_cache: dict[str, str | None] = {}
    for asset_id in candidates["asset_id"].unique():
        ref_path = _find_ref_image(DATA_RAW / asset_id)
        encoded  = _encode_local_image(ref_path) if ref_path else None
        if encoded is None:
            print(f"  [WARN] No ref image for {asset_id}.", file=sys.stderr)
        else:
            print(f"  [REF] {asset_id} → {ref_path.name}")
        ref_cache[asset_id] = encoded

    # --- LLM loop ---
    results: list[dict] = []
    total = len(candidates)

    print(f"\n{'─'*70}")
    print(f"{'#':>5}  {'item_id':<16}  {'asset_id':<30}  {'τ':>5}  {'¥price':>7}  {'result':<10}  reason")
    print(f"{'─'*70}")

    n_title_skipped = 0

    for _, row in candidates.iterrows():
        asset_id  = row["asset_id"]
        item_id   = row["item_id"]
        image_url = row["image_url"]
        title     = str(row.get("title", ""))
        tau_val   = int((row["transaction_date"] - row["T2"]).days)
        price_val = int(row["price_raw"])

        if _title_suggests_mixed_bundle(title):
            result = {"is_pure": False, "quantity": 0, "reason": "title_keyword_filter"}
            n_title_skipped += 1
        else:
            ref_b64     = ref_cache.get(asset_id)
            listing_b64 = _encode_remote_image(_thumbnail_to_orig_url(image_url)) if image_url else None

            if ref_b64 is None or listing_b64 is None:
                result = {"is_pure": None, "quantity": None, "reason": "missing_image"}
            else:
                result = _call_vision_api(
                    client, ref_b64, listing_b64,
                    jp_name=jp_name_map.get(asset_id, ""),
                    product_type=product_type_map.get(asset_id, ""),
                    title=title,
                )
                if result.get("is_pure") is True:
                    result["quantity"] = _count_items_api(
                        client,
                        listing_b64,
                        product_type=product_type_map.get(asset_id, ""),
                    )
                else:
                    result["quantity"] = 0

        results.append({
            **row.to_dict(),
            "is_pure":      result.get("is_pure"),
            "llm_quantity": result.get("quantity"),
            "llm_reason":   result.get("reason"),
        })

        row_num  = len(results)
        is_pure  = result.get("is_pure")
        qty      = result.get("quantity", "—")
        reason   = (result.get("reason") or "")[:45]
        pure_str = "✓ pure  " if is_pure else ("✗ impure" if is_pure is False else "? error ")
        tag      = "[T]" if reason == "title_keyword_filter" else "   "
        print(f"{row_num:>5}  {item_id:<16}  {asset_id:<30}  {tau_val:>5}  ¥{price_val:>6}  {pure_str}  {tag} {reason}")

        time.sleep(SLEEP_BETWEEN_ROWS)

    print(f"{'─'*70}\n")

    # --- Build result DataFrame ---
    result_df = pd.DataFrame(results)

    n_api_errors = result_df["is_pure"].isna().sum()
    result_df = result_df[result_df["is_pure"].notna()].copy()

    n_impure = (result_df["is_pure"] == False).sum()  # noqa: E712
    result_df = result_df[result_df["is_pure"]].copy()

    result_df["llm_quantity"] = pd.to_numeric(result_df["llm_quantity"], errors="coerce")
    n_bad_qty = (result_df["llm_quantity"].isna() | (result_df["llm_quantity"] < 1)).sum()
    result_df = result_df[result_df["llm_quantity"] >= 1].copy()

    result_df["true_unit_price"] = (result_df["price_raw"] / result_df["llm_quantity"]).round(2)

    # NOTE: NO IQR FILTER HERE — that is the entire purpose of this script.

    # --- Finalise output columns (same schema as master_cleaned_data.csv) ---
    output_cols = [
        "item_id", "asset_id", "asset_en_name",
        "price_raw", "llm_quantity", "true_unit_price",
        "transaction_date", "image_url",
        "llm_reason", "T1", "T2",
    ]
    # Guard: only keep columns that exist (raw_df may have extra columns)
    output_cols = [c for c in output_cols if c in result_df.columns]
    result_df = result_df[output_cols]

    result_df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")

    # --- Summary ---
    print(f"{'='*60}")
    print("IQR RECOVERY SUMMARY")
    print(f"{'='*60}")
    print(f"  Candidates processed    : {total:>6}")
    print(f"  Title-filtered (no LLM) : {n_title_skipped:>6}")
    print(f"  API errors / no image   : {n_api_errors:>6}")
    print(f"  Impure / mixed listings : {n_impure:>6}")
    print(f"  Invalid quantity (LLM)  : {n_bad_qty:>6}")
    print(f"  Recovered pure rows     : {len(result_df):>6}  ← new rows to add to clean data")
    print(f"  Output → {OUTPUT_CSV}")
    print(f"{'='*60}")

    if len(result_df) > 0:
        print("\nRecovered rows by asset:")
        for asset, grp in result_df.groupby("asset_id"):
            tau_col = (pd.to_datetime(grp["transaction_date"]) - pd.to_datetime(grp["T2"])).dt.days
            print(f"  {asset:<35} {len(grp):>3} rows  "
                  f"τ ∈ [{tau_col.min()}, {tau_col.max()}]  "
                  f"unit_price ¥{grp['true_unit_price'].min():.0f}–¥{grp['true_unit_price'].max():.0f}")
        print()
        print("Next step: review the output CSV, then merge into master_cleaned_data.csv:")
        print("  python scripts/02b_iqr_recovery.py  # already done")
        print("  # Inspect: open data_clean/iqr_recovery_candidates.csv")
        print("  # Merge:   see docstring at top of this file for one-liner")

    if TEST_MODE:
        print("\n⚠  TEST_MODE is ON — set TEST_MODE = False for the full recovery run.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    recover()
