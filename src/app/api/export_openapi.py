"""Export the OpenAPI schema to docs/openapi.json, or check that the file is up to date.

uv run python -m app.api.export_openapi          # write the file
uv run python -m app.api.export_openapi --check  # exit 1 if the file differs from the code
"""

import argparse
import json
import sys
from pathlib import Path

from app.api.main import create_app

DEFAULT_PATH = Path("docs/openapi.json")


def render_schema() -> str:
    # The schema is built from routes and models only; no database or settings needed
    schema = create_app().openapi()
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if the file is outdated")
    parser.add_argument("--path", type=Path, default=DEFAULT_PATH)
    args = parser.parse_args(argv)

    expected = render_schema()
    if args.check:
        actual = args.path.read_text(encoding="utf-8") if args.path.exists() else ""
        if actual != expected:
            print(  # noqa: T201  # CLI output
                f"{args.path} is outdated. Run: uv run python -m app.api.export_openapi",
                file=sys.stderr,
            )
            return 1
        print(f"{args.path} is up to date")  # noqa: T201
        return 0

    args.path.parent.mkdir(parents=True, exist_ok=True)
    args.path.write_text(expected, encoding="utf-8")
    print(f"Wrote {args.path}")  # noqa: T201
    return 0


if __name__ == "__main__":
    sys.exit(main())
