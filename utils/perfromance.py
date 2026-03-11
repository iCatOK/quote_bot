import logging
import os
import resource
import time

log = logging.getLogger(__name__)

def _get_memory_mb() -> float:
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        rss_kb = usage.ru_maxrss
        if os.uname().sysname == "Darwin":
            return rss_kb / 1024 / 1024
        return rss_kb / 1024
    except Exception:
        return 0.0


def _get_rss_mb() -> float:
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except Exception:
        pass
    return _get_memory_mb()


class PerfTimer:
    def __init__(self, label: str):
        self.label = label
        self.start_time = 0.0
        self.start_cpu = 0.0
        self.start_rss = 0.0

    def __enter__(self):
        self.start_rss = _get_rss_mb()
        self.start_cpu = time.process_time()
        self.start_time = time.perf_counter()
        return self

    def __exit__(self, *exc):
        elapsed = time.perf_counter() - self.start_time
        cpu_used = time.process_time() - self.start_cpu
        end_rss = _get_rss_mb()
        delta_rss = end_rss - self.start_rss
        log.info(
            "⏱ [%s] wall=%.3fs cpu=%.3fs RSS=%.1fMB (Δ%+.1fMB)",
            self.label, elapsed, cpu_used, end_rss, delta_rss,
        )
        return False