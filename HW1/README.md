# CSCI 572 HW1 Toolkit

This repository contains the tooling I built to tackle the DuckDuckGo vs. Google comparison assignment. It includes:

- `csci572_hw1.py`: end-to-end CLI for scraping DuckDuckGo, comparing against one or more Google JSON baselines, and exporting reports.
- `google_cse_fetch.py`: async helper that calls the Google Custom Search JSON API to produce fresh Google result files using the same URL-normalization rules as the homework.
- Generated artefacts such as `hw1.json`, `hw1_combined.csv`, and `hw1_combined.txt`, which document the current comparison results.

Everything below is written from my perspective as the student who implemented and ran these scripts.

## Environment & Dependencies

The code targets Python 3.9+ and relies on a few third-party packages:

- `httpx` (sync + async HTTP client)
- `lxml` (HTML parsing)

Install them first:

```bash
python3 -m pip install httpx lxml
```

The scripts only use the public internet to reach DuckDuckGo and, optionally, the Google Custom Search JSON API.

## DuckDuckGo Scraper (`csci572_hw1.py scrape`)

The scraper respects the assignment’s politeness rules while still being resilient:

- Random delay between each query (`--min-delay` / `--max-delay`) plus a one-second pause between paginated requests.
- Header rotation and a warm-up request to reduce the chance of 403 blocks.
- Support for multiple DuckDuckGo HTML front-ends and a `/lite` fallback.
- URL normalization (`http` vs `https`, `www.` removal, trailing slash handling) to avoid counting duplicates.

Example usage (produces `hw1.json` with DuckDuckGo’s top-10 URLs per query):

```bash
python3 csci572_hw1.py scrape \
  --queries 100QueriesSet4.txt \
  --out hw1.json \
  --min-delay 10 --max-delay 100
```

Add `--limit 5` and `--no-delay` only for quick local testing.

## Comparison Workflows

### Single Baseline (`compare`)

Compare the DuckDuckGo scrape against one Google JSON file and export CSV/TXT summaries:

```bash
python3 csci572_hw1.py compare \
  --engine hw1.json \
  --google Google_Result4.json \
  --csv hw1_vs_given.csv \
  --txt hw1_vs_given.txt
```

For each query the CSV records the overlap percentage (out of Google’s top-10) and the Spearman rho computed on the overlapping URLs. The TXT report stores the averages.

### Multiple Baselines (`compare-multi`)

Run the single-baseline comparison against several Google JSONs in one command. Per-baseline CSV/TXT files are emitted with a consistent naming pattern:

```bash
python3 csci572_hw1.py compare-multi \
  --engine hw1.json \
  --googles Google_Result4.json Google_Result1_live.json \
  --out-prefix hw1
```

### Combined Report (`compare-combined`)

Generate a single CSV/TXT that lines up the DuckDuckGo scrape against many Google JSONs at once. I used this to compare my scrape with both the provided homework baseline and the live Google API snapshot:

```bash
python3 csci572_hw1.py compare-combined \
  --engine hw1.json \
  --googles Google_Result4.json Google_Result1_live.json \
  --labels "given" "live" \
  --out-csv hw1_combined.csv \
  --out-txt hw1_combined.txt
```

The resulting CSV contains columns for each baseline (`Overlap%` + `SpearmanCoefficient`) plus an `Averages` row. The TXT file summarizes the average overlap and rho per baseline and reminds the reader about the interpretation of rho values in this homework variant.

## Google Custom Search Helper (`google_cse_fetch.py`)

This script lets me refresh the Google baseline using my Programmable Search Engine credentials while keeping everything resume-friendly and API quota conscious:

```bash
python3 google_cse_fetch.py \
  --queries 100QueriesSet4.txt \
  --out Google_Result1_live.json \
  --api-key "$GOOGLE_API_KEY" \
  --cx "$GOOGLE_CX" \
  --parallel 5 --shuffle
```

Key features:

- Async concurrency with a semaphore to respect QPS limits.
- Automatic checkpointing after every query (atomic JSON writes).
- Retry/backoff loop when the API returns empty results.
- Same URL equivalence rules as the DuckDuckGo pipeline so downstream comparisons stay consistent.

## Current Outputs

The repository includes the latest combined comparison artefacts, re-generated with the commands above:

- `hw1_combined.csv`: `Query` rows with overlap/rho columns for `given` and `live` Google datasets, plus an `Averages` summary.
- `hw1_combined.txt`: Textual summary highlighting average overlap (13.4% for `given`, 14.9% for `live`) and average Spearman coefficients (-3.9935 and -7.2355 respectively).

When re-running the pipeline, always keep the DuckDuckGo delays intact, and remember that the Google API requires valid credentials and may incur quota costs.

## Troubleshooting Notes

- If DuckDuckGo starts returning 403s, re-run with a higher `--min-delay`/`--max-delay` or regenerate cookies by restarting the scraper (the warm-up request usually fixes it).
- Missing packages (`httpx`, `lxml`) will surface as `ModuleNotFoundError`; reinstall via `pip` and retry.
- The combined comparison assumes the DuckDuckGo JSON and every Google JSON share the same ordered query set. Mismatches will show up as blank cells in the CSV.

Feel free to adapt the scripts for other query sets—just point `--queries` and the input JSONs at the new files.

## Conclusion

The homework asked me to quantify how closely DuckDuckGo’s top-ten links align with Google’s. After running the full pipeline, the combined reports (`hw1_combined.csv` and `hw1_combined.txt`) show that the overlap remains modest—about 13.4 % against the provided Google snapshot and 14.9 % against the live API pull. The negative Spearman coefficients (−3.99 and −7.24) confirm that even when the same URLs appear, their relative order differs quite a bit. In practice this means users are likely to see noticeably different ranking experiences between the two engines, which matches the expectation that each service optimizes with its own corpus and ranking signals. These metrics wrap up the assignment by demonstrating both the methodology (scraping, normalization, comparison) and the ultimate takeaway about cross-engine divergence.
