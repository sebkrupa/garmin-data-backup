import sys
from pathlib import Path

from . import cli
from .sync import main

args = sys.argv[1:]
missing = "--missing-data" in args
args = [a for a in args if a != "--missing-data"]
# Legacy `./run` form: one bare path. A person root literally named new/edit/sync is ambiguous; pass it as ./new.
if len(args) == 1 and args[0] not in ("new", "edit", "sync") and not args[0].startswith("-"):
    if sys.stdin.isatty():
        cli.onboard(Path(args[0]))
    sys.exit(main(Path(args[0]), missing=missing))
if missing:
    sys.exit("--missing-data works only with ./run (no subcommand)")
sys.exit(cli.run(args))
