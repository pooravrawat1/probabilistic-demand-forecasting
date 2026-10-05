"""Command line entry point for source acquisition and audit."""

from __future__ import annotations

import argparse
from pathlib import Path
import tomllib

from .audit import audit_source
from .download import download_source


def main() -> None:
    parser = argparse.ArgumentParser(prog="demand-forecast")
    parser.add_argument("--config", type=Path, default=Path("config/project.toml"))
    commands = parser.add_subparsers(dest="command", required=True)

    download = commands.add_parser("download", help="Download the unchanged UCI archive")
    download.add_argument("--output", type=Path, default=Path("data/raw/electricityloaddiagrams20112014.zip"))

    audit = commands.add_parser("audit", help="Audit a local UCI ZIP or text source")
    audit.add_argument("--source", type=Path, default=Path("data/raw/electricityloaddiagrams20112014.zip"))
    audit.add_argument("--output-dir", type=Path, default=Path("data/audit"))

    args = parser.parse_args()
    with args.config.open("rb") as handle:
        config = tomllib.load(handle)

    if args.command == "download":
        result = download_source(config["source"]["url"], args.output)
        verb = "Already present" if result["already_present"] else "Downloaded"
        print(f"{verb}: {result['bytes']:,} bytes at {args.output}")
        print(f"SHA-256: {result['sha256']}")
    elif args.command == "audit":
        summary = audit_source(args.source, args.output_dir, config)
        print(f"Audited {summary['source']['timestamp_rows']:,} timestamp rows and "
              f"{summary['source']['client_columns']:,} clients")
        print(f"Eligible clients: {summary['eligibility']['eligible_clients']:,}")
        print(f"Audit outputs: {args.output_dir}")


if __name__ == "__main__":
    main()
