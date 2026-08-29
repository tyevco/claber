"""
build.py - what code is actually running.

`pyproject.toml` pins version = "0.1.0" and never moves, which is the
whole reason install_pi.sh needs `--force-reinstall --no-deps`: pip sees
0.1.0 already installed and skips. So the version string cannot tell you
whether the Pi is running this week's code, and the failure this repo has
actually been burned by is exactly that - old code behind an unchanged
interface.

install_pi.sh writes `_build.py` next to this file at install time with
the source revision and a digest of printers.py. It is generated, not
committed, so a git checkout has none and says so.
"""

import hashlib
from pathlib import Path


def stamp():
    """{'rev': ..., 'printers_sha': ..., 'source': 'installed'|'checkout'}"""
    try:
        from . import _build
        return {"rev": getattr(_build, "REV", "unknown"),
                "printers_sha": getattr(_build, "PRINTERS_SHA", "unknown"),
                "source": "installed"}
    except ImportError:
        pass
    # A checkout can still digest its own printers.py, which is the half
    # that actually detects a skewed print path.
    try:
        sha = hashlib.sha256(
            (Path(__file__).parent / "printers.py").read_bytes()
        ).hexdigest()[:12]
    except OSError:
        sha = "unknown"
    return {"rev": "checkout", "printers_sha": sha, "source": "checkout"}
