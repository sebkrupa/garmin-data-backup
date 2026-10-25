import sys
from pathlib import Path

from .sync import main

if len(sys.argv) != 2:
    sys.exit("usage: python -m garmin_data_handler <person-folder>")
sys.exit(main(Path(sys.argv[1])))
