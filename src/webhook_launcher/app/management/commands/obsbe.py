from celery import Celery
import pika
from celery.signals import task_success
import subprocess
import sys
import json

app = Celery(main='obsbe', backend='amqp', broker='amqp://celery:celery@webhooks//')

app.conf.update(
    CELERY_IGNORE_RESULT=True,
    CELERY_TASK_SERIALIZER='json',
    CELERY_ACCEPT_CONTENT = ['json'],
    CELERYD_PREFETCH_MULTIPLIER = 1,
    CELERY_SEND_TASK_ERROR_EMAILS = True,
    SERVER_EMAIL = "celery@obs-docker",
    EMAIL_HOST = "localhost",
    CELERY_TIMEZONE = 'Europe/Helsinki',
    CELERY_ENABLE_UTC = True,
)

@app.task(name='obsbe.tasks.check_project', bind=True, acks_late=True)
def check_project(self, project):
    print "got %s" % (project)

    try:
        print subprocess.check_output(["/usr/sbin/obs_admin", "--deep-check-project", str(project), "i586"], stderr=subprocess.STDOUT, shell=False)
    except subprocess.CalledProcessError as e:
        print >>sys.stderr, "Execution failed:", e.returncode, e.output

    sys.stderr.flush()
    return True

@app.task(name='obsbe.tasks.mirror', bind=True, acks_late=True)
def mirror(self, workitem):
    print workitem
    mirrors = workitem["payload"].get("mirrors",[])
    for mirror in mirrors:
        try:
            comm = ["/usr/sbin/git_mirror", mirror["source"], mirror["target"]]
            comm.extend(mirror["refspecs"].split(" "))
            print comm
            log = subprocess.check_output(comm, stderr=subprocess.STDOUT, shell=False)
            mirror["updated"] = True
            mirror["log"] = log
        except subprocess.CalledProcessError as e:
            print >>sys.stderr, "Execution failed:", e.returncode, e.output
            mirror["log"] = e.output
            mirror["updated"] = False

    sys.stderr.flush()
    return workitem

@task_success.connect
def handle_task_success(sender=None, **kwargs):
    """Report task results back to workflow engine."""

    credentials = pika.PlainCredentials('celery', 'celery')
    parameters = pika.ConnectionParameters(host="webhooks",credentials=credentials)
    connection = pika.BlockingConnection(parameters)
    channel = connection.channel()
    channel.basic_publish(exchange='',
                          routing_key='bureaucrat_msgs',
                          body=json.dumps(kwargs["result"], ensure_ascii=False).encode('utf-8'),
                          properties=pika.BasicProperties(
                              delivery_mode=2,
                              content_type='application/x-bureaucrat-message'
                          ))
    connection.close()

if __name__ == '__main__':
    app.start()

