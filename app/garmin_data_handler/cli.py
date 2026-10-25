"""Command line: new / edit / sync for one person folder."""

from __future__ import annotations

import argparse
import getpass
from datetime import date
from pathlib import Path

from .datasets import BY_NAME, DATASETS
from .sync import API_KEY_FILE, DATASETS_FILE, START_DATE_FILE, Api, datasets_template, prepare, sync_person

DEFAULT_ROOT = Path(__file__).resolve().parents[2] / "person"  # same folder ./run uses


def _person_dir(root: Path, name: str) -> Path:
    if not name or name.startswith(".") or name != Path(name).name or "\\" in name:
        raise ValueError(f"Invalid person name: {name!r} (use a plain folder name)")
    return root / name


def new_person(root: Path, name: str) -> Path:
    person_dir = _person_dir(root, name)
    if person_dir.exists():
        raise ValueError(f"Person already exists: {person_dir}")
    person_dir.mkdir(parents=True)
    prepare(person_dir)
    return person_dir


def edit_person(root: Path, name: str, api_key: str | None = None,
                datasets: list[str] | None = None, start_date: str | None = None) -> Path:
    person_dir = _person_dir(root, name)
    if not person_dir.is_dir():
        raise ValueError(f"No such person: {person_dir}")
    unknown = [d for d in datasets or [] if d not in BY_NAME]
    if unknown:
        raise ValueError(f"Unknown data set: {', '.join(unknown)}. Known: {', '.join(BY_NAME)}")
    if start_date is not None:
        try:
            date.fromisoformat(start_date)
        except ValueError:
            raise ValueError(f"Invalid start date: {start_date!r} (use yyyy-MM-dd)") from None
    if api_key is not None:
        key_file = person_dir / API_KEY_FILE
        key_file.touch(mode=0o600)
        key_file.chmod(0o600)  # before the secret is written
        key_file.write_text(api_key.strip() + "\n")
    if datasets is not None:
        # keep the template layout; disabled sets are commented out, so read_datasets still parses it
        lines = [("" if line.split("#", 1)[0].strip() in datasets else "# ") + line
                 if line.split("#", 1)[0].strip() in BY_NAME else line
                 for line in datasets_template().splitlines()]
        (person_dir / DATASETS_FILE).write_text("\n".join(lines) + "\n")
    if start_date is not None:
        (person_dir / START_DATE_FILE).write_text(start_date + "\n")
    return person_dir


def onboard(root: Path, ask=input, ask_secret=getpass.getpass) -> None:
    """Interactive setup for `./run`: create the root and a first person, ask for missing API keys.

    Empty answer to a question skips it (no name: stop; no key: sync reports it later)."""
    root.mkdir(parents=True, exist_ok=True)
    if not any(p.is_dir() and not p.name.startswith(".") for p in root.iterdir()):
        print(f"There is no person folder in {root}.")
        while True:
            name = ask("Name the first person to download Garmin data for (empty = cancel): ").strip()
            if not name:
                return
            try:
                new_person(root, name)
                break
            except ValueError as e:
                print(f"Error: {e}")
        print(f"Created {root / name}.")
    for d in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")):
        key_file = d / API_KEY_FILE
        if key_file.exists() and key_file.read_text().strip():
            continue
        prepare(d)
        print("Get the key from https://connect.garmin.com/app/settings/personalApiToken")
        key = ask_secret(f"Now provide the API key for {d.name} (input hidden, empty = later): ").strip()
        if key:
            edit_person(root, d.name, api_key=key)
            print(f"Saved key to {key_file} (mode 600).")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="garmin_data_handler", description="Garmin data backup")
    p.add_argument("--person-root", type=Path, default=DEFAULT_ROOT, help="folder with person folders")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("new", help="create a person folder").add_argument("name")
    e = sub.add_parser("edit", help="change API key, data sets or start date")
    e.add_argument("name")
    e.add_argument("--api-key")
    e.add_argument("--datasets", type=lambda s: [x.strip() for x in s.split(",") if x.strip()],
                   help="comma-separated, others are turned off: " + ",".join(d.name for d in DATASETS))
    e.add_argument("--start-date", help="yyyy-MM-dd")
    s = sub.add_parser("sync", help="download (full on first run, then only what is missing)")
    s.add_argument("name")
    s.add_argument("--full", action="store_true", help="download everything again")
    return p


def run(argv: list[str], api_factory=Api) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "new":
            d = new_person(args.person_root, args.name)
            print(f"Created {d}. Next: edit {args.name} --api-key <key>")
        elif args.command == "edit":
            d = edit_person(args.person_root, args.name, args.api_key, args.datasets, args.start_date)
            print(f"Updated {d}")
        else:
            d = _person_dir(args.person_root, args.name)
            if not d.is_dir():
                raise ValueError(f"No such person: {d}")
            r = sync_person(d, api_factory=api_factory, full=args.full)
            print(f"{r.person}: {r.status} | ok={r.ok} failed={r.failed} rows={r.rows}"
                  + (f" | data from {r.origin}" if r.origin else ""))
            for f in r.failures[:5]:
                print(f"  - {f}")
            return 0 if r.status == "ok" and not r.failed else 1
    except ValueError as e:
        print(f"Error: {e}")
        return 2
    return 0
