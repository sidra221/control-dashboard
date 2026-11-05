"""يتأكد إنه مخزن تحديد المعدّل (settings.RATE_LIMIT_BACKEND) صحيح تحت طلبات
متزامنة على القاعدة/الـ cache الفعلية — شغّله على سيرفر الإنتاج بعد النشر:

    python manage.py ratelimit_selftest
    python manage.py ratelimit_selftest --threads 64 --limit 20

بيطلق N خيط بنفس اللحظة على نفس العدّاد: لازم بالضبط `limit` منهم يعدّوا
والباقي يتحجب. بيمسح عدّاده بعد الفحص. exit code 1 لو فشل.
"""

import sys
import threading
import uuid

from django.core.management.base import BaseCommand
from django.db import connection

from apitokens.services import ratelimit


class Command(BaseCommand):
    help = "Concurrent correctness self-test for the rate-limit store"

    def add_arguments(self, parser):
        parser.add_argument("--threads", type=int, default=32)
        parser.add_argument("--limit", type=int, default=10)

    def handle(self, *args, **opts):
        threads, limit = opts["threads"], opts["limit"]
        store = ratelimit.get_store()
        key = f"rl:selftest:{uuid.uuid4().hex}:0"
        counts, errors = [], []
        lock = threading.Lock()
        barrier = threading.Barrier(threads)

        def worker():
            try:
                barrier.wait(timeout=30)
                value = store.incr(key, 60)
                with lock:
                    counts.append(value)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(repr(exc))
            finally:
                connection.close()

        pool = [threading.Thread(target=worker) for _ in range(threads)]
        for t in pool:
            t.start()
        for t in pool:
            t.join()
        try:
            store.delete(key)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"cleanup: {exc!r}")

        allowed = sum(1 for c in counts if c <= limit)
        distinct = len(set(counts)) == len(counts)
        self.stdout.write(
            f"backend={getattr(ratelimit.settings, 'RATE_LIMIT_BACKEND', 'database')} "
            f"threads={threads} limit={limit} -> allowed={allowed} blocked={len(counts) - allowed} "
            f"errors={len(errors)} distinct_counts={distinct}"
        )
        ok = not errors and len(counts) == threads and allowed == limit and distinct
        for e in errors[:3]:
            self.stdout.write(self.style.ERROR(f"  {e}"))
        self.stdout.write(self.style.SUCCESS("PASS") if ok else self.style.ERROR("FAIL"))
        if not ok:
            sys.exit(1)
