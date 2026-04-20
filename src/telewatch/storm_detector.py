from collections import deque
from enum import Enum
import time

class StormState(Enum):
    NORMAL   = 'normal'
    STORM    = 'storm'
    COOLDOWN = 'cooldown'

class StormDetector:
    """
    Sliding-window alert rate tracker.
    Thread-safe for single-process async bots.
    """

    def __init__(
        self,
        threshold: int = 10,
        window_seconds: int = 60,
        cooldown_seconds: int = 300,
    ):
        self.threshold        = threshold
        self.window_seconds   = window_seconds
        self.cooldown_seconds = cooldown_seconds
        self._timestamps: deque = deque()
        self._state           = StormState.NORMAL
        self._cooldown_until  = 0.0

    # ── public API ──────────────────────────────────────────────────────────

    def record_alert(self) -> bool:
        """
        Call every time an alert would fire.
        Returns True the moment we tip into STORM state.
        """
        self._evict_old()

        if self._state == StormState.COOLDOWN:
            if time.time() >= self._cooldown_until:
                self._state = StormState.NORMAL
            else:
                return False   # still cooling down

        self._timestamps.append(time.time())

        if (self._state == StormState.NORMAL
                and len(self._timestamps) >= self.threshold):
            self._state = StormState.STORM
            return True        # caller should escalate

        return False

    def is_storming(self) -> bool:
        """True while in STORM or COOLDOWN state."""
        return self._state in (StormState.STORM, StormState.COOLDOWN)

    def enter_cooldown(self):
        """Call after OpenCode job completes."""
        self._state = StormState.COOLDOWN
        self._cooldown_until = time.time() + self.cooldown_seconds
        self._timestamps.clear()

    def reset(self):
        """Force back to NORMAL (e.g. on /resume command)."""
        self._state = StormState.NORMAL
        self._cooldown_until = 0.0
        self._timestamps.clear()

    @property
    def state(self) -> StormState:
        return self._state

    # ── internals ───────────────────────────────────────────────────────────

    def _evict_old(self):
        cutoff = time.time() - self.window_seconds
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()
