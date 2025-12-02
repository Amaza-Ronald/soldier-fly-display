# gunicorn.conf.py
bind = "0.0.0.0:10000"
workers = 2
worker_class = "gevent"
timeout = 120
keepalive = 2
max_requests = 1000
max_requests_jitter = 100
preload_app = True