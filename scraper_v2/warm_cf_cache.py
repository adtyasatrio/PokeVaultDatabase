#!/usr/bin/env python3
"""
PikaVault Cloudflare Cache Warmer
----------------------------------
Iterates through all card images in Supabase and requests them through
Cloudflare CDN (https://cdn.pikavault.site) to pre-warm the edge cache.

Usage:
    python3 scripts/warm_cf_cache.py
    python3 scripts/warm_cf_cache.py --workers 30
    python3 scripts/warm_cf_cache.py --limit 500
    python3 scripts/warm_cf_cache.py --type both
"""

import argparse
import concurrent.futures
import json
import os
import signal
import sys
import time
import urllib.error
import urllib.request
from typing import List, Set

SUPABASE_URL = "https://vpfjmgefygjhabuizdsq.supabase.co"
SUPABASE_ANON_KEY = "sb_publishable_Agz4sczyTCdGL_ziwndI1g_dPTwVZHO"
CDN_BASE = "https://cdn.pikavault.site"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 PikaVaultWarmer/1.0"
STATE_FILE = ".cache_warmed_state.txt"

# ANSI Colors
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
RED = "\033[91m"
BOLD = "\033[1m"
RESET = "\033[0m"


class CacheWarmer:
    def __init__(
        self,
        cdn_base: str,
        workers: int,
        limit: int,
        image_type: str,
        resume: bool,
    ):
        self.cdn_base = cdn_base.rstrip("/")
        self.workers = workers
        self.limit = limit
        self.image_type = image_type
        self.resume = resume
        self.stop_requested = False

        self.already_warmed: Set[str] = set()
        if self.resume and os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                self.already_warmed = {line.strip() for line in f if line.strip()}
            print(f"{CYAN}Loaded {len(self.already_warmed)} already warmed URLs from {STATE_FILE}{RESET}")

        self.state_file_handle = open(STATE_FILE, "a")

        self.total_processed = 0
        self.count_hit = 0
        self.count_miss = 0
        self.count_error = 0
        self.start_time = time.time()

    def close(self):
        if self.state_file_handle:
            self.state_file_handle.close()

    def fetch_image_paths(self) -> List[str]:
        """Fetch all card image paths from Supabase in batches."""
        print(f"\n{BOLD}Fetching card paths from Supabase...{RESET}")
        paths = []
        batch_size = 1000
        offset = 0

        # Choose columns based on image_type
        if self.image_type == "small":
            select_cols = "id,image_small_b2_path"
            filter_clause = "image_small_b2_path=not.is.null"
        elif self.image_type == "hires":
            select_cols = "id,image_large_b2_path"
            filter_clause = "image_large_b2_path=not.is.null"
        else:
            select_cols = "id,image_small_b2_path,image_large_b2_path"
            filter_clause = ""

        while True:
            url = f"{SUPABASE_URL}/rest/v1/pokemon_cards?select={select_cols}"
            if filter_clause:
                url += f"&{filter_clause}"

            req = urllib.request.Request(
                url,
                headers={
                    "apikey": SUPABASE_ANON_KEY,
                    "Range-Unit": "items",
                    "Range": f"{offset}-{offset + batch_size - 1}",
                },
            )

            try:
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                    if not data:
                        break

                    for card in data:
                        if self.image_type in ("small", "both"):
                            p = card.get("image_small_b2_path")
                            if p and p not in self.already_warmed:
                                paths.append(p)
                        if self.image_type in ("hires", "both"):
                            p = card.get("image_large_b2_path")
                            if p and p not in self.already_warmed:
                                paths.append(p)

                    print(f"  Loaded {len(paths)} pending paths (offset: {offset})...", end="\r")
                    offset += len(data)

                    if self.limit and len(paths) >= self.limit:
                        paths = paths[: self.limit]
                        break

                    if len(data) < batch_size:
                        break
            except Exception as e:
                print(f"\n{RED}Error fetching batch at offset {offset}: {e}{RESET}")
                break

        print(f"\n{GREEN}Total unique paths to warm: {len(paths)}{RESET}\n")
        return paths

    def warm_single_image(self, path: str):
        """Warm single image by requesting it through Cloudflare CDN."""
        if self.stop_requested:
            return

        clean_path = path.lstrip("/")
        url = f"{self.cdn_base}/{clean_path}"

        req = urllib.request.Request(
            url,
            headers={"User-Agent": USER_AGENT},
        )

        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                status = resp.headers.get("cf-cache-status", "UNKNOWN").upper()
                code = resp.getcode()

                if "HIT" in status:
                    self.count_hit += 1
                else:
                    self.count_miss += 1  # Cloudflare pulled it to edge cache!

                # Persist to state
                self.state_file_handle.write(f"{path}\n")
                self.state_file_handle.flush()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                self.count_error += 1
            else:
                self.count_error += 1
        except Exception:
            self.count_error += 1
        finally:
            self.total_processed += 1

    def run(self):
        paths = self.fetch_image_paths()
        if not paths:
            print("No images to warm. Everything is already cached or no records found.")
            return

        total = len(paths)
        print(f"{BOLD}Starting cache warming with {self.workers} concurrent workers...{RESET}")
        print(f"CDN Base: {CYAN}{self.cdn_base}{RESET}\n")

        self.start_time = time.time()

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.workers) as executor:
            futures = [executor.submit(self.warm_single_image, path) for path in paths]

            # Progress display loop
            while True:
                done = sum(1 for f in futures if f.done())
                elapsed = max(time.time() - self.start_time, 0.001)
                speed = done / elapsed
                pct = (done / total) * 100 if total > 0 else 100

                bar_len = 30
                filled = int(bar_len * done / total) if total > 0 else bar_len
                bar = "█" * filled + "░" * (bar_len - filled)

                sys.stdout.write(
                    f"\r[{bar}] {done}/{total} ({pct:.1f}%) | "
                    f"{GREEN}HIT: {self.count_hit}{RESET} | "
                    f"{YELLOW}CACHED(MISS): {self.count_miss}{RESET} | "
                    f"{RED}ERR: {self.count_error}{RESET} | "
                    f"{CYAN}{speed:.1f} req/s{RESET}"
                )
                sys.stdout.flush()

                if done >= total or self.stop_requested:
                    break
                time.sleep(0.2)

        print("\n\n" + "=" * 50)
        print(f"{BOLD}Cache Warming Finished!{RESET}")
        print(f"Total Processed   : {self.total_processed}")
        print(f"Already Cached    : {GREEN}{self.count_hit}{RESET}")
        print(f"Newly Cached      : {YELLOW}{self.count_miss}{RESET}")
        print(f"Failed / Missing  : {RED}{self.count_error}{RESET}")
        elapsed = time.time() - self.start_time
        print(f"Total Time        : {elapsed:.1f} seconds ({self.total_processed / max(elapsed, 1):.1f} req/s)")
        print("=" * 50 + "\n")


def main():
    parser = argparse.ArgumentParser(description="PikaVault Cloudflare Cache Warmer")
    parser.add_argument(
        "--workers",
        "-w",
        type=int,
        default=25,
        help="Number of concurrent worker threads (default: 25)",
    )
    parser.add_argument(
        "--limit",
        "-l",
        type=int,
        default=0,
        help="Limit number of images to warm (0 = all, default: 0)",
    )
    parser.add_argument(
        "--type",
        "-t",
        choices=["small", "hires", "both"],
        default="small",
        help="Type of images to warm (default: small)",
    )
    parser.add_argument(
        "--cdn-base",
        default=CDN_BASE,
        help=f"Base URL of the CDN (default: {CDN_BASE})",
    )
    parser.add_argument(
        "--no-resume",
        action="store_true",
        help="Do not resume from previous run state",
    )

    args = parser.parse_args()

    warmer = CacheWarmer(
        cdn_base=args.cdn_base,
        workers=args.workers,
        limit=args.limit,
        image_type=args.type,
        resume=not args.no_resume,
    )

    def handle_sigint(sig, frame):
        print(f"\n{YELLOW}Gracefully stopping workers...{RESET}")
        warmer.stop_requested = True
        warmer.close()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_sigint)

    try:
        warmer.run()
    finally:
        warmer.close()


if __name__ == "__main__":
    main()
