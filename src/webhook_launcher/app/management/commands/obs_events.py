from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from webhook_launcher.app.bureaucrat import launch
from optparse import make_option
import os, sys, time, json, time

from celery import Celery
from celery import bootsteps
from kombu import Consumer, Exchange, Queue


app = Celery(main="obs_events", broker='amqp://celery:celery@127.0.0.1')

app.config_from_object('django.conf:settings')

app.conf.update(
    CELERY_IGNORE_RESULT=True,
    CELERY_TASK_SERIALIZER='json',
    CELERY_ACCEPT_CONTENT = ['json'],
    CELERYD_PREFETCH_MULTIPLIER = 1,
    CELERY_SEND_TASK_ERROR_EMAILS = True,
    SERVER_EMAIL = "celery@webhooks-docker",
    EMAIL_HOST = "mail.ge.com",
)

class MyConsumerStep(bootsteps.ConsumerStep):
    

    def get_consumers(self, channel):
        return [Consumer(channel,
                         queues=[Queue('obs', Exchange('obs'), 'obs', auto_declare = False)],
                         callbacks=[self.handle_message],
                         accept=['json'], auto_declare = False)]

    def handle_message(self, body, message):
        payload = json.loads(body)
        pfile = os.path.join(settings.PROCESS_DIR, "%s.xml" % payload.get("eventtype"))
        if os.path.exists(pfile):
            launch(pfile, payload)
        message.ack()

app.steps['consumer'].add(MyConsumerStep)

class Command(BaseCommand):
    def handle(self, *args, **options):
        app.worker_main(argv=["obs_events"])
        sys.exit(0)
