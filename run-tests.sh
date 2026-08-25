#!/bin/bash
# The CLI declares its deps inline for `uv run --script`; pytest needs the same
# set plus pytest itself, so the test run mirrors that list here.
cd "$(dirname "$0")" || exit 1
exec uv run --quiet \
  --with pytest --with typer --with rich --with python-frontmatter --with pyyaml \
  python -m pytest tests/ "$@"
