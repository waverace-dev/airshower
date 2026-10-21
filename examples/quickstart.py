"""Run from the repository root: python3 examples/quickstart.py.

Uses a temporary local server and fictional fixtures. No package installation,
provider account, API credit, desktop access, or real file sharing is involved.
For a persistent service, use ``python3 -m airshower serve --help``.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from airshower.demo import run_demo


if __name__ == "__main__":
    print(json.dumps(run_demo(), indent=2, sort_keys=True))
