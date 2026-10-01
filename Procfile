# --threads is load-bearing: DB_WORKER_THREADS below must match it, because the
# pool keeps one warm connection per thread so a thread never queues behind a
# connect. DB_WORKER_COUNT must match --workers, so the startup log can state
# the total connection budget this service spends against Supabase.
web: gunicorn --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 120 --access-logfile - wsgi:app
