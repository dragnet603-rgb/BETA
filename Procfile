# Tuned for small-RAM (512MB) servers:
#  - 1 worker: halves baseline Python memory vs 2 workers (export is async,
#    so workers are never tied up rendering video; it also keeps the
#    in-memory export-job store consistent).
#  - 4 threads: enough concurrency for uploads/API without 8 simultaneous
#    request stacks.
#  - max-requests recycles the worker periodically so slow leaks can't
#    accumulate on a long-running small box.
#  - MALLOC_ARENA_MAX=2 curbs glibc heap fragmentation under threads.
web: MALLOC_ARENA_MAX=2 gunicorn app:app --workers 1 --threads 4 --timeout 300 --max-requests 200 --max-requests-jitter 50 --bind 0.0.0.0:$PORT
