"""One durable worker; safely supports more workers using PostgreSQL row locks."""
import functools
import logging
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
from integrations.base import ProviderError
from . import services

logger=logging.getLogger('operations.worker')
running=True

def stop(*_):
    global running
    running=False

def _logged(kind,handler):
    @functools.wraps(handler)
    def run(job):
        try:
            return handler(job)
        except services.StillPending:
            raise
        except ProviderError as exc:
            logger.warning('Job %s (%s) attempt %s refused by provider: %s',job.pk,kind,job.attempts,exc)
            raise
        except Exception:
            logger.exception('Job %s (%s) attempt %s failed',job.pk,kind,job.attempts)
            raise
    run.logs_failures=True
    return run

def install_job_logging():
    """Log every job failure before run_one turns it into a generic needs_review message."""
    for kind,handler in list(services.HANDLERS.items()):
        if getattr(handler,'logs_failures',None) is not True:
            services.HANDLERS[kind]=_logged(kind,handler)

def accountant_housekeeping():
    """Day 1: queue last month's accountant package; also drop expired ZIPs. Never stops the worker."""
    try:
        from core.accountant.jobs import housekeeping
        housekeeping()
    except Exception:
        logger.exception('Accountant package scheduling failed')

def main():
    signal.signal(signal.SIGTERM,stop);signal.signal(signal.SIGINT,stop)
    install_job_logging()
    logger.info('Worker started')
    last_cleanup=0
    while running:
        close_old_connections()
        try:
            processed=services.run_one()
            now=time.time()
            if now-last_cleanup>3600:
                Session.objects.filter(expire_date__lt=timezone.now()).delete();last_cleanup=now
                accountant_housekeeping()
                try:
                    from core.nfce.service import sweep
                    sweep()  # contingency number blocks left unused become inutilizações
                except Exception:
                    logger.exception('NFC-e sweep failed')
            Path('/tmp/pdv2-worker-health').write_text(str(int(now)))
            if not processed:time.sleep(2)
        except Exception:
            logger.exception('Worker iteration failed')
            time.sleep(5)
    logger.info('Worker stopped')
if __name__=='__main__':main()
