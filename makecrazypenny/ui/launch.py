"""Console entry point that launches the Streamlit dashboard.

Exposed as the ``makecrazypenny-dashboard`` console script. It shells into
Streamlit's own CLI (``streamlit run <dashboard.py>``) so the app runs under the
real Streamlit runtime. Streamlit is imported lazily here so this module stays
import-safe when the optional ``ui`` extra is not installed.
"""

from __future__ import annotations

import sys
from pathlib import Path


def launch() -> None:
    """Run ``streamlit run`` on the bundled dashboard, forwarding extra args.

    Exit codes: ``2`` if Streamlit is not installed; otherwise whatever the
    Streamlit CLI returns.
    """
    try:
        from streamlit.web import cli as stcli
    except ModuleNotFoundError:
        sys.stderr.write(
            "Streamlit is not installed. Install the UI extra:\n"
            "    pip install -e .[ui]\n"
        )
        raise SystemExit(2) from None

    app = str(Path(__file__).with_name("dashboard.py"))
    # Replace argv with a Streamlit invocation, preserving any user-passed flags
    # (e.g. --server.port 8502) after the script path.
    sys.argv = ["streamlit", "run", app, *sys.argv[1:]]
    raise SystemExit(stcli.main())


if __name__ == "__main__":
    launch()
