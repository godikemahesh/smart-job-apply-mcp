#!/usr/bin/env python3
"""
Standalone runner for job-apply-mcp.
No Claude needed — just run: python3 run.py
"""

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from tools.session import check_all_sessions, interactive_login, SUPPORTED_PLATFORMS
from tools.search import search_jobs, filter_jobs
from tools.apply import bulk_apply
from tools.tracker import get_application_summary


def show_menu():
    print()
    print("=" * 55)
    print("  Smart Job Apply MCP — Standalone Interactive Runner")
    print("=" * 55)
    print()
    print("  1. Check login status (all platforms)")
    print("  2. Login to a platform (save session)")
    print("  3. Search & Apply for jobs")
    print("  4. View application status history")
    print("  5. Search only (browse matches)")
    print("  6. Exit")
    print()
    return input("  Choose [1-6]: ").strip()


def get_platforms():
    print(f"\n  Available: {', '.join(SUPPORTED_PLATFORMS)}")
    raw = input("  Platforms (comma-separated, or 'naukri'): ").strip()
    if not raw:
        return ["naukri"]
    return [p.strip().lower() for p in raw.split(",")]


def get_keywords():
    print("\n  Default keywords: DevOps, MLOps, Cloud, SRE, Platform, K8s, CI/CD, GenAI, LLMOps")
    raw = input("  Custom keywords (or press Enter for defaults): ").strip()
    if not raw:
        return [
            "DevOps Engineer", "MLOps Engineer", "Cloud Engineer",
            "SRE Engineer", "Platform Engineer", "Kubernetes Engineer",
            "CI CD Engineer", "GenAI Engineer", "LLMOps Engineer",
        ]
    return [k.strip() for k in raw.split(",")]


async def do_login():
    print(f"\n  Available: {', '.join(SUPPORTED_PLATFORMS)}")
    platform = input("  Platform to login: ").strip().lower()
    if platform not in SUPPORTED_PLATFORMS:
        print(f"  Invalid platform. Choose from: {', '.join(SUPPORTED_PLATFORMS)}")
        return
    print(f"\n  Opening browser for {platform} login...")
    result = await interactive_login(platform)
    print(f"  Result: {json.dumps(result, indent=2)}")


async def do_search_and_apply():
    platforms = get_platforms()
    keywords = get_keywords()

    max_apps_raw = input("  Max applications (default 20): ").strip()
    max_apps = int(max_apps_raw) if max_apps_raw.isdigit() else 20

    dry_run_raw = input("  Dry run? (y/N): ").strip().lower()
    dry_run = dry_run_raw in ("y", "yes")

    print(f"\n  Searching {', '.join(platforms)} with {len(keywords)} keyword sets...")
    all_jobs, seen = [], set()
    for kw in keywords:
        jobs = await search_jobs(
            keywords=[kw], location="India",
            experience_years=3, platforms=platforms,
        )
        for j in jobs:
            if j["apply_url"] not in seen:
                seen.add(j["apply_url"])
                all_jobs.append(j)
        print(f"    {kw:30s} +{len(jobs):2d}  total={len(all_jobs)}")

    filtered = filter_jobs(all_jobs, min_match_score=0.25)
    print(f"\n  Found {len(all_jobs)} jobs, {len(filtered)} after filtering")

    if not filtered:
        print("  No matching jobs found.")
        return

    print(f"\n  Top jobs:")
    for i, j in enumerate(filtered[:10], 1):
        days = j.get("posted_days_ago", -1)
        age = f"{days}d ago" if days >= 0 else ""
        print(f"    {i:2d}. [{j['match_score']:.2f}] {j['title']}")
        print(f"        {j['company']} | {j['location']} {age}")

    confirm = input(f"\n  Apply to {min(max_apps, len(filtered))} jobs? (Y/n): ").strip().lower()
    if confirm in ("n", "no"):
        print("  Cancelled.")
        return

    mode = "DRY RUN" if dry_run else "REAL"
    print(f"\n  Applying ({mode})...")
    result = await bulk_apply(
        jobs=filtered, max_applications=max_apps, dry_run=dry_run,
    )
    print(f"\n  {json.dumps(result['summary'], indent=2)}")
    if result["applied"]:
        print("\n  Applied:")
        for a in result["applied"]:
            print(f"    [OK] {a.get('title','?')} @ {a.get('company','?')}")
    if result["failed"]:
        print("\n  Failed:")
        for f_ in result["failed"]:
            print(f"    [X]  {f_.get('title','?')} — {f_.get('error','')[:60]}")


async def do_search_only():
    platforms = get_platforms()
    keywords = get_keywords()

    print(f"\n  Searching...")
    all_jobs, seen = [], set()
    for kw in keywords:
        jobs = await search_jobs(
            keywords=[kw], location="India",
            experience_years=3, platforms=platforms,
        )
        for j in jobs:
            if j["apply_url"] not in seen:
                seen.add(j["apply_url"])
                all_jobs.append(j)

    filtered = filter_jobs(all_jobs, min_match_score=0.25)
    print(f"\n  Found {len(all_jobs)} jobs, {len(filtered)} after filtering\n")
    for i, j in enumerate(filtered, 1):
        days = j.get("posted_days_ago", -1)
        age = f"({days}d ago)" if days >= 0 else ""
        print(f"  {i:2d}. [{j['match_score']:.2f}] {j['title']}")
        print(f"      {j['company']} | {j['location']} | {j['salary']} {age}")
        print(f"      {j['apply_url'][:80]}")
        print()


def do_status():
    days = input("  Look back how many days? (default 7): ").strip()
    days = int(days) if days.isdigit() else 7
    summary = get_application_summary(days=days)
    print(f"\n  Total applications: {summary['total']} (last {days} days)")
    print(f"  By status: {json.dumps(summary['by_status'], indent=4)}")
    for plat, statuses in summary.get("by_platform", {}).items():
        for status, apps in statuses.items():
            print(f"\n  [{plat}] {status}:")
            for a in apps:
                print(f"    - {a['job_title']} @ {a['company']}")


def do_check_login_status():
    print("\n  Checking session status across all platforms...")
    res = check_all_sessions()
    print(f"  Platforms Ready: {res['logged_in_count']} / {res['total_checked']}\n")
    for plat, data in res["platforms"].items():
        status = "LOGGED IN (Ready)" if data.get("logged_in") else "NOT LOGGED IN"
        icon = "[OK]" if data.get("logged_in") else "[--]"
        print(f"    {icon} {plat:12s}: {status} ({data.get('hint', '')})")
    print()


async def main():
    while True:
        choice = show_menu()
        if choice == "1":
            do_check_login_status()
        elif choice == "2":
            await do_login()
        elif choice == "3":
            await do_search_and_apply()
        elif choice == "4":
            do_status()
        elif choice == "5":
            await do_search_only()
        elif choice == "6":
            print("\n  Bye!\n")
            break
        else:
            print("  Invalid choice.")


if __name__ == "__main__":
    asyncio.run(main())

