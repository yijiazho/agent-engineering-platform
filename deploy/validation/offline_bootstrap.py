"""Install AEP's hash-locked validation environment without network access."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


def bootstrap_and_compile(workspace: Path) -> None:
    workspace = workspace.resolve(strict=True)
    validation = workspace / "deploy" / "validation"
    requirements = validation / "offline-requirements.txt"
    wheelhouse = validation / "wheelhouse"
    if not requirements.is_file() or not wheelhouse.is_dir():
        raise SystemExit("offline validation artifacts are incomplete")

    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-index",
            "--no-deps",
            "--require-hashes",
            "--find-links",
            str(wheelhouse),
            "--requirement",
            str(requirements),
        ],
        check=True,
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--disable-pip-version-check",
            "--no-index",
            "--no-deps",
            "--no-build-isolation",
            "--editable",
            str(workspace),
        ],
        check=True,
    )
    subprocess.run(
        [
            sys.executable,
            "-m",
            "compileall",
            "-q",
            str(workspace / "src"),
            str(workspace / "tests"),
        ],
        check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", type=Path, default=Path("/workspace"))
    arguments = parser.parse_args()
    bootstrap_and_compile(arguments.workspace)


if __name__ == "__main__":
    main()
