"""An identifier that never goes online, for tests of the ETF service."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ident import Identifier  # noqa: E402


class NoFigi:
    def lookup(self, keys):
        return {k: None for k in keys}


def offline_ids():
    return Identifier(figi=NoFigi(), ftd=None, yahoo=None, background=False)
