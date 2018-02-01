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
    return None
    
def listen_streams():
    gerrit_instances = {}
    while True:
        sys.stdout.flush()
        for inst in VCSService.objects.filter(gerrit=True):
            gerrit = gerrit_instances.get(inst)
            if gerrit is None:
                print "Connecting to %s" % inst.netloc
                gerrit_instances[inst] = connect(inst)
                continue

            event = None
            try:
                event = gerrit.get_event(block=True, timeout=1)
            except Exception, e:
                print "Error %s getting event from %s" % (e, inst.netloc)
                gerrit_instances[inst] = None
                continue

            if event is not None:
                data = event.json
                print "Got %s" % data
                if data.get("type", "") == "error-event":
                    gerrit.stop_event_stream()
                    gerrit_instances[inst] = None 
                else:
                    data["gerrit"] = inst.netloc 
                    data["vcsname"] = inst.name
                    launch_queue({"payload" : data})

class Command(BaseCommand):
    def handle(self, *args, **options):
        listen_streams()
        sys.exit(0)
