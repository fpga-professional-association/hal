#!/usr/bin/env python3
"""Fetch the Jane Street ASIC puzzle files from the upstream repository.

The puzzle inputs (GDS, VCD, warm-up chain) belong to Jane Street and carry no
redistribution license, so this HAL example does not check them in. Run this
script once to populate the example directory, then follow README.md.

Usage:  python tools/fetch_puzzle.py
"""

import urllib.request
from pathlib import Path

BASE = "https://raw.githubusercontent.com/janestreet/asic-puzzle-2026/HEAD/"
ROOT = Path(__file__).resolve().parent.parent

# remote path -> local path (their README is stored as upstream_README.md so it
# does not collide with this example's own README.md)
FILES = {
    "README.md": "upstream_README.md",
    "example_inputs.vcd": "example_inputs.vcd",
    "layout.png": "layout.png",
    "puzzle.gds": "puzzle.gds",
    "warmup/00_source.v": "warmup/00_source.v",
    "warmup/01_netlist.v": "warmup/01_netlist.v",
    "warmup/02_netlist_with_power_rails.v": "warmup/02_netlist_with_power_rails.v",
    "warmup/03_post_place_and_route.def": "warmup/03_post_place_and_route.def",
    "warmup/04_final.gds": "warmup/04_final.gds",
}


def main() -> None:
    for remote, local in FILES.items():
        dest = ROOT / local
        if dest.exists():
            print(f"exists   {local}")
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        print(f"fetching {local}")
        urllib.request.urlretrieve(BASE + remote, dest)
    print("done")


if __name__ == "__main__":
    main()
