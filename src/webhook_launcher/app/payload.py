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

from django.conf import settings
from django.contrib.auth.models import User

import urlparse
import json
import requests
import os

from webhook_launcher.app.models import (WebHookMapping, LastSeenRevision, RelayTarget, Project, VCSNameSpace, BuildService)

from webhook_launcher.app.tasks import trigger_build 
from webhook_launcher.app.bureaucrat import launch_notify, launch_build, launch_pr_voting, launch_pr_delete, launch_mirror
from webhook_launcher.app.misc import bbAPIcall
from webhook_launcher.app.misc import getGerritChange

def get_payload(data):
    """ payload factory function hides the messy details of detecting payload type """

    bburl = "https://bitbucket.org"
    url = None
    klass = Payload
    params = data.get('webhook_parameters', {})
    repo = data.get('repository', None)
    gh_pull_request = data.get('pull_request', None)
    gerrit = data.get('gerrit', None)
    bb_pr_keys = ["pullrequest_created"]
    # https://bitbucket.org/site/master/issue/8340/pull-request-post-hook-does-not-include
    # "pullrequest_merged", "pullrequest_declined","pullrequest_updated"

    #TODO: support more payload types
    key = None
    for key in bb_pr_keys:
        bb_pull_request = data.get(key, None)
        if bb_pull_request:
            break

    if key and bb_pull_request:
        klass = BbPull
        url = urlparse.urljoin(bburl, data[key]['destination']['repository']['full_name']) + '.git'

    elif gh_pull_request:
        # Github pull request event
        klass = GhPull
        url = data['pull_request']['base']['repo']['clone_url']

    elif gerrit:
        event_type = data["type"]
        if event_type in ["patchset-created", "change-restored", "change-abandoned", "change-merged", "comment-added", "reviewer-added", "draft-published", "topic-changed"]:
            url = data["gerrit"] + '/' + data["change"]["project"]
            klass = GerritPull
        elif event_type == "ref-updated":
            url = data["gerrit"] + "/" + data["refUpdate"]["project"]
            klass = GerritPush

    elif repo:
        if repo.get('absolute_url', None):
            # bitbucket type payload
            url = repo.get('absolute_url', None)
            canon_url = data.get('canon_url', None)
            if canon_url and url:
                print "bitbucket payload"
                if url.endswith('/'):
                    url = url[:-1]
                url = urlparse.urljoin(canon_url, url)
                if not url.endswith(".git"):
                    url = url + ".git"
                klass = BbPush

        elif repo.get('url', None):
            # github type payload
            url = repo.get('url', None)
            if url:
                print "github payload"
                if not url.endswith(".git"):
                    url = url + ".git"
                klass = GhPush

    return klass(url, params, data)

class Payload(object):

    def __init__(self, url, params, data):

        self.url = url
        self.params = params
        self.data = data

    def create_placeholder(self, repourl, branch, packages=None):
    
        vcsns = VCSNameSpace.find(repourl)
    
        project = None
        if vcsns and vcsns.default_project:
            project = vcsns.default_project.name
        elif settings.DEFAULT_PROJECT:
            project = settings.DEFAULT_PROJECT
    
        if not project:
            return []
    
        if not packages:
            packages = [""]
    
        print "no mappings, create placeholders"
        mapobjs = []
        for package in packages:
            mapobj = WebHookMapping(repourl=repourl, branch=branch,
                                    user=User.objects.get(id=1),
                                    obs=BuildService.objects.all()[0],
                                    project=project, package=package)
            mapobj.save()
            mapobjs.append(mapobj)
    
        return mapobjs

    def handle_commit(self, mapobj, lsr, user, notify=False):
    
        lsr.tag = ""
        lsr.handled = False
        lsr.save()
    
        #message = "%s commit(s) pushed by %s to %s branch of %s" % (len(lsr.payload["commits"]), user, mapobj.branch, mapobj.repourl)
        #if not mapobj.mapped:
        #    message = "%s, which is not mapped yet. Please map it." % message
    
        fields = mapobj.to_fields()
        #fields['msg'] = message
        fields['payload'] = lsr.payload

        if mapobj.notify:
            launch_notify(fields)

        if mapobj.build_head:
            trigger_build(mapobj, user, lsr=lsr)

        if mapobj.mirror_set.count():
            _ = mapobj.mirror_set.update(uptodate=False)
            launch_mirror(fields)
    
    def handle_pr(self, mapobj, pr, action="create", slave=None, voter=int(True)):

        if mapobj.masters.count() and slave is None:
            for master in mapobj.masters.all():
                self.handle_pr(master, pr, action=action, slave=mapobj, voter=voter)
            return

        fields = mapobj.to_fields()
        fields['payload'] = self.data
        fields['pr'] = pr
        fields['voter'] = voter
        if slave is None:
            fields['branch'] = pr['source_branch']
            fields['repourl'] = pr['source_repourl']
            fields['revision'] = pr['revision']
        else:
            for slv in fields['slaves']:
                if slv["id"] == slave.pk:
                    slv['branch'] = pr['source_branch']
                    slv['repourl'] = pr['source_repourl']
                    slv['revision'] = pr['revision']
               
        message = "Pull request #%s by %s from %s / %s to %s %s (%s)" % (
                pr['id'], pr['username'], pr['source_repourl'],
                pr['source_branch'], mapobj, pr['action'], pr['url'])
        print message
        fields['msg'] = message

        if mapobj.notify:
            launch_notify(fields)

        if action == "create":
            launch_pr_voting(fields)
        elif action == "delete":
            launch_pr_delete(fields)

    def relay(self, relays=None):

        if not self.url:
            return

        parsed_url = urlparse.urlparse(self.url)
        official_projects = list(set(prj.name for prj in Project.objects.filter(official=True, allowed=True)))
        official_packages = list(set(mapobj.package for mapobj in
                                WebHookMapping.objects.filter(repourl=self.url,
                                project__in=official_projects).exclude(package="")))

        service_path = os.path.dirname(parsed_url.path)
        if not relays:
            relays = list(RelayTarget.objects.filter(active=True,
                                    sources__path=service_path,
                                    sources__service__netloc=parsed_url.netloc))
            if not relays:
                return

        headers = {'content-type': 'application/json'}
        proxies = {}

        if settings.OUTGOING_PROXY:
            proxy = "%s:%s" % (settings.OUTGOING_PROXY, settings.OUTGOING_PROXY_PORT)
            proxies = {"http" : proxy, "https": proxy}

        payload = self.data
        payload["webhook_parameters"]["packages"] = official_packages
        data = json.dumps(payload)
        for relay in relays:
            #TODO: allow uploading self signed certificates and client certificates
            print "Relaying event from %s to %s" % (self.url, relay)
            response = requests.post(relay.url, data=data,
                                     headers=headers, proxies=proxies,
                                     verify=relay.verify_SSL)
            if response.status_code != requests.codes.ok:
                raise RuntimeError("%s returned %s" % (relay, response.status_code))
        

    def handle(self):

        if self.data.get('zen', None) and self.data.get('hook_id', None):
            # Github ping event, do nothing
            print "Github says hi!"
        else:
            print "unknown payload"
            print json.dumps(self.data, indent=2, sort_keys=True)

class GerritPull(Payload):

    def handle(self):
        payload = self.data
        repourl = self.url
        action = "create"
        if payload["type"] in ["change-abandoned", "change-merged"]:
            action = "delete"

        if payload["type"] == "reviewer-added":
            reviewer = payload["reviewer"]["username"]
            return

        if payload["type"] in ["comment-added"]:#, "change-restored"]:
            comment = payload.get("comment", "").lower()
            if not "trigger" in comment or not "@ci" in comment:
                return

        if payload["type"] == "topic-changed":
            fullchange = getGerritChange(payload["gerrit"], payload["change"]["number"])
            if fullchange:
                payload["change"] = fullchange
                payload["patchSet"] = fullchange["currentPatchSet"]
                self.data = payload
            else:
                return

        if payload["patchSet"].get("isDraft", False):
            return

        branch = payload["change"]["branch"]
        mappings = WebHookMapping.objects.filter(repourl=repourl, branch=branch)

        pr = { "url" : payload["change"]["url"],
               "source_repourl" : repourl,
               "source_project" : payload["change"]["project"],
               "source_branch" : payload["patchSet"]["ref"],
               "target_branch" : branch,
               "revision" : payload["patchSet"]["revision"],
               "username" : payload["change"]["owner"]["username"],
               "action" : payload['type'].replace("-", " "),
               "id" : "%s:%s" % (payload['change']['number'], payload['patchSet']['number']),
               "gerrit_id" : payload['change']['id'],
               "subject" : payload['change']['subject'],
               "vcsname" : payload["vcsname"],
              }

        if payload['change'].get('topic', None):
            pr["topic"] = payload['change']['topic']
            pr["topicurl"] = pr["url"].replace(payload['change']['number'], "#/q/topic:%s" % pr["topic"])

        for mapobj in mappings:
            if mapobj.mapped and mapobj.pr_voting:
                pr["builds"] = [othermap.package for othermap in mappings if othermap.mapped and othermap.pr_voting and mapobj.project == othermap.project]
                self.handle_pr(mapobj, pr, action=action, voter=int(pr["builds"][0] == mapobj.package))

        if payload["type"] == "topic-changed":
            action = "delete"
            pr["topic"] = payload['oldTopic']
            pr["topicurl"] = pr["url"].replace(payload['change']['number'], "#/q/topic:%s" % pr["topic"])

            for mapobj in mappings:
                if mapobj.mapped and mapobj.pr_voting:
                    pr["builds"] = [othermap.package for othermap in mappings if othermap.mapped and othermap.pr_voting and mapobj.project == othermap.project]
                    self.handle_pr(mapobj, pr, action=action, voter=int(pr["builds"][0] == mapobj.package))

class GerritPush(Payload):

    def handle(self):
        payload = self.data
        repourl = self.url
        print payload

        refsplit = payload['refUpdate']['refName'].split("/", 2)
        if len(refsplit) > 1:
            reftype, refname = refsplit[1:]
        else:
            reftype = "heads"
            refname = payload['refUpdate']['refName']

        branches = []
        if reftype == "heads":
        # commit to branch
            branches = [refname]
        elif reftype == "tags":
            print "WIP"
        else:
            print "Couldn't use payload"
            return
        
        mapobj = None
        mapobjs = WebHookMapping.objects.filter(repourl=repourl)
        if branches:
            mapobjs = mapobjs.filter(branch__in=branches)
        print mapobjs

        revision = payload['refUpdate']['newRev']
        print revision
        zerosha = '0000000000000000000000000000000000000000'
        # action
        if revision == zerosha:
            #deleted
            if reftype == "heads":
                for mapobj in mapobjs:
                    # branch was deleted
                    # FIXME: Notify
                    # NOTE: do related objects get removed ?
                    mapobj.delete()

        else:
            #created or changed
            submitter = payload.get("submitter", {})
            emails = set([submitter.get("email", "")])
            name = submitter.get("username", "")

            if not revision:
                return

            if not len(mapobjs):
                mapobjs = []
                packages = self.params.get("packages", None)
                for branch in branches:
                    mapobjs.extend(self.create_placeholder(repourl, branch,
                                                           packages=packages))

            notified = False
            for mapobj in mapobjs:
                seenrev, created = LastSeenRevision.objects.get_or_create(mapping=mapobj)
                seenrev.payload = json.dumps(payload)
                seenrev.revision = revision

                if emails:
                    seenrev.emails = json.dumps(list(emails))

                self.handle_commit(mapobj, seenrev, name, notify=mapobj.notify and not notified)
                notified = True
                #trigger_build(mapobj, name, lsr=seenrev, tag=refname)

class GhPull(Payload):

    def handle(self):

        payload = self.data
        repourl = self.url

        branch = payload['pull_request']['base']['ref']
        pr = { "url" : payload['pull_request']['html_url'],
                 "source_repourl" : payload['pull_request']['head']['repo']['clone_url'],
                 "source_branch" : payload['pull_request']['head']['ref'],
                 "username" : payload['pull_request']['user']['login'],
                 "action" : payload['action'],
                 "id" : payload['number'],
               }

        for mapobj in WebHookMapping.objects.filter(repourl=repourl, branch=branch):
            self.handle_pr(mapobj, pr)

class GhPush(Payload):

    def handle(self):

        payload = self.data
        repourl = self.url

        # github performs one POST per ref (tag/branch) touched even if they are pushed together
        refsplit = payload['ref'].split("/", 2)
        if len(refsplit) > 1:
            reftype, refname = refsplit[1:]
        else:
            print "Couldn't figure out reftype or refname"
            return

        if reftype == "tags":
        # tag
            if 'base_refs' in payload:
                branches = payload['base_refs']
            elif 'base_ref' in payload and not payload['base_ref'] is None:
                branches = [payload['base_ref'].split("/")[2]]
            else:
                # unfortunately github doesn't send info about the branch that an annotated tag is in
                # nor the commit sha1 it points at. The tag itself is enough to tell what to pull and build
                # but we wouldn't know which project / package to trigger
                # try to use the head sha1sum to detect
                print "annotated tag on %s" % repourl
                branches = []

        elif reftype == "heads":
        # commit to branch
            branches = [refname]
        else:
            print "Couldn't use payload"
            return

        print repourl
        print branches
        mapobj = None
        mapobjs = WebHookMapping.objects.filter(repourl=repourl)
        if branches:
            mapobjs = mapobjs.filter(branch__in=branches)
        print mapobjs

        zerosha = '0000000000000000000000000000000000000000'
        # action
        if payload.get('after', '') == zerosha:
            #deleted
            if reftype == "heads":
                for mapobj in mapobjs:
                    # branch was deleted
                    # FIXME: Notify
                    # NOTE: do related objects get removed ?
                    mapobj.delete()
        else:
            #created or changed
            #the head commit is either the branch's HEAD or what the tag is pointing at
            revision = None
            name = None
            emails = set()
            if 'head_commit' in payload:
                revision = payload['head_commit']['id']
                name = payload["pusher"]["name"]
                try:
                    emails.add(payload["head_commit"]["author"]["email"])
                    emails.add(payload["head_commit"]["committer"]["email"])
                except KeyError as e:
                    # do not fail if head_commit does not have "author" or "committer" info
                    pass
            else:
                revision = payload['after']
                name = payload["user_name"]
                for commit in payload.get("commits", []):
                    emails.add(commit["author"]["email"])
                    if len(emails) == 2:
                        break

            if "pusher" in payload:
                emails.add(payload["pusher"]["email"])

            if not revision or not name:
                return

            if not len(mapobjs):
                mapobjs = []
                packages = self.params.get("packages", None)
                for branch in branches:
                    mapobjs.extend(self.create_placeholder(repourl, branch,
                                                           packages=packages))

            notified = False
            for mapobj in mapobjs:
                seenrev, created = LastSeenRevision.objects.get_or_create(mapping=mapobj)
                seenrev.payload = json.dumps(payload)

                if emails:
                    seenrev.emails = json.dumps(list(emails))

                if created or seenrev.revision != revision:
                    if branches:
                        print "%s in %s was not seen before, notify it if enabled" % (revision, mapobj.branch)
                        seenrev.revision = revision

                    else:
                        # annotated tag. only continue if we already had a mapping with a matching
                        # revision
                        continue

                # notify new branch created or commit in branch
                if reftype == "heads":
                    self.handle_commit(mapobj, seenrev, name, notify=mapobj.notify and not notified)
                    notified = True

                elif reftype == "tags":
                    print "Tag %s for %s in %s/%s, notify and build it if enabled" % (refname, revision, repourl, mapobj.branch)
                    trigger_build(mapobj, name, lsr=seenrev, tag=refname)


class BbPull(Payload):

    def handle(self):

        bburl = "https://bitbucket.org"

        for key, values in self.data.items():
            if not "destination" in values:
                continue
            branch = values['destination']['branch']['name']
            source = urlparse.urljoin(bburl, values['source']['repository']['full_name'])

            pr = { "url" : urlparse.urljoin(source + '/', "pull-request/%s" % values['id']),
                     "source_repourl" : source + ".git",
                     "source_branch" : values['source']['branch']['name'],
                     "username" : values['author']['username'],
                     "action" : key.replace("pullrequest_", ""),
                     "id" : values['id'],
                 }

        for mapobj in WebHookMapping.objects.filter(repourl=self.url, branch=branch):
            self.handle_pr(mapobj, pr)


class BbPush(Payload):

    def handle(self):

        payload = self.data
        repourl = self.url

        mapobj = None
        tips = {}

        #generate pairs of branch : commits in this commit
        for comm in payload['commits']:
            if not comm['branch']:
                print "Dangling commit !" 
            else:
                if not comm['branch'] in tips:
                    tips[comm['branch']] = []
                tips[comm['branch']].append(comm['raw_node'])

        if not len(tips.keys()):
            print "no tips due to empty event, calling api"
            bbcall = bbAPIcall(payload['repository']['absolute_url'])
            bts = bbcall.branches_tags()
            for branch in bts['branches']:
                print branch
                for tag in bts['tags']:
                    print tag
                    if tag['changeset'] == branch['changeset']:
                        print "found tagged branch"
                        tips[branch['name']] = ([branch['changeset']], tag['name'])

        print tips

        for branch, ct in tips.items():
            if isinstance(ct, tuple):
                commits = ct[0]
                tag = ct[1]
            else:
                commits = ct
                tag = None

            mapobjs = WebHookMapping.objects.filter(repourl=repourl, branch=branch)

            if not len(mapobjs):
                packages = self.params.get("packages", None)
                mapobjs = self.create_placeholder(repourl, branch,
                                                  packages=packages)

            notified = False
            for mapobj in mapobjs:
                print "found or created mapping"

                seenrev, created = LastSeenRevision.objects.get_or_create(mapping=mapobj)
                seenrev.payload = json.dumps(payload)
                emails = set()
                for commit in payload["commits"]:
                    emails.add(commit["raw_author"])
                    if len(emails) == 2: break

                if emails:
                    seenrev.emails = json.dumps(list(emails))

                if created or seenrev.revision != commits[-1]:

                    print "%s in %s was not seen before, notify it if enabled" % (commits[-1], branch)
                    seenrev.revision = commits[-1]
                    self.handle_commit(mapobj, seenrev, payload["user"], notify=mapobj.notify and not notified)
                    notified = True

                else:
                    print "%s in %s was seen before, notify and build it if enabled" % (commits[-1], branch)
                    trigger_build(mapobj, payload["user"], lsr=seenrev, tag=tag)



