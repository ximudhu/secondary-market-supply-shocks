# Secondary Market Supply Shocks and Scarcity-Premium Erosion

Evidence from a Closed C2C Marketplace for Limited-Edition IP-Based Collectibles

---

## Overview

This project empirically investigates how official manufacturer re-release events erode scarcity premiums in closed consumer-to-consumer secondary markets. When a previously scarce limited-edition collectible is officially restocked, secondary-market prices—which had been sustained by the assumption of permanent scarcity—are exposed to a credible supply expansion. The central questions are: how large is the price decline, how quickly does it occur, and is there evidence that markets begin adjusting before physical stock becomes available?

The empirical setting is a major closed C2C marketplace for Japanese IP-based collectibles (anime and gaming merchandise). Transaction-level data cover six heterogeneous asset types—action figures, acrylic stands, plush mascots, holographic badges, classic badges, and lottery plush toys—spanning re-release events from late 2025 through mid-2026.

## Research Design

The study adopts a dual-anchor event study framework, distinguishing between two event dates for each asset: T1 (the public announcement of the re-release) and T2 (the date of physical availability). All price observations are expressed on a relative time axis with T2 as Day 0. This design identifies three event phases: a pre-announcement baseline (Phase 1), an anticipation window between announcement and release (Phase 2), and a post-availability period (Phase 3). By separating T1 from T2, the framework can decompose the total price impact into an anticipation component and a realisation component—a decomposition that single-anchor designs cannot achieve.

Prices are normalised to each asset's pre-T2 baseline mean, so that a value of 1.0 represents the pre-shock scarcity premium level and values below 1.0 represent erosion.

## Data Pipeline and LLM Validation

A significant methodological challenge in C2C marketplace data is bundle-sale noise: listings that combine multiple units or unrelated items at a single aggregate price, which systematically distort per-unit price estimates. Text-based heuristics are insufficient to identify these listings reliably; visual inspection of product photographs is required.

This project introduces a two-pass multimodal LLM validation pipeline using GPT-4o-mini. The pipeline separates purity classification (does this listing match the target SKU?) from quantity counting (how many units are visible?), processing each as an independent API call. This architectural choice resolves a systematic failure mode in single-pass approaches: at temperature=0, presenting a reference image alongside a listing photograph causes the model to deterministically overcount items due to cross-image visual confusion. Separating the two tasks eliminates this artifact entirely. The pipeline also incorporates full-resolution product images (1080x1080px) and structured text context anchors (product name, product type, listing title) to improve classification accuracy.

Of 954 raw transactions, 478 were classified as impure bundles and excluded; after IQR-based outlier treatment and a supplementary pre-event recovery pass, 439 clean transactions were retained for analysis.

## Key Findings

Five of six assets exhibit below-baseline normalised prices in the post-release period, with average Phase 3 erosion ranging from 6% to 42%.

The most statistically reliable case is Asset C (plush mascot), which has a simultaneous announcement and availability date, providing a clean pre-shock baseline. Prices were approximately 42% below baseline within 48 hours of the release date and remained stable at that lower level across 68 subsequent observation days and 207 transactions. Part of the decline began a few days before the official date, consistent with a pre-announcement leak.

For assets with a positive announcement-to-availability lead time, Phase 2 prices are systematically below Phase 1 levels before any physical stock becomes available, consistent with anticipatory price adjustment at the moment of the T1 announcement. The contrast between the instantaneous-shock case (largest Phase 3 erosion) and pre-announced cases (partial pre-release adjustment absorbed into the baseline) is qualitatively consistent with a two-stage efficient price discovery model.

Asset F (lottery plush) is the exception: its Phase 3 mean is modestly above baseline. The lottery distribution mechanism creates a bounded supply expansion that is insufficient to erode the scarcity premium; in addition, the 294-day announcement window had already caused substantial price adjustment before T2. This case illustrates that the mechanism and scale of supply expansion—not merely its announcement—are the operative variables governing secondary-market price impact.

Selected price trajectory figures are shown below.

![All-asset price trajectories](report/figures/fig_01_all_assets_grid.png)

![Cross-asset comparison aligned to T2](report/figures/fig_02_combined_overlay.png)

## Repository Structure

```
scripts/
    01_data_ingestion.py        raw CSV consolidation, field extraction, deduplication
    02_cleaning_and_llm.py      two-pass LLM purity classification and unit price recovery
    02b_iqr_recovery.py         supplementary LLM pass to reinstate pre-event IQR-excluded rows
    03_analysis.py              event-phase assignment, daily aggregation, price normalisation
    04_visualization.ipynb      figure generation (8 output PNG files)

report/
    Research_Report.md          full working paper (methodology, findings, conclusion)
    figures/                    price trajectory figures for all six assets

requirements.txt                Python dependencies
```

## Notes on Data Availability

Raw and cleaned transaction datasets are excluded from this repository. All assets are identified using anonymised labels (Asset A through Asset F) in all code, output files, and the report. The underlying Japanese IP identities are not disclosed.

## Requirements

Python 3.11. See `requirements.txt` for package dependencies. An OpenAI API key is required to run the LLM cleaning scripts (scripts 02 and 02b).
