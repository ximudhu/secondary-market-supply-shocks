"""
02_cleaning_and_llm.py
----------------------
Cleaning layer: multimodal LLM validation for bundle-sale detection, followed
by IQR-based outlier removal on the recovered per-unit price.

Pipeline:
    master_raw_data.csv
        → Vision LLM (purity check + unit count per listing)
        → Drop impure / mixed-bundle rows
        → Derive true_unit_price = price_raw / llm_quantity
        → IQR outlier removal per asset_id
        → master_cleaned_data.csv

Usage (from project root, with .venv activated):
    python scripts/02_cleaning_and_llm.py

Flip TEST_MODE to False for the full 954-row production run.
"""

import base64
import json
import os
import random
import re
import sys
import time
from pathlib import Path

import pandas as pd
import requests
from dotenv import load_dotenv
from openai import OpenAI, APIError, APITimeoutError, RateLimitError

# ---------------------------------------------------------------------------
# Global control switches
# ---------------------------------------------------------------------------

# Safety gate: when True, processes only 2 rows per asset_id for API validation.
TEST_MODE: bool = False

TEST_SAMPLE_PER_ASSET: int = 2   # rows sampled per asset in TEST_MODE
TEST_RANDOM_SEED: int = 7        # change seed here to draw a different test sample
MODEL: str = "gpt-4o-mini"       # OpenAI vision-capable model (upgraded from gpt-4.1-nano)

# Hard-filter keywords: terms that UNAMBIGUOUSLY signal a mixed-character or
# mixed-product lot. Deliberately excludes ambiguous terms like セット/まとめ/複数,
# which also appear in legitimate single-character multi-unit listings on Mercari
# (e.g. "孤爪研磨 3枚セット") — those are sent to the LLM for visual adjudication.
BUNDLE_KEYWORDS_HARD: tuple[str, ...] = (
    "詰め合わせ", "詰合せ",   # explicit assorted lots — reliably mixed
    "アソート",                # assortment product — always mixed
    "福袋",                    # mystery lucky bag — always mixed
    "色々",                    # "various things" — reliably mixed
    "各キャラ",                # "each character" — explicitly multi-character
    # Excluded: まとめ売り / まとめ出品 — also used for same-SKU multi-unit bulk sales
    # Excluded: 各種 — can mean "various sizes/colours" of the same item
    # Excluded: ガラポン / ランダム — describe lottery-origin; not bundle structure
    # Excluded: セット / まとめ / 複数 — too common in single-character multi-unit listings
)
MAX_RETRIES: int = 3             # max API call attempts before marking as error
RETRY_DELAY_BASE: float = 2.0    # base delay (seconds) for exponential backoff
REQUEST_TIMEOUT: int = 30        # per-image HTTP download timeout (seconds)
# Inter-row sleep to avoid TPM rate-limit bursts.
# 0.3s is enough for TEST_MODE (12 rows).  For the full 954-row production run,
# set to 1.0s: two API calls per pure row push average token rate above 200K TPM
# at shorter intervals.
SLEEP_BETWEEN_ROWS: float = 20.0

# ---------------------------------------------------------------------------
# Path constants
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_RAW     = PROJECT_ROOT / "data_raw"
DATA_CLEAN   = PROJECT_ROOT / "data_clean"
INPUT_CSV    = DATA_CLEAN / "master_raw_data.csv"
OUTPUT_CSV   = DATA_CLEAN / "master_cleaned_data.csv"

# ---------------------------------------------------------------------------
# System prompt — engineered for structured, unambiguous JSON output.
# The prompt is intentionally terse and instruction-first to minimise
# hallucination and maximise compliance with the JSON-only constraint.
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
      (e.g. Image A shows an All-Star badge; Image B shows that badge + a different series badge = IMPURE)
    • A DIFFERENT CHARACTER appears anywhere in the listing.
    • A DIFFERENT PRODUCT TYPE appears (badge mixed with figure, plush, keychain, etc.).
    • Items from a different franchise or IP.
    • Assorted lots, mystery bags, or bulk miscellaneous goods.

  WHEN IN DOUBT → classify as IMPURE. Conservative exclusion protects price-series integrity.

Respond with ONLY valid JSON — no markdown, no text outside the JSON object:
{"is_pure": <true|false>, "reason": "<one concise English sentence>"}
"""

# ---------------------------------------------------------------------------
# Count prompt — used in a SEPARATE second API call that receives only the
# listing image (Image B), with NO reference image.  Isolating the count call
# from the purity-check call eliminates the cross-image confusion that caused
# gpt-4o-mini to systematically return qty=2 when Image A and Image B look
# visually similar (e.g. same product in a similar transparent bag).
# ---------------------------------------------------------------------------
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
# Helper: title-based pre-filters (avoid unnecessary LLM calls)
# ---------------------------------------------------------------------------

def _title_suggests_mixed_bundle(title: str) -> bool:
    """
    Return True only for titles containing UNAMBIGUOUS mixed-bundle signals.
    Ambiguous terms (セット, まとめ, 複数) are intentionally excluded because they
    also appear in legitimate single-character multi-unit listings; the LLM handles those.
    """
    return any(kw in title for kw in BUNDLE_KEYWORDS_HARD)


# ---------------------------------------------------------------------------
# Helper: image → base64 data URI
# ---------------------------------------------------------------------------

def _find_ref_image(asset_dir: Path) -> Path | None:
    """
    Return the first existing reference image under asset_dir.
    Priority: .png → .jpg → .jpeg; avoids hardcoding a single extension.
    """
    for ext in ("ref.png", "ref.jpg", "ref.jpeg"):
        candidate = asset_dir / ext
        if candidate.exists():
            return candidate
    return None


def _encode_local_image(path: Path) -> str | None:
    """Read a local image file and return a base64-encoded data URI string."""
    if not path.exists():
        return None
    suffix = path.suffix.lower().lstrip(".")
    # Normalise jpg → jpeg for MIME compliance; all other extensions used as-is.
    mime = "jpeg" if suffix in ("jpg", "jpeg") else suffix
    with open(path, "rb") as f:
        b64 = base64.standard_b64encode(f.read()).decode("utf-8")
    return f"data:image/{mime};base64,{b64}"


def _encode_remote_image(url: str) -> str | None:
    """Download a remote image and return a base64-encoded data URI string."""
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
    """Convert a Mercari CDN thumbnail URL to the original full-resolution image URL.

    Thumbnail pattern:
        https://static.mercdn.net/thumb/item/jpeg/{item_id}_{n}.jpg?{timestamp}
    Orig pattern:
        https://static.mercdn.net/item/detail/orig/photos/{item_id}_{n}.jpg
    Falls back to the input URL unchanged if the pattern does not match.
    """
    m = re.search(r"/(m\w+)_(\d+)\.jpg", thumb_url)
    if not m:
        return thumb_url
    item_id, photo_num = m.group(1), m.group(2)
    return f"https://static.mercdn.net/item/detail/orig/photos/{item_id}_{photo_num}.jpg"


# ---------------------------------------------------------------------------
# Core LLM call with exponential-backoff retry
# ---------------------------------------------------------------------------

def _call_vision_api(
    client: OpenAI,
    ref_b64: str,
    listing_b64: str,
    jp_name: str = "",
    product_type: str = "",
    title: str = "",
) -> dict:
    """
    Submit ref + listing images (plus optional text context) to the Vision API.
    jp_name:      Japanese product name of the reference SKU (from config_meta.csv).
    product_type: Product category of the reference SKU, bilingual (from config_meta.csv).
    title:        Mercari listing title written by the seller (from master_raw_data.csv).
    Returns a parsed dict with keys: is_pure, reason.
    Returns {"is_pure": None, "reason": "api_error"} on failure.
    Quantity is NOT determined here — use _count_items_api() for pure listings.
    """
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
                            {
                                "type": "image_url",
                                "image_url": {"url": ref_b64, "detail": "low"},
                            },
                            {
                                # "auto" is sufficient for orig full-resolution images (typically
                                # 1080×1080 or larger); the model will apply high-detail tiling
                                # automatically. "high" was only needed to force full processing
                                # on 240×240 CDN thumbnails that "auto" would downgrade to "low".
                                "type": "image_url",
                                "image_url": {"url": listing_b64, "detail": "auto"},
                            },
                        ],
                    },
                ],
                max_tokens=150,
                temperature=0,  # deterministic output for classification tasks
            )
            raw_json = response.choices[0].message.content.strip()
            parsed   = json.loads(raw_json)

            # Validate expected keys are present.
            if not {"is_pure", "reason"}.issubset(parsed.keys()):
                raise ValueError(f"Missing keys in LLM response: {parsed}")

            return parsed

        except (RateLimitError, APITimeoutError) as exc:
            wait = RETRY_DELAY_BASE ** attempt
            print(
                f"    [RETRY {attempt}/{MAX_RETRIES}] Rate-limit/timeout: {exc}. "
                f"Waiting {wait:.1f}s…",
                file=sys.stderr,
            )
            time.sleep(wait)

        except APIError as exc:
            print(f"    [ERROR] OpenAI API error: {exc}", file=sys.stderr)
            return error_sentinel

        except (json.JSONDecodeError, ValueError) as exc:
            print(f"    [ERROR] JSON parse failure: {exc}", file=sys.stderr)
            return error_sentinel

    # Exhausted all retries.
    print(f"    [ERROR] Max retries reached for this row.", file=sys.stderr)
    return error_sentinel


# ---------------------------------------------------------------------------
# Second-pass quantity counter (listing image only — no reference)
# ---------------------------------------------------------------------------

def _count_items_api(
    client: OpenAI,
    listing_b64: str,
    product_type: str = "",
) -> int:
    """
    Count the number of complete, individually sellable units in the listing image.
    Intentionally receives NO reference image so that cross-image visual confusion
    (which caused gpt-4o-mini to return qty=2 when Image A ≈ Image B) cannot occur.
    Returns 1 on any API error or parse failure (conservative fallback).
    """
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
                                "text": (
                                    f"Product type: {product_type}\n" if product_type else ""
                                ) + "Count the individually sellable items of this type in the image.",
                            },
                            {
                                "type": "image_url",
                                "image_url": {"url": listing_b64, "detail": "auto"},
                            },
                        ],
                    },
                ],
                max_tokens=50,
                temperature=0,
            )
            raw  = response.choices[0].message.content.strip()
            qty  = int(json.loads(raw).get("quantity", 1))
            return max(1, qty)

        except (RateLimitError, APITimeoutError) as exc:
            wait = RETRY_DELAY_BASE ** attempt
            print(
                f"    [RETRY {attempt}/{MAX_RETRIES}] Count rate-limit/timeout: {exc}. "
                f"Waiting {wait:.1f}s…",
                file=sys.stderr,
            )
            time.sleep(wait)

        except APIError as exc:
            print(f"    [ERROR] Count API error: {exc}", file=sys.stderr)
            return 1

        except (json.JSONDecodeError, ValueError, KeyError) as exc:
            print(f"    [ERROR] Count parse failure: {exc}", file=sys.stderr)
            return 1

    print(f"    [ERROR] Count API max retries reached.", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# IQR outlier removal
# ---------------------------------------------------------------------------

def _apply_iqr_filter(df: pd.DataFrame, col: str = "true_unit_price") -> pd.DataFrame:
    """
    Remove per-asset outliers using the Tukey fence (1.5 × IQR).
    Grouped by asset_id to respect heterogeneous price scales across assets.
    """
    # Build the boolean mask per-asset via explicit loop to avoid pandas 2.x
    # groupby/apply index-promotion issues that can desync mask and DataFrame index.
    keep_indices: list[pd.Index] = []
    for _, group in df.groupby("asset_id"):
        q1, q3 = group[col].quantile([0.25, 0.75])
        iqr    = q3 - q1
        mask   = (group[col] >= q1 - 1.5 * iqr) & (group[col] <= q3 + 1.5 * iqr)
        keep_indices.append(group.index[mask])

    all_keep = keep_indices[0].append(keep_indices[1:]) if keep_indices else pd.Index([])
    return df.loc[all_keep].reset_index(drop=True)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def clean() -> None:
    """Orchestrates the full LLM-cleaning and IQR-filtering pipeline."""

    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("[ERROR] OPENAI_API_KEY not found in environment / .env file.", file=sys.stderr)
        sys.exit(1)

    client = OpenAI(api_key=api_key)

    # --- Load input ---
    df = pd.read_csv(INPUT_CSV, parse_dates=["transaction_date", "T1", "T2"])
    print(f"Loaded {len(df)} rows from {INPUT_CSV.name}")

    if TEST_MODE:
        print(f"\n{'='*60}")
        print(f"  TEST_MODE = True  |  Sampling {TEST_SAMPLE_PER_ASSET} rows per asset")
        print(f"{'='*60}\n")
        # Explicit loop avoids pandas 2.x groupby/apply index-promotion behaviour.
        sampled_frames = [
            group.sample(n=min(TEST_SAMPLE_PER_ASSET, len(group)), random_state=TEST_RANDOM_SEED)
            for _, group in df.groupby("asset_id")
        ]
        df = pd.concat(sampled_frames, ignore_index=True)
        print(f"Test sample: {len(df)} rows across {df['asset_id'].nunique()} assets\n")

    # --- Load per-asset text context from config_meta.csv ---
    config_meta = pd.read_csv(PROJECT_ROOT / "config_meta.csv")
    jp_name_map: dict[str, str] = dict(
        zip(config_meta["folder_name"], config_meta["jp_name"])
    )
    product_type_map: dict[str, str] = dict(
        zip(config_meta["folder_name"], config_meta["product_type"])
    )

    # Pre-load reference images per asset (avoids redundant disk reads in the loop).
    ref_cache: dict[str, str | None] = {}
    for asset_id in df["asset_id"].unique():
        ref_path = _find_ref_image(DATA_RAW / asset_id)
        encoded  = _encode_local_image(ref_path) if ref_path else None
        if encoded is None:
            print(
                f"  [WARN] No ref image (ref.png/jpg/jpeg) found for {asset_id}. "
                f"Rows for this asset will be marked as API error.",
                file=sys.stderr,
            )
        else:
            print(f"  [REF] {asset_id} → {ref_path.name}")
        ref_cache[asset_id] = encoded

    # --- Row-level LLM loop ---
    results: list[dict] = []
    total = len(df)

    print(f"{'─'*60}")
    print(f"{'#':>5}  {'item_id':<16}  {'asset_id':<30}  {'is_pure':<8}  {'qty':>4}  reason")
    print(f"{'─'*60}")

    n_title_skipped = 0

    for idx, row in df.iterrows():
        asset_id  = row["asset_id"]
        item_id   = row["item_id"]
        image_url = row["image_url"]
        title     = str(row.get("title", ""))

        # --- Title pre-filter: skip LLM for explicit mixed-bundle listings ---
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
                # Second-pass quantity count: only Image B, no reference.
                # Eliminates cross-image confusion that caused systematic qty=2 errors.
                if result.get("is_pure") is True:
                    result["quantity"] = _count_items_api(
                        client,
                        listing_b64,
                        product_type=product_type_map.get(asset_id, ""),
                    )
                else:
                    result["quantity"] = 0  # impure or api_error → quantity irrelevant

        results.append({
            **row.to_dict(),
            "is_pure":      result.get("is_pure"),
            "llm_quantity": result.get("quantity"),
            "llm_reason":   result.get("reason"),
        })

        # Per-row console log for real-time monitoring.
        row_num  = len(results)
        is_pure  = result.get("is_pure")
        qty      = result.get("quantity", "—")
        reason   = (result.get("reason") or "")[:55]
        pure_str = ("✓ pure  " if is_pure else ("✗ impure" if is_pure is False else "? error "))
        tag      = "[T]" if reason == "title_keyword_filter" else "   "
        print(f"{row_num:>5}  {item_id:<16}  {asset_id:<30}  {pure_str}  {str(qty):>4}  {tag} {reason}")
        time.sleep(SLEEP_BETWEEN_ROWS)

    print(f"{'─'*60}\n")

    # --- Build results DataFrame ---
    result_df = pd.DataFrame(results)

    # Rows where LLM returned None (API error / missing image) — excluded downstream.
    n_api_errors = result_df["is_pure"].isna().sum()
    result_df = result_df[result_df["is_pure"].notna()].copy()

    # --- Drop impure listings ---
    n_impure = (result_df["is_pure"] == False).sum()  # noqa: E712 — robust against object dtype
    result_df = result_df[result_df["is_pure"]].copy()

    # Guard: quantity must be a positive integer to avoid division errors.
    result_df["llm_quantity"] = pd.to_numeric(result_df["llm_quantity"], errors="coerce")
    n_bad_qty = (result_df["llm_quantity"].isna() | (result_df["llm_quantity"] < 1)).sum()
    result_df = result_df[result_df["llm_quantity"] >= 1].copy()

    # --- Derive per-unit price ---
    result_df["true_unit_price"] = (
        result_df["price_raw"] / result_df["llm_quantity"]
    ).round(2)

    pre_iqr_count = len(result_df)

    # --- IQR outlier removal ---
    # Only meaningful with sufficient sample size; skip assets with < 4 records.
    eligible = result_df.groupby("asset_id").filter(lambda g: len(g) >= 4)
    skipped  = result_df[~result_df.index.isin(eligible.index)]

    if not eligible.empty:
        eligible = _apply_iqr_filter(eligible)

    result_df = pd.concat([eligible, skipped], ignore_index=True)
    n_iqr_removed = pre_iqr_count - len(result_df)

    # --- Finalise output columns ---
    output_cols = [
        "item_id", "asset_id", "asset_en_name",
        "price_raw", "llm_quantity", "true_unit_price",
        "transaction_date", "image_url",
        "llm_reason", "T1", "T2",
    ]
    result_df = result_df[output_cols]
    result_df.to_csv(OUTPUT_CSV, index=False, encoding="utf-8-sig")

    # --- Summary ---
    print(f"{'='*60}")
    print("CLEANING SUMMARY")
    print(f"{'='*60}")
    print(f"  Input rows              : {total:>6}")
    print(f"  Hard-filtered (no LLM)  : {n_title_skipped:>6}  (unambiguous mixed-bundle keywords in title)")
    print(f"  API errors / no image   : {n_api_errors:>6}")
    print(f"  Impure / mixed listings : {n_impure:>6}")
    print(f"  Invalid quantity (LLM)  : {n_bad_qty:>6}")
    print(f"  IQR outliers removed    : {n_iqr_removed:>6}")
    print(f"  Final clean rows        : {len(result_df):>6}")
    print(f"  Output → {OUTPUT_CSV}")
    print(f"{'='*60}")

    if TEST_MODE:
        print("\n⚠  TEST_MODE is ON — set TEST_MODE = False for full production run.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    clean()
