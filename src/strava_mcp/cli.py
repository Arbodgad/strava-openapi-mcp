"""Command line entry point for the Strava MCP server and maintenance commands."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from . import __version__
from .auth import OAuthError, run_local_oauth
from .config import Settings
from .openapi import SpecError, SpecStore, build_operations
from .server import run_stdio


def _settings() -> Settings:
    try:
        return Settings.from_env()
    except ValueError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc


def _list_tools(settings: Settings) -> int:
    operations = build_operations(SpecStore(settings).load())
    for operation in operations:
        print(f"{operation.method.upper():6} {operation.path:35} {operation.tool_name}")
        description = " ".join(operation.description.split())
        print(f"       Description: {description}")
    print(f"\n{len(operations)} generated tools")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generic Strava MCP server generated from Swagger")
    parser.add_argument("--version", action="version", version=__version__)
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("auth", help="Run the local browser-based Strava OAuth flow")
    subparsers.add_parser(
        "update-spec", help="Download, validate, and save the official Swagger spec"
    )
    subparsers.add_parser("show-config", help="Show effective non-secret configuration")
    subparsers.add_parser("list-tools", help="List generated tools")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = _settings()
    try:
        if args.command == "auth":
            tokens = run_local_oauth(settings)
            print(
                f"Authorization saved for scopes: {', '.join(tokens.scopes) or '(none reported)'}"
            )
            return 0
        if args.command == "update-spec":
            version, path, refs = SpecStore(settings).update()
            message = (
                f"Saved Strava Swagger version {version} to {path} "
                f"({refs} referenced schema documents)"
            )
            print(message)
            return 0
        if args.command == "show-config":
            print(json.dumps(settings.safe_dict(), indent=2, ensure_ascii=False))
            return 0
        if args.command == "list-tools":
            return _list_tools(settings)
        asyncio.run(run_stdio(settings))
        return 0
    except (SpecError, OAuthError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
