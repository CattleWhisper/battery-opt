"""
Guard for ADR-0001: `core/` (and the backtest) never need homeassistant.

Checked in a subprocess because this test session itself has HA
loaded: the child imports every core module plus the driver and
asserts homeassistant never entered sys.modules — which also keeps
the package __init__ light (its HA imports are deliberately lazy).
"""

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

_CHECK = (
    "import sys; "
    "import custom_components.battery_opt.core.calendar; "
    "import custom_components.battery_opt.core.prices; "
    "import custom_components.battery_opt.core.plan; "
    "import custom_components.battery_opt.core.optimiser; "
    "import custom_components.battery_opt.core.static_schedule; "
    "import custom_components.battery_opt.driver; "
    "import custom_components.battery_opt.executor; "
    "polluted = [n for n in sys.modules if n.startswith('homeassistant')]; "
    "assert not polluted, f'homeassistant imported via: {polluted}'"
)


def test_core_and_driver_import_without_homeassistant() -> None:
    """Importing core modules and the driver must not pull in HA."""
    result = subprocess.run(  # noqa: S603 - fixed argv, our own interpreter
        [sys.executable, "-c", _CHECK],
        cwd=REPO_ROOT,
        env={**os.environ, "PYTHONPATH": str(REPO_ROOT)},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
