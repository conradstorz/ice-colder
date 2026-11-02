# ice_edge/vend/__main__.py
"""CLI for `dispensers.toml`: `uv run python -m ice_edge.vend`."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from ice_contracts.board.vending_machine import SubsystemCapabilities
from ice_common.config_model import ConfigModel
from ice_common.paths import config_path
from ice_edge.vend.dispensers import DispenserProfiles, dispensers_path


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for dispensers validation.

    Returns:
        0: validation ok (or --example)
        1: validation has errors
        2: directory path or unreadable config
    """
    parser = argparse.ArgumentParser(
        prog="python -m ice_edge.vend",
        description="Validate dispensers.toml against the product catalog",
    )
    parser.add_argument(
        "path",
        nargs="?",
        help="Path to dispensers.toml (default: $ICE_COLDER_DISPENSERS or dispensers.toml)",
    )
    parser.add_argument(
        "--config",
        help="Path to config.json (default: $ICE_COLDER_CONFIG or config.json)",
    )
    parser.add_argument(
        "--capabilities",
        metavar="FILE",
        help="Path to JSON file with SubsystemCapabilities",
    )

    action_group = parser.add_mutually_exclusive_group()
    action_group.add_argument(
        "--example",
        action="store_true",
        help="Print example dispensers.toml and exit",
    )
    action_group.add_argument(
        "--check",
        action="store_true",
        help="Check dispensers.toml (default action)",
    )

    args = parser.parse_args(argv)

    if args.example:
        from ice_edge.vend.dispensers_doc import render_example

        sys.stdout.write(render_example())
        return 0

    dispensers_file_path = dispensers_path() if args.path is None else Path(args.path)

    # Not `ice_edge.app.startup_config.load_config`: `vend` sits below `app`
    # in the import-linter layers, and that loader would also create a
    # default config.json on a first run -- wrong for a read-only check.
    config_path_str = args.config or str(config_path())
    try:
        with open(config_path_str, "r", encoding="utf-8") as f:
            config = ConfigModel.model_validate(json.load(f))
    except ValidationError as exc:
        error_msg = f"{exc.error_count()} validation error(s) in {config_path_str}"
        print(f"Error reading config: {error_msg}", file=sys.stderr)
        return 2
    except (OSError, ValueError) as exc:
        error_msg = str(exc).splitlines()[0]
        print(f"Error reading config: {error_msg}", file=sys.stderr)
        return 2

    profiles = DispenserProfiles(config, dispensers_file_path)
    try:
        report = profiles.load()
    except OSError as exc:
        error_msg = str(exc).splitlines()[0]
        print(f"Error reading dispensers file: {error_msg}", file=sys.stderr)
        return 2

    if args.capabilities:
        try:
            with open(args.capabilities, "r", encoding="utf-8") as f:
                caps = SubsystemCapabilities.model_validate(json.load(f))
            report = profiles.set_capabilities(caps)
        except ValidationError as exc:
            error_msg = (
                f"{exc.error_count()} validation error(s) in {args.capabilities}"
            )
            print(f"Error reading capabilities: {error_msg}", file=sys.stderr)
            return 2
        except (OSError, ValueError) as exc:
            error_msg = str(exc).splitlines()[0]
            print(f"Error reading capabilities: {error_msg}", file=sys.stderr)
            return 2

    print(report.render_text())
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
