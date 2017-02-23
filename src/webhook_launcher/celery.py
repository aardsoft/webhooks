from __future__ import absolute_import
import os
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "webhook_launcher.settings")

import pika
import json
import django
from django.conf import settings
django.setup()

from celery import Celery
from celery.signals import task_success

app = Celery('bureaucrat',
             broker='amqp://localhost',
             include=['webhook_launcher.tasks'])

app.config_from_object('django.conf:settings')

app.conf.update(
    CELERY_IGNORE_RESULT=True,
    CELERY_TASK_SERIALIZER='json',
    CELERY_ACCEPT_CONTENT = ['json'],
    CELERYD_PREFETCH_MULTIPLIER = 1,
    CELERY_SEND_TASK_ERROR_EMAILS = True,
    SERVER_EMAIL = "celery@webhooks",
    EMAIL_HOST = "smtp",
    CELERY_ENABLE_UTC = True,
)

@task_success.connect
def handle_task_success(sender=None, **kwargs):
    """Report task results back to workflow engine."""

    parameters = pika.ConnectionParameters(host="localhost")
    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()
    channel.basic_publish(exchange='',
                          routing_key='bureaucrat_msgs',
                          body=json.dumps(kwargs["result"]),
                          properties=pika.BasicProperties(
                              delivery_mode=2,
                              content_type='application/x-bureaucrat-message'
                          ))
    connection.close()

if __name__ == '__main__':
    app.start()
