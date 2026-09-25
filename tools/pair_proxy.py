"""Compatibility entrypoint for the packaged Rocket Now proxy addon."""

from pathlib import Path
import sys

_source_root = Path(__file__).resolve().parents[1] / "src"
if str(_source_root) not in sys.path:
    sys.path.insert(0, str(_source_root))

from rocketnow_cli.assets import pair_proxy as _implementation  # noqa: E402

addons = _implementation.addons
sys.modules[__name__] = _implementation
