"""One durable worker; safely supports more workers using PostgreSQL row locks."""
import os
import signal
import time
from pathlib import Path
os.environ.setdefault('DJANGO_SETTINGS_MODULE','pdvcloud.settings')
import django
django.setup()
from django.db import close_old_connections
from django.contrib.sessions.models import Session
from django.utils import timezone
from .services import run_one

running=True

def stop(*_):
    global running
    running=False

def main():
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    last_cleanup=0
    while running:
        close_old_connections()
        try:
            processed=run_one()
            now=time.time()
            if now-last_cleanup>3600:
                Session.objects.filter(expire_date__lt=timezone.now()).delete();last_cleanup=now
            Path('/tmp/pdv2-worker-health').write_text(str(int(now)))
            if not processed:time.sleep(2)
        except Exception:
            import logging
            logging.exception('Worker iteration failed')
            time.sleep(5)
if __name__=='__main__':main()
