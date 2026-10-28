"""Turn Harbor telemetry off in each Python process with this repository on PYTHONPATH.

Harbor jobs import the agents from here, so a running round stops telemetry at its next trial.
"""

import os

os.environ["HARBOR_TELEMETRY"] = "off"
