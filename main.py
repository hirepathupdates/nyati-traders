# =============================================================================
# main.py  —  Application entry point
# =============================================================================
# Usage:
#   1. Fill in your Angel One credentials in config.py
#   2. Install dependencies:  pip install -r requirements.txt
#   3. Run:                   python main.py
# =============================================================================

import logging
import sys

from PyQt5.QtWidgets import QApplication

from ui import MainWindow


def _setup_logging() -> None:
    """Configure root logger to print INFO+ to stdout."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)-8s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def main() -> None:
    _setup_logging()

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationName("Nyati Traders")
    app.setOrganizationName("NyatiTraders")

    window = MainWindow()
    window.show()

    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
