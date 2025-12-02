# gunicorn.conf.py
bind = "0.0.0.0:10000"
workers = 1
threads = 2
worker_class = "gthread"
timeout = 120
keepalive = 5
max_requests = 1000
max_requests_jitter = 50
preload_app = False