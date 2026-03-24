"""Entry point for the ``miniquota`` command.

Reads workspace_dir and statusline script path from config.yaml,
so the command works regardless of the caller's working directory.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from miniclaw.config import load_config


def main() -> None:
    config = load_config()
    workspace_dir = config["agent"]["workspace_dir"]
    script_name = config["statusline"]["script"]
    script = Path(workspace_dir) / script_name
    python_path = sys.executable
    result = subprocess.run(
        [str(python_path), str(script)],
        check=True,
        input="{}",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    print(result.stdout)


if __name__ == "__main__":
    main()
