import dataclasses
import random


@dataclasses.dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 4
    base_delay: float = 0.25
    max_delay: float = 8.0
    jitter_ratio: float = 0.2
    retry_statuses: frozenset[int] = frozenset({408, 425, 429, 500, 502, 503, 504})

    def delay(self, attempt: int, random_source: random.Random | None = None) -> float:
        source = random_source or random
        delay = min(self.max_delay, self.base_delay * (2 ** max(0, attempt - 1)))
        jitter = delay * self.jitter_ratio
        return max(0.0, delay + source.uniform(-jitter, jitter))
