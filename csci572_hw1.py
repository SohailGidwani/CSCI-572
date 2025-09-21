#!/usr/bin/env python3
# csci572_hw1.py — resilient DDG scraper + comparator with incremental checkpoints
# Author: Sohail Gidwani (structure) — CSCI 572 HW1
#
#   Compare against multiple Google JSONs:
#     python csci572_hw1.py compare-multi --engine hw1.json --googles Google_Result4.json Google_Result1_live.json --out-prefix hw1
#   Single combined CSV/TXT for multiple Google JSONs:
#     python csci572_hw1.py compare-combined --engine hw1.json --googles Google_Result4.json Google_Result1_live.json --out-csv hw1_combined.csv --out-txt hw1_combined.txt
#   (Optional labels):
#     python csci572_hw1.py compare-combined --engine hw1.json --googles Google_Result4.json Google_Result1_live.json --labels "given" "live" --out-csv hw1_combined.csv --out-txt hw1_combined.txt

import argparse
import csv
import json
import os
import random
import tempfile
import time
from dataclasses import dataclass
from typing import Dict, List, Tuple, Optional
from urllib.parse import urlparse, parse_qs, unquote

import httpx
from lxml import html

# ============================== Config & Normalization ===============================

@dataclass
class ScrapeConfig:
    min_delay: float = 10.0
    max_delay: float = 100.0
    pages_per_query: int = 4
    http2: bool = False

class URLNormalizer:
    @staticmethod
    def key(url: str) -> Tuple[str, str, str, str, str]:
        try:
            p = urlparse(url)
            host = p.netloc.lower()
            if host.startswith("www."):
                host = host[4:]
            path = p.path[:-1] if p.path.endswith("/") and len(p.path) > 1 else p.path
            return (host, path, p.params, p.query, p.fragment)
        except Exception:
            return ("", url, "", "", "")

# =================================== Header Rotation =================================

UA_POOL = [
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
     "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) "
     "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15"),
]

def browsery_headers() -> Dict[str, str]:
    return {
        "User-Agent": random.choice(UA_POOL),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://duckduckgo.com/",
        "DNT": "1",
        "Upgrade-Insecure-Requests": "1",
        "Cache-Control": "no-cache",
    }

# ================================== Cookie Persistence ===============================

def save_cookies(client: httpx.Client, path: str) -> None:
    data = []
    for cookie in client.cookies.jar:
        data.append({
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path,
            "expires": cookie.expires,
            "secure": cookie.secure,
            "rest": cookie._rest if hasattr(cookie, "_rest") else {},
        })
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)

def load_cookies(client: httpx.Client, path: str) -> None:
    if not path or not os.path.exists(path):
        return
    try:
        from http.cookiejar import Cookie, CookieJar
        jar: CookieJar = client.cookies.jar
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for c in data:
            ck = Cookie(
                version=0,
                name=c["name"],
                value=c["value"],
                port=None,
                port_specified=False,
                domain=c["domain"],
                domain_specified=bool(c["domain"]),
                domain_initial_dot=c["domain"].startswith(".") if c["domain"] else False,
                path=c.get("path") or "/",
                path_specified=True,
                secure=bool(c.get("secure")),
                expires=c.get("expires"),
                discard=False,
                comment=None,
                comment_url=None,
                rest=c.get("rest") or {},
                rfc2109=False,
            )
            jar.set_cookie(ck)
    except Exception:
        pass

# ================================== Safe JSON Writer =================================

def write_json_atomic(path: str, obj) -> None:
    dir_ = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tmp_hw1_", suffix=".json", dir=dir_)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try: os.remove(tmp)
        except Exception: pass
        raise

# ================================= DuckDuckGo Scraper ================================

class DuckDuckGoScraper:
    BASES = [
        "https://html.duckduckgo.com/html/",
        "https://duckduckgo.com/html/",
    ]
    LITE = "https://lite.duckduckgo.com/lite/"

    def __init__(self, cfg: ScrapeConfig, debug_html: bool = False, cookies_path: Optional[str] = None):
        self.cfg = cfg
        self.debug_html = debug_html
        self.cookies_path = cookies_path
        self.client = httpx.Client(
            headers=browsery_headers(),
            timeout=httpx.Timeout(30.0, connect=15.0),
            follow_redirects=True,
            http2=cfg.http2,
        )
        # load cookies if present
        if cookies_path:
            load_cookies(self.client, cookies_path)
        # warm-up
        try:
            self.client.get("https://duckduckgo.com/", timeout=10.0)
        except Exception:
            pass
        if self.debug_html:
            os.makedirs("debug_pages", exist_ok=True)

    @staticmethod
    def _extract_target(href: str) -> str:
        if not href:
            return ""
        p = urlparse(href)
        if p.netloc.endswith("duckduckgo.com") and p.path.startswith("/l"):
            qs = parse_qs(p.query)
            if "uddg" in qs and qs["uddg"]:
                return unquote(qs["uddg"][0])
        return href

    def _fetch_page(self, base: str, query: str, offset: int = 0) -> httpx.Response:
        params = {"q": query, "kl": "us-en", "ia": "web"}
        if offset:
            params["s"] = str(offset)
        r = self.client.get(base, params=params)
        retries = 2
        while r.status_code == 403 and retries > 0:
            time.sleep(random.uniform(2.0, 5.0))
            self.client.headers.update(browsery_headers())
            r = self.client.get(base, params=params)
            retries -= 1
        return r

    def _fetch_page_lite(self, query: str) -> httpx.Response:
        return self.client.get(self.LITE, params={"q": query})

    @staticmethod
    def _parse_html_results(html_text: str) -> List[str]:
        doc = html.fromstring(html_text)
        return doc.xpath('//a[contains(@class, "result__a")]/@href')

    @staticmethod
    def _parse_lite_results(html_text: str) -> List[str]:
        doc = html.fromstring(html_text)
        hrefs = doc.xpath('//a[contains(@class, "result-link")]/@href')
        if not hrefs:
            hrefs = doc.xpath('//a[@href and (contains(@class,"result") or contains(@class,"link"))]/@href')
        return hrefs

    def _maybe_dump(self, label: str, html_text: str):
        if not self.debug_html:
            return
        ts = int(time.time() * 1000)
        fname = os.path.join("debug_pages", f"{label}_{ts}.html")
        try:
            with open(fname, "w", encoding="utf-8") as f:
                f.write(html_text)
            print(f"[debug] Saved HTML dump: {fname}")
        except Exception:
            pass

    def top10_once(self, query: str, idx_label: str = "") -> List[str]:
        collected, seen = [], set()
        offset = 0
        for page_idx in range(self.cfg.pages_per_query):
            fetched = False
            last_text = ""
            for base in self.BASES:
                try:
                    r = self._fetch_page(base, query, offset)
                    if r.status_code >= 400:
                        self._maybe_dump(f"err_{idx_label}_p{page_idx}_{r.status_code}", r.text)
                        continue
                    last_text = r.text
                    fetched = True
                    break
                except httpx.RequestError:
                    continue
            if not fetched:
                time.sleep(random.uniform(2.0, 4.0))
                break

            try:
                hrefs = self._parse_html_results(last_text)
            except Exception:
                hrefs = []

            if not hrefs:
                self._maybe_dump(f"empty_html_{idx_label}_p{page_idx}", last_text)
                time.sleep(random.uniform(8.0, 15.0))

            for href in hrefs:
                target = self._extract_target(href)
                key = URLNormalizer.key(target)
                if key in seen:
                    continue
                seen.add(key)
                collected.append(target)
                if len(collected) == 10:
                    return collected

            offset += 30
            if len(collected) < 10:
                time.sleep(1.0)

        if len(collected) < 10:
            try:
                r = self._fetch_page_lite(query)
                if r.status_code >= 400:
                    self._maybe_dump(f"err_lite_{idx_label}_{r.status_code}", r.text)
                else:
                    hrefs = self._parse_lite_results(r.text)
                    if not hrefs:
                        self._maybe_dump(f"empty_lite_{idx_label}", r.text)
                    for href in hrefs:
                        target = self._extract_target(href)
                        key = URLNormalizer.key(target)
                        if key in seen:
                            continue
                        seen.add(key)
                        collected.append(target)
                        if len(collected) == 10:
                            break
            except Exception:
                pass

        return collected

    def top10_with_retries(self, query: str, idx_label: str, max_retries: int, backoff_minutes: List[Tuple[float, float]]) -> List[str]:
        """
        Try multiple times with escalating backoff windows (in minutes).
        backoff_minutes: list of (min, max) minute windows per retry step.
        """
        attempt = 0
        urls: List[str] = []
        while attempt == 0 or (not urls and attempt <= max_retries):
            if attempt > 0:
                # escalate backoff
                min_m, max_m = backoff_minutes[min(attempt-1, len(backoff_minutes)-1)]
                wait_s = random.uniform(min_m*60, max_m*60)
                print(f"[backoff] 0 links → waiting {wait_s/60:.1f} min before retry {attempt}/{max_retries}")
                time.sleep(wait_s)
                # rotate headers & warm up
                self.client.headers.update(browsery_headers())
                try:
                    self.client.get("https://duckduckgo.com/", timeout=10.0)
                except Exception:
                    pass
            urls = self.top10_once(query, idx_label=f"{idx_label}_try{attempt}")
            attempt += 1

        # save cookies to persist session if path provided
        if self.cookies_path:
            try:
                save_cookies(self.client, self.cookies_path)
            except Exception:
                pass
        return urls

# ====================================== Metrics ======================================

def build_rank_map(urls: List[str]) -> Dict[Tuple[str, str, str, str, str], int]:
    ranks: Dict[Tuple[str, str, str, str, str], int] = {}
    for i, u in enumerate(urls[:10], start=1):
        ranks[URLNormalizer.key(u)] = i
    return ranks

def spearman_rho(google_ranks: Dict, other_ranks: Dict) -> float:
    keys = [k for k in google_ranks if k in other_ranks]
    n = len(keys)
    if n == 0: return 0.0
    if n == 1:
        k = keys[0]
        return 1.0 if google_ranks[k] == other_ranks[k] else 0.0
    ssd = sum((google_ranks[k] - other_ranks[k]) ** 2 for k in keys)
    return 1.0 - (6.0 * ssd) / (n * (n * n - 1))

# ================================== Writers (CSV/TXT) =================================

def write_csv(results: List[Tuple[str, float, float]], out_csv: str) -> Tuple[float, float]:
    overlaps = [r[1] for r in results]
    rhos = [r[2] for r in results]
    avg_overlap = sum(overlaps) / len(overlaps) if overlaps else 0.0
    avg_rho = sum(rhos) / len(rhos) if rhos else 0.0
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Query", "Overlap%", "SpearmanCoefficient"])
        for row in results:
            w.writerow([row[0], f"{row[1]}", f"{row[2]}"])
        w.writerow(["Averages", f"{avg_overlap}", f"{avg_rho}"])
    return avg_overlap, avg_rho

def write_txt(avg_overlap: float, avg_rho: float, out_txt: str) -> None:
    with open(out_txt, "w", encoding="utf-8") as f:
        f.write(
            "Based on 100 queries comparing DuckDuckGo to Google, the average percent overlap "
            f"was {avg_overlap} and the average Spearman coefficient was {avg_rho}. "
            "In this assignment’s variant, a higher positive rho indicates closer ranking behavior, "
            "while negative values indicate dissimilar ranking among overlapping results."
        )

# ===================================== Commands ======================================

def cmd_scrape(args) -> None:
    cfg = ScrapeConfig(
        min_delay=args.min_delay,
        max_delay=args.max_delay,
        pages_per_query=args.pages_per_query,
        http2=False if args.force_http1 else True if args.http2 else False,
    )
    scraper = DuckDuckGoScraper(cfg, debug_html=args.debug_html, cookies_path=args.cookies)

    with open(args.queries, "r", encoding="utf-8") as f:
        all_queries = [q.strip() for q in f if q.strip()]

    if args.limit is not None:
        all_queries = all_queries[:max(0, int(args.limit))]

    results: Dict[str, List[str]] = {}
    if os.path.exists(args.out):
        try:
            with open(args.out, "r", encoding="utf-8") as f:
                results = json.load(f)
            print(f"[resume] Loaded {len(results)} completed queries from {args.out}")
        except Exception:
            print(f"[resume] Could not read {args.out}; starting fresh.")
            results = {}

    pending = [q for q in all_queries if q not in results]
    if args.shuffle:
        random.shuffle(pending)

    total = len(all_queries)
    zero_streak = 0

    # backoff profile per retry: [(min_minutes, max_minutes), ...]
    retry_backoff = [(0.5, 1.5), (3.0, 5.0), (8.0, 12.0)]  # escalate waits

    for i, q in enumerate(pending, start=1):
        if not args.no_delay:
            delay = random.uniform(cfg.min_delay, cfg.max_delay)
            print(f"[delay] Sleeping {delay:.1f}s before query #{len(results)+1}/{total}")
            time.sleep(delay)

        urls = scraper.top10_with_retries(
            q,
            idx_label=f"q{len(results)+1}",
            max_retries=args.max_retries,
            backoff_minutes=retry_backoff,
        )

        if urls:
            zero_streak = 0
        else:
            zero_streak += 1

        results[q] = urls
        print(f"[save] {q[:60]}… → {len(urls)} urls (checkpoint)")
        write_json_atomic(args.out, results)

        # global cooldown if repeated empty pages (DDG is grumpy)
        if zero_streak >= args.streak_cooldown_after:
            mins = random.uniform(args.cooldown_min, args.cooldown_max)
            print(f"[cooldown] {zero_streak} zero-link queries in a row → pausing {mins:.1f} min…")
            time.sleep(mins * 60)
            zero_streak = 0
            # rotate headers and warm-up again
            scraper.client.headers.update(browsery_headers())
            try:
                scraper.client.get("https://duckduckgo.com/", timeout=10.0)
            except Exception:
                pass

    print(f"[done] Wrote {args.out} with {len(results)} queries.")


# ========================== Comparison Helper and Multi Compare =========================

def compare_and_write(engine_path: str, google_path: str, out_csv: str, out_txt: str) -> Tuple[float, float]:
    """
    Compare engine JSON to Google JSON, write CSV and TXT, print summary.
    Returns (avg_overlap, avg_rho).
    """
    with open(engine_path, "r", encoding="utf-8") as f:
        engine = json.load(f)
    with open(google_path, "r", encoding="utf-8") as f:
        google = json.load(f)

    queries = list(google.keys())
    rows: List[Tuple[str, float, float]] = []
    for idx, q in enumerate(queries, start=1):
        g_list = (google.get(q) or [])[:10]
        e_list = (engine.get(q) or [])[:10]
        g_rank = build_rank_map(g_list)
        e_rank = build_rank_map(e_list)
        overlap_count = sum(1 for k in g_rank if k in e_rank)
        overlap_pct = (overlap_count / 10.0) * 100.0
        rho = spearman_rho(g_rank, e_rank)
        rows.append((f"Query {idx}", overlap_pct, rho))

    avg_overlap, avg_rho = write_csv(rows, out_csv)
    write_txt(avg_overlap, avg_rho, out_txt)
    print(f"Wrote {out_csv} and {out_txt}")
    print(f"Average overlap: {avg_overlap:.2f}% | Average rho: {avg_rho:.4f}")
    return avg_overlap, avg_rho

def compare_combined_and_write(engine_path: str, google_paths: List[str], labels: Optional[List[str]], out_csv: str, out_txt: str) -> None:
    import os
    # Load engine
    with open(engine_path, "r", encoding="utf-8") as f:
        engine = json.load(f)

    # Derive labels from stems if not provided or length mismatch
    if not labels or len(labels) != len(google_paths):
        labels = [os.path.splitext(os.path.basename(p))[0] for p in google_paths]

    # Load all Google JSONs
    googles = []
    for p in google_paths:
        with open(p, "r", encoding="utf-8") as f:
            googles.append(json.load(f))

    # Master ordered list: engine's query keys
    engine_queries = list(engine.keys())

    # For each Google file, compute per-query metrics dictionary: q -> (overlap_pct, rho) or None
    per_label_results: List[Dict[str, Optional[Tuple[float, float]]]] = []
    for g in googles:
        results_for_g: Dict[str, Optional[Tuple[float, float]]] = {}
        for q in engine_queries:
            g_list = (g.get(q) or [])[:10]
            e_list = (engine.get(q) or [])[:10]
            if g_list:
                g_rank = build_rank_map(g_list)
                e_rank = build_rank_map(e_list)
                overlap_count = sum(1 for k in g_rank if k in e_rank)
                overlap_pct = (overlap_count / 10.0) * 100.0  # denominator is Google's 10
                rho = spearman_rho(g_rank, e_rank)
                results_for_g[q] = (overlap_pct, rho)
            else:
                results_for_g[q] = None
        per_label_results.append(results_for_g)

    # Write combined CSV
    headers = ["Query"]
    for lab in labels:
        headers.extend([f"{lab} Overlap%", f"{lab} SpearmanCoefficient"])

    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(headers)
        for idx, q in enumerate(engine_queries, start=1):
            row = [f"Query {idx}"]
            for results_for_g in per_label_results:
                val = results_for_g.get(q)
                if val is None:
                    row.extend(["", ""])
                else:
                    row.extend([f"{val[0]}", f"{val[1]}"])
            w.writerow(row)

        # Averages row per label
        avg_row = ["Averages"]
        for results_for_g in per_label_results:
            vals = [v for v in results_for_g.values() if v is not None]
            if vals:
                avg_overlap = sum(v[0] for v in vals) / len(vals)
                avg_rho = sum(v[1] for v in vals) / len(vals)
                avg_row.extend([f"{avg_overlap}", f"{avg_rho}"])
            else:
                avg_row.extend(["", ""])
        w.writerow(avg_row)

    # Write combined TXT summary
    with open(out_txt, "w", encoding="utf-8") as f:
        f.write("Combined comparison of DuckDuckGo vs multiple Google JSONs.\n")
        for lab, results_for_g in zip(labels, per_label_results):
            vals = [v for v in results_for_g.values() if v is not None]
            n = len(vals)
            avg_overlap = sum(v[0] for v in vals) / n if n else 0.0
            avg_rho = sum(v[1] for v in vals) / n if n else 0.0
            f.write(f"- {lab}: average percent overlap = {avg_overlap}, average Spearman coefficient = {avg_rho} (over {n} queries with data).\n")
        f.write("Notes: Overlap% uses 10 as the denominator (Google's top-10). Spearman is computed only on overlapping URLs; in this assignment variant, values may fall outside [-1,1]. Blank cells indicate the query was not present in that Google JSON.\n")

    print(f"Wrote {out_csv} and {out_txt}")

def cmd_compare(args) -> None:
    compare_and_write(args.engine, args.google, args.csv, args.txt)

def cmd_compare_combined(args) -> None:
    compare_combined_and_write(args.engine, args.googles, args.labels, args.out_csv, args.out_txt)

# Multi-compare: compare engine against multiple Google JSONs
def cmd_compare_multi(args) -> None:
    import os
    generated_files = []
    averages = []
    for g_path in args.googles:
        stem = os.path.splitext(os.path.basename(g_path))[0]
        out_csv = f"{args.out_prefix}__vs_{stem}.csv"
        out_txt = f"{args.out_prefix}__vs_{stem}.txt"
        avg_overlap, avg_rho = compare_and_write(args.engine, g_path, out_csv, out_txt)
        generated_files.extend([out_csv, out_txt])
        print(f"Compared against {g_path}: avg_overlap={avg_overlap:.2f}%, avg_rho={avg_rho:.4f}")
        averages.append((g_path, avg_overlap, avg_rho))
    print("\nSummary of all comparisons:")
    for g_path, avg_overlap, avg_rho in averages:
        print(f"  {g_path}: overlap={avg_overlap:.2f}%, rho={avg_rho:.4f}")
    print("\nAll generated files:")
    for f in generated_files:
        print(f"  {f}")

# ===================================== CLI Builder ===================================

def build_cli() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="CSCI 572 HW1: DuckDuckGo vs Google (Set 1) — scraper & comparator"
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    ps = sub.add_parser("scrape", help="Scrape DuckDuckGo and write hw1.json (incremental)")
    ps.add_argument("--queries", default="100QueriesSet4.txt", help="Path to queries file")
    ps.add_argument("--out", default="hw1.json", help="Output JSON path (checkpoints)")
    ps.add_argument("--min-delay", type=float, default=10.0, help="Min delay between queries (sec)")
    ps.add_argument("--max-delay", type=float, default=100.0, help="Max delay between queries (sec)")
    ps.add_argument("--pages-per-query", type=int, default=4, help="DDG pages to crawl per query")
    ps.add_argument("--no-delay", action="store_true", help="(Testing only) skip big sleep (risk blocks)")
    ps.add_argument("--limit", type=int, default=None, help="(Testing) limit number of queries")
    ps.add_argument("--debug-html", action="store_true", help="Dump 0-link/error pages to debug_pages/")
    ps.add_argument("--http2", action="store_true", help="Force HTTP/2 (default off)")
    ps.add_argument("--force-http1", action="store_true", help="Force HTTP/1.1 (default)")
    ps.add_argument("--shuffle", action="store_true", help="Randomize query order")
    ps.add_argument("--max-retries", type=int, default=3, help="Retries per query when 0 links")
    ps.add_argument("--streak-cooldown-after", type=int, default=3, help="Cooldown after this many 0-link queries in a row")
    ps.add_argument("--cooldown-min", type=float, default=5.0, help="Global cooldown min minutes")
    ps.add_argument("--cooldown-max", type=float, default=10.0, help="Global cooldown max minutes")
    ps.add_argument("--cookies", default=None, help="Path to persist cookies (e.g., cookies.json)")
    ps.set_defaults(func=cmd_scrape)

    pc = sub.add_parser("compare", help="Compare hw1.json with Google and write hw1.csv + hw1.txt")
    pc.add_argument("--engine", default="hw1.json", help="Your scraped JSON")
    pc.add_argument("--google", default="Google_Result4.json", help="Google reference JSON")
    pc.add_argument("--csv", default="hw1.csv", help="Output CSV path")
    pc.add_argument("--txt", default="hw1.txt", help="Output TXT summary path")
    pc.set_defaults(func=cmd_compare)

    # Add compare-multi subparser
    pm = sub.add_parser(
        "compare-multi",
        help="Compare engine against multiple Google JSONs; write one CSV/TXT per Google file"
    )
    pm.add_argument("--engine", default="hw1.json", help="Your scraped JSON")
    pm.add_argument("--googles", nargs="+", required=True, help="One or more Google JSONs (e.g., Google_Result4.json Google_Result1_live.json)")
    pm.add_argument("--out-prefix", default="hw1", help="Prefix for output files (creates <prefix>__vs_<google-stem>.csv/.txt)")
    pm.set_defaults(func=cmd_compare_multi)
    # Add compare-combined subparser
    pcmb = sub.add_parser(
        "compare-combined",
        help="Compare engine against multiple Google JSONs and write a single combined CSV/TXT"
    )
    pcmb.add_argument("--engine", default="hw1.json", help="Your scraped JSON")
    pcmb.add_argument("--googles", nargs="+", required=True, help="Two or more Google JSONs to compare")
    pcmb.add_argument("--labels", nargs="+", help="Optional labels corresponding to --googles (defaults to file stems)")
    pcmb.add_argument("--out-csv", default="hw1_combined.csv", help="Output combined CSV path")
    pcmb.add_argument("--out-txt", default="hw1_combined.txt", help="Output combined TXT path")
    pcmb.set_defaults(func=cmd_compare_combined)
    return p

def main():
    parser = build_cli()
    args = parser.parse_args()
    args.func(args)

if __name__ == "__main__":
    main()