"""Suite-wide configuration.

Hypothesis runs deterministically in CI (`derandomize`), so a failing property is a real
defect rather than a lucky draw, and without a deadline, so a slow runner cannot turn a
passing property into a flaky one. Set HYPOTHESIS_PROFILE=dev locally for more, random
examples.
"""

from __future__ import annotations

import os

from hypothesis import HealthCheck, settings

settings.register_profile(
    "ci",
    max_examples=100,
    deadline=None,
    derandomize=True,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile("dev", max_examples=300, deadline=None)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "ci"))
