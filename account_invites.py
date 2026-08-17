#!/usr/bin/env python3
"""Create one-time, email-bound account registration invitations."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from platform_store import PlatformStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("create", choices=["create"])
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--email", required=True)
    parser.add_argument("--hours", type=int, default=72)
    args = parser.parse_args(argv)
    try:
        invite, token = PlatformStore(args.project_root).create_registration_invite(
            args.email, hours=args.hours,
        )
    except (OSError, ValueError, RuntimeError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}), file=sys.stderr)
        return 1
    print(json.dumps({**invite, "token": token}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
