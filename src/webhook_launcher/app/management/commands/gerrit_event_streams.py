from django.core.management.base import BaseCommand, CommandError
from webhook_launcher.app.models import VCSService 
from webhook_launcher.app.misc import giturlparse
from webhook_launcher.app.bureaucrat import launch_queue
from pygerrit.client import GerritClient
from optparse import make_option
import sys, time, json, time

def connect(inst):
    print "Connecting to gerrit", inst.netloc
    parsed_netloc = giturlparse(inst.netloc)
    gerrit = GerritClient(host=parsed_netloc.netloc, port=parsed_netloc.port, keepalive=5)
    for i in range(0,5):
        try:
            version = gerrit.gerrit_version()
            gerrit.start_event_stream()
            print "Connected to Gerrit", version, "at", inst.netloc
            return gerrit
        except Exception, e:
            print "%s error try %s" % (e, i)
        time.sleep(2)
    raise RuntimeError("Gave up connecting")
    
def listen_streams():
    gerrit_instances = {}
    for inst in VCSService.objects.filter(gerrit=True):
        try:
            gerrit = connect(inst)
            gerrit_instances[inst] = gerrit
        except Exception, e:
            print e, "connecting to Gerrit", inst.netloc

    while True:
        sys.stdout.flush()
        for inst, gerrit in gerrit_instances.items():
            event = None
            event = gerrit.get_event(block=True, timeout=1)
            if event is not None:
                data = event.json
                print "Got %s" % data
                if data.get("type", "") == "error-event":
                    print "Reconnecting to %s" % inst.netloc
                    gerrit.stop_event_stream()
                    try:
                        connect(inst)
                    except RuntimeError, exc:
                        # put back error event so we hopefully try again later
                        gerrit.put_event(data)
                else:
                    data["gerrit"] = inst.netloc 
                    launch_queue({"payload" : data})
            #else:
            #    print "%s %s" % (inst.netloc, gerrit.gerrit_version())

class Command(BaseCommand):
    def handle(self, *args, **options):
        listen_streams()
        sys.exit(0)
