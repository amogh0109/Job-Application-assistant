"""
CLI entrypoint for Apply Bot.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .config import load_profile, load_config
from .batch import BatchOrchestrator
from .logger import RunLogger
from .job_context import JobContextBuilder


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--profile", type=Path, required=True, help="Path to profile.json")
    p.add_argument("--config", type=Path, required=True, help="Path to config.yaml")
    p.add_argument("--jobs", type=Path, required=True, help="Path to job_links.txt")
    return p.parse_args()


async def amain():
    args = parse_args()
    profile = load_profile(args.profile)
    config = load_config(args.config)
    logger = RunLogger(config.log_dir)
    context_builder = JobContextBuilder()

    job_links = [line.strip() for line in args.jobs.read_text().splitlines() if line.strip()]
    orchestrator = BatchOrchestrator(profile, config, logger, context_builder)
    await orchestrator.run(job_links)


def main():
    # Check for Gemini SDK if likely needed
    try:
        import google.generativeai
    except ImportError:
        # Check if user seems to want Gemini
        args = parse_args()
        try:
             # loose check on config
             with open(args.config, "r") as f:
                 if "gemini_api_key" in f.read():
                     print("WARNING: 'google-generativeai' is not installed, but 'gemini_api_key' is present in config.")
                     print("         The bot will run in degraded mode (no smart planning).")
                     print("         Install with: pip install google-generativeai")
        except Exception:
            pass

    asyncio.run(amain())


if __name__ == "__main__":
    main()
