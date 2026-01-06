import psutil
import time
from typing import TypedDict

_process = psutil.Process()


class ProcessMetrics(TypedDict):
    """Container for process-level performance metrics."""

    cpu_time_s: float
    memory_mb: float
    timestamp: float


def collect_process_metrics() -> ProcessMetrics:
    """
    Collects current process metrics: CPU time and RSS memory.

    Returns:
        ProcessMetrics: A TypedDict containing:
            - cpu_time_s: Total CPU time (user + system) in seconds.
            - memory_mb: Resident Set Size (RSS) memory in MB.
            - timestamp: Current Unix timestamp.
    """
    global _process
    cpu = _process.cpu_times()
    mem = _process.memory_info()

    return ProcessMetrics(
        cpu_time_s=cpu.user + cpu.system,
        memory_mb=mem.rss / (1024 * 1024),
        timestamp=time.time(),
    )
