import sys
from pathlib import Path

from . import cli
from .sync import main

args = sys.argv[1:]
# Legacy `./run` form: one bare path. A person root literally named new/edit/sync is ambiguous; pass it as ./new.
if len(args) == 1 and args[0] not in ("new", "edit", "sync") and not args[0].startswith("-"):
    sys.exit(main(Path(args[0])))
sys.exit(cli.run(args))
