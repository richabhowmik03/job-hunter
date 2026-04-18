"""Scrape LinkedIn guest job listings into JSON for ``LINKEDIN_INGEST_PATH`` (e.g. CI)."""

from __future__ import annotations

import sys
from pathlib import Path

if __name__ == "__main__" and __package__ is None:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    __package__ = "job_hunter"

import argparse
import logging

from .core.profile_loader import load_config, load_profile
from .core.raw_job_io import save_raw_jobs
from .main import _load_env
from .sources.linkedin import fetch_guest_for_scrape

logger = logging.getLogger(__name__)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch LinkedIn jobs via the public guest API with pagination, "
            "and write a JSON ingest file for LINKEDIN_INGEST_PATH."
        )
    )
    parser.add_argument(
        "--root",
        default=".",
        help="Repository root (contains config.yaml and profiles/).",
    )
    parser.add_argument(
        "--out",
        default="data/linkedin_ingest.json",
        help="Output JSON path (array of job objects).",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Skip per-job description fetches (smaller, faster scrape).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
    )
    args = parser.parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING)

    root = Path(args.root).resolve()
    _load_env(root)

    cfg = load_config(root / "config.yaml")
    profile = load_profile(root / "profiles", cfg["profile"])

    logger.info(
        "Scraping LinkedIn guest (fast=%s) for profile %r…",
        args.fast,
        cfg["profile"],
    )
    jobs = fetch_guest_for_scrape(profile, fetch_descriptions=not args.fast)
    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = (root / out_path).resolve()
    save_raw_jobs(out_path, jobs)
    logger.info("Wrote %d job(s) to %s", len(jobs), out_path)
    print(f"linkedin_scrape: {len(jobs)} job(s) → {out_path}")


if __name__ == "__main__":
    main()
