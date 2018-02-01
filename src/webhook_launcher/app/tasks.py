# Copyright (C) 2013 Jolla Ltd.
# Contact: Islam Amer <islam.amer@jollamobile.com>
# All rights reserved.
# 
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
# 
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
# 
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.

import json

from django.db.models import Q

from webhook_launcher.app.models import (LastSeenRevision, QueuePeriod)
from webhook_launcher.app.bureaucrat import launch_notify, launch_build, launch_mirror

def trigger_build(mapobj, user, lsr=None, tag=None, force=False, master=None):

    if lsr is None:
        lsr, created = LastSeenRevision.objects.get_or_create(mapping=mapobj)

    if user is None:
        user = mapobj.user.username
 
    # Only fire for projects which allow webhooks. We can't just
    # rely on validation since a Project may forbid hooks after
    # the hook was created
    #if mapobj.project_disabled:
    #    print "Project has build disabled"
    #    return

    build = (mapobj.build_head or mapobj.build_tag) and mapobj.mapped
    delayed = False
    skipped = False
    qp = None

    message = ""
    if build:
        if not force:
            if lsr.handled and lsr.tag == tag:
                print "build already handled, skipping"
                build = False
                skipped = True

        # Find possible queue period objects
        qps = QueuePeriod.objects.filter(projects__name=mapobj.project,
                                         projects__obs__pk=mapobj.obs.pk)
        for qp in qps:
            if qp.delay() and not qp.override(webuser=user):
                print "Build trigger for %s delayed by %s" % (mapobj, qp)
                print qp.comment
                lsr.handled = False
                build = False
                delayed = True
                break

        if mapobj.masters.filter(Q(build_head=True) or Q(build_tag=True)).count() and master is None:
            lsr.save()
            for master in mapobj.masters.filter(Q(build_head=True) or Q(build_tag=True)):
                trigger_build(mapobj, user, lsr=lsr, tag=tag, force=force, master=master)
            lsr.handled = True
            lsr.save()
            return "%s trigger diverted to masters" % mapobj

    if lsr.tag:
        message = "Tag %s" % lsr.tag
        if force:
            message = "Forced build trigger for %s" % lsr.tag
    else:
        message = "%s" % mapobj.rev_or_head
        if force:
            message = "Forced build trigger for %s" % mapobj.rev_or_head
  
    message = "%s by %s in %s branch of %s" % (message, user, mapobj.branch,
                                               mapobj.repourl)
    if not mapobj.mapped:
        message = "%s, which is not mapped yet. Please map it." % message
    elif build:
        if master is not None:
            mapobj = master
            lsr = master.lsr

        message = ("%s, which will trigger build in project %s package "
                   "%s (%s/package/show?package=%s&project=%s)" % (message,
                    mapobj.project, mapobj.package, mapobj.obs.weburl,
                    mapobj.package, mapobj.project))
  
    elif skipped:
        message = "%s, which was already handled; skipping" % message
    elif qp and delayed:
        message = "%s, which will be delayed by %s" % (message, qp)
        if qp.comment:
            message = "%s\n%s" % (message, qp.comment)
  
    fields = mapobj.to_fields()
    if lsr.payload:
        fields['payload'] = json.loads(lsr.payload)
    else:
        fields['payload'] = json.loads("{}")

    if mapobj.notify:
        fields['msg'] = message
        launch_notify(fields)
  
    if build:
        fields = mapobj.to_fields()
        fields['branch'] = mapobj.branch
        fields['revision'] = lsr.revision or mapobj.branch
        if lsr.payload:
            fields['payload'] = json.loads(lsr.payload)
        else:
            fields['payload'] = json.loads("{}")
        launch_build(fields)
        lsr.handled = True

    if master is None:
        lsr.save()
 
    return message

def trigger_mirror(payload):
    launch_mirror(payload)
