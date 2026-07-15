import dataclasses
import datetime
import math
from collections.abc import Iterable


UTC = datetime.timezone.utc


@dataclasses.dataclass(frozen=True)
class ReconciliationPartition:
    account_uuid: str
    entity_kind: str
    partition_key: str
    depth: int = 1
    interval_seconds: int = 300
    estimated_cost: float = 1.0
    mismatch_score: float = 0.0
    clean_streak: int = 0
    last_verified_at: datetime.datetime | None = None
    next_due_at: datetime.datetime | None = None
    cursor: str | None = None

    def due_at(self) -> datetime.datetime:
        return self.next_due_at or datetime.datetime.min.replace(tzinfo=UTC)


class DynamicReconciliationScheduler:
    def __init__(
        self,
        min_interval_seconds: int = 30,
        max_interval_seconds: int = 7 * 24 * 60 * 60,
        max_depth: int = 64,
    ):
        self.min_interval_seconds = min_interval_seconds
        self.max_interval_seconds = max_interval_seconds
        self.max_depth = max_depth

    @staticmethod
    def _score(partition: ReconciliationPartition, now: datetime.datetime) -> float:
        overdue = max(0.0, (now - partition.due_at()).total_seconds()) + 1.0
        risk = 1.0 + partition.mismatch_score + math.log2(partition.depth + 1)
        return overdue * risk / max(0.01, partition.estimated_cost)

    def select(
        self,
        partitions: Iterable[ReconciliationPartition],
        now: datetime.datetime,
        budget: float,
    ) -> list[ReconciliationPartition]:
        due = [partition for partition in partitions if partition.due_at() <= now]
        due.sort(key=lambda item: self._score(item, now), reverse=True)
        selected = []
        remaining = budget
        for partition in due:
            cost = max(0.01, partition.estimated_cost)
            if selected and cost > remaining:
                continue
            selected.append(partition)
            remaining -= cost
            if remaining <= 0:
                break
        return selected

    def complete(
        self,
        partition: ReconciliationPartition,
        now: datetime.datetime,
        mismatches: int,
        actual_cost: float,
        cursor: str | None = None,
    ) -> ReconciliationPartition:
        if mismatches:
            interval = max(
                self.min_interval_seconds,
                partition.interval_seconds // 2,
            )
            depth = min(self.max_depth, max(2, partition.depth * 2))
            mismatch_score = min(
                100.0,
                partition.mismatch_score * 0.75 + mismatches,
            )
            clean_streak = 0
        else:
            clean_streak = partition.clean_streak + 1
            interval = min(
                self.max_interval_seconds,
                int(partition.interval_seconds * (1.5 if clean_streak < 3 else 2.0)),
            )
            depth = (
                max(1, partition.depth // 2) if clean_streak >= 3 else partition.depth
            )
            mismatch_score = max(0.0, partition.mismatch_score * 0.5)
        estimated_cost = max(0.01, partition.estimated_cost * 0.7 + actual_cost * 0.3)
        return dataclasses.replace(
            partition,
            depth=depth,
            interval_seconds=interval,
            estimated_cost=estimated_cost,
            mismatch_score=mismatch_score,
            clean_streak=clean_streak,
            last_verified_at=now,
            next_due_at=now + datetime.timedelta(seconds=interval),
            cursor=cursor,
        )
