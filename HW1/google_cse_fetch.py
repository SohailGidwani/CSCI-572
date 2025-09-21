#!/usr/bin/env python3
# google_cse_fetch.py
# Fetch top-10 Google results per query via Custom Search JSON API (CSE), fast & resumable.
#
# Output JSON format matches the course's Google reference:
# {
#   "query1": ["url1", ..., "url10"],
#   "query2": [...],
#   ...
# }
#
# Usage:
#   python google_cse_fetch.py \
#     --queries 100QueriesSet1.txt \
#     --out Google_Result1_live.json \
#     --api-key $GOOGLE_API_KEY \
#     --cx $GOOGLE_CX \
#     --parallel 5 --shuffle
#
# Then compare with your DDG file using your existing compare script.

import os, sys, json, time, math, random, tempfile, argparse, asyncio
from typing import Dict, List, Tuple, Optional
from urllib.parse import urlparse
import httpx

# ---------------- URL equivalence for de-dupe/matching (same rules you used before) ----------------

def equivalence_key(url: str) -> Tuple[str, str, str, str, str]:
    try:
        p = urlparse(url)
        host = p.netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        path = p.path[:-1] if p.path.endswith("/") and len(p.path) > 1 else p.path
        return (host, path, p.params, p.query, p.fragment)
    except Exception:
        return ("", url, "", "", "")

# ---------------- atomic JSON write (safe checkpoints after every query) ----------------

def write_json_atomic(path: str, obj) -> None:
    dir_ = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp = tempfile.mkstemp(prefix=".tmp_google_", suffix=".json", dir=dir_)
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

# ---------------- Google CSE client ----------------

class GoogleCSE:
    def __init__(self, api_key: str, cx: str, hl: str = "en", gl: str = "us", safe: str = "off", timeout: float = 20.0):
        self.api_key = api_key
        self.cx = cx
        self.hl = hl
        self.gl = gl
        self.safe = safe  # "off" | "active" | "high" (API supports "off"/"active")
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0))

    async def top10(self, query: str) -> List[str]:
        """
        One API call returns up to 10 results. We dedupe using equivalence_key, preserving order.
        """
        params = {
            "key": self.api_key,
            "cx": self.cx,
            "q": query,
            "num": 10,          # max allowed by API per request
            "hl": self.hl,
            "gl": self.gl,
            "safe": self.safe,  # "off" or "active"
        }
        r = await self.client.get("https://www.googleapis.com/customsearch/v1", params=params)
        # caller handles retries/backoff; return empty on non-200 to keep control flow simple
        if r.status_code != 200:
            return []
        data = r.json()
        items = data.get("items", []) or []
        seen, results = set(), []
        for it in items:
            link = it.get("link")
            if not link:
                continue
            k = equivalence_key(link)
            if k in seen:
                continue
            seen.add(k)
            results.append(link)
            if len(results) == 10:
                break
        return results

    async def aclose(self):
        await self.client.aclose()

# ---------------- worker with retries/backoff ----------------

async def fetch_with_retries(cse: GoogleCSE, query: str, max_retries: int = 3, base_wait: float = 1.5) -> List[str]:
    """
    Retry on empty (e.g., transient errors / quota hiccups). Exponential backoff with jitter.
    """
    attempt = 0
    while True:
        urls = await cse.top10(query)
        if urls or attempt >= max_retries:
            return urls
        # backoff: 1.5^attempt seconds, capped lightly
        wait = min(30.0, (base_wait ** attempt)) + random.uniform(0.0, 0.5)
        await asyncio.sleep(wait)
        attempt += 1

# ---------------- main pipeline ----------------

async def run(queries_path: str,
              out_path: str,
              api_key: str,
              cx: str,
              parallel: int = 5,
              shuffle: bool = False,
              limit: Optional[int] = None,
              hl: str = "en",
              gl: str = "us",
              safe: str = "off",
              max_retries: int = 3) -> None:

    # load queries
    with open(queries_path, "r", encoding="utf-8") as f:
        queries = [q.strip() for q in f if q.strip()]

    if limit is not None:
        queries = queries[:max(0, int(limit))]

    # load/initialize results (resume)
    results: Dict[str, List[str]] = {}
    if os.path.exists(out_path):
        try:
            with open(out_path, "r", encoding="utf-8") as f:
                results = json.load(f)
            print(f"[resume] Loaded {len(results)} existing queries from {out_path}")
        except Exception:
            print(f"[resume] Could not read {out_path}; starting fresh.")
            results = {}

    # filter pending
    pending = [q for q in queries if q not in results]
    if shuffle:
        random.shuffle(pending)

    total = len(queries)
    print(f"[start] total={total}, pending={len(pending)}, parallel={parallel}")

    cse = GoogleCSE(api_key=api_key, cx=cx, hl=hl, gl=gl, safe=safe)

    lock = asyncio.Lock()                       # protect results + file writes
    sem = asyncio.Semaphore(max(1, parallel))   # bound concurrency

    async def worker(q: str, idx: int):
        async with sem:
            urls = await fetch_with_retries(cse, q, max_retries=max_retries)
            async with lock:
                results[q] = urls
                write_json_atomic(out_path, results)
                print(f"[save] {idx+1}/{total} — {q[:60]}… → {len(urls)} urls")

    try:
        tasks = [asyncio.create_task(worker(q, idx)) for idx, q in enumerate(pending, start=len(results))]
        # Keep some human-ish pacing without killing speed:
        # slight stagger to avoid bursty QPS
        for t in tasks:
            await asyncio.sleep(random.uniform(0.05, 0.2))
        await asyncio.gather(*tasks)
    finally:
        await cse.aclose()

    print(f"[done] wrote {out_path} with {len(results)} queries")

# ---------------- CLI ----------------

def main():
    ap = argparse.ArgumentParser(description="Fetch Google top-10 results via Custom Search JSON API.")
    ap.add_argument("--queries", default="100QueriesSet1.txt", help="Path to queries file")
    ap.add_argument("--out", default="Google_Result1_live.json", help="Output JSON path (checkpoints)")
    ap.add_argument("--api-key", default=os.getenv("GOOGLE_API_KEY"), help="Google API key (env GOOGLE_API_KEY)")
    ap.add_argument("--cx", default=os.getenv("GOOGLE_CX"), help="Programmable Search Engine CX (env GOOGLE_CX)")
    ap.add_argument("--parallel", type=int, default=5, help="Max concurrent API calls")
    ap.add_argument("--shuffle", action="store_true", help="Randomize query order")
    ap.add_argument("--limit", type=int, default=None, help="Limit number of queries (testing)")
    ap.add_argument("--hl", default="en", help="Interface language (hl)")
    ap.add_argument("--gl", default="us", help="Geographic country/region (gl)")
    ap.add_argument("--safe", default="off", choices=["off", "active"], help="Safe search")
    ap.add_argument("--max-retries", type=int, default=3, help="Retries per query on empty response")
    args = ap.parse_args()

    if not args.api_key or not args.cx:
        print("ERROR: Missing API key or CX. Provide --api-key and --cx or set env GOOGLE_API_KEY/GOOGLE_CX.", file=sys.stderr)
        sys.exit(1)

    asyncio.run(run(
        queries_path=args.queries,
        out_path=args.out,
        api_key=args.api_key,
        cx=args.cx,
        parallel=args.parallel,
        shuffle=args.shuffle,
        limit=args.limit,
        hl=args.hl,
        gl=args.gl,
        safe=args.safe,
        max_retries=args.max_retries,
    ))

if __name__ == "__main__":
    main()