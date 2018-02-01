from __future__ import absolute_import

import os
from collections import defaultdict
import datetime
from pygerrit.client import GerritClient
from pygerrit.error import GerritError

os.environ['DJANGO_SETTINGS_MODULE'] = 'webhook_launcher.settings'

from webhook_launcher.celery import app
from webhook_launcher.app.payload import get_payload
from webhook_launcher.app.misc import giturlparse
from webhook_launcher.app.models import BuildService, Mirror

from osc import conf, core
from urllib import quote_plus
from urllib2 import HTTPError, URLError
from httplib import HTTPException
from StringIO import StringIO
import xml.etree.cElementTree as ElementTree
import xml.sax.saxutils as saxutils

import json

from celery.utils.log import get_task_logger

logger = get_task_logger(__name__)

@app.task(bind=True, acks_late=True)
def handle_webhook(self, workitem):
    """Handle POST request to a webhook."""

    payload = get_payload(workitem["payload"]["payload"])
    payload.handle()

    return workitem

@app.task
def relay_webhook(workitem):
    """Relay webhook POST request to task queue."""

    payload = get_payload(workitem["payload"]["payload"])
    payload.relay()

    return workitem

service_template = """
<services>
<service name="tar_git">
  <param name="url">%(repourl)s</param>
  <param name="branch">%(branch)s</param>
  <param name="revision">%(revision)s</param>
  <param name="token">%(token)s</param>
  <param name="debian">%(debian)s</param>
  <param name="dumb">%(dumb)s</param>
  <param name="manifest">%(manifest)s</param>
  <param name="bitbake">%(bitbake)s</param>
  <param name="distro">%(distro)s</param>
  <param name="machine">%(machine)s</param>
  %(pins)s
</service>
</services>
"""

@app.task(bind=True, acks_late=True)
def trigger_service(self, workitem):
    f = workitem["payload"]
    project = f["project"]
    package = f["package"]

    params = {}
    for pn in ["repourl", "branch", "revision", "token", "debian", "dumb", "manifest", "bitbake", "distro", "machine",]:
        p = f.get(pn, "")
        if p == "None" or p is None:
            p = ""
        params[pn] = p

    pins = []
    for pin in f.get("slaves", []):
        pins.append('  <param name="pin">%s:%s:%s</param>' % (pin["package"], pin["branch"], pin["revision"]))
    params["pins"] = "\n".join(pins)

    conf.get_config()
    apiurl = conf.config["apiurl_aliases"].get(f["ev"]["namespace"], conf.config["apiurl"])

    try:
        core.show_files_meta(apiurl, str(project), str(package), expand=False, meta=True)
    except (HTTPError, HTTPException, URLError) as exc:
        body=""
        if hasattr(exc, 'read'):
            body = exc.read()
        logger.warn("%s show_files_meta %s %s: %s" % (exc, project, package, body))
        if hasattr(exc, "code") and getattr(exc, "code", 0) == 404:
            data = core.metatypes['pkg']['template']
            data = StringIO(data % { "name" : str(package), "user" : conf.config['api_host_options'][apiurl]['user'] }).readlines()
            u = core.makeurl(apiurl, ['source', str(project), str(package), "_meta"])
            try:
                x = core.http_PUT(u, data="".join(data))
            except (HTTPError, HTTPException) as exc:
                body=""
                if hasattr(exc, 'read'):
                    body = exc.read()
                logger.warn("%s creating package %s %s: %s" % (exc, project, package, body))
                raise self.retry(exc=exc, countdown=self.request.retries ** 2, max_retries=20)
        else:
            raise self.retry(exc=exc, countdown=self.request.retries ** 2, max_retries=20)

    service = service_template % params
    try:
        core.http_PUT(core.makeurl(apiurl, ['source', project, package, "_service"]),
                  data=service)
    except (HTTPError, HTTPException, URLError) as exc:
        logger.warn("%s trying to put service file in %s %s" % (exc, project, package))
        raise self.retry(exc=exc, countdown=self.request.retries ** 2, max_retries=25)

    return workitem

@app.task(bind=True, acks_late=True)
def create_branch(self, workitem):

    f = workitem["payload"]
    parent_project = f["project"]
    package = f["package"]
    name = f["pr"].get("vcsname", package)
    pr_id = f["pr"]["id"]
    builds = f["pr"].get("builds", None)

    conf.get_config()
    apiurl = conf.config["apiurl_aliases"].get(f["ev"]["namespace"], conf.config["apiurl"])

    # move to build system support classes
    newprj = parent_project + ":" + name + ":" + pr_id
    title = f["pr"]["url"]
    topic = f["pr"].get("topic", None)
    gating_macros = ["_gating_%s 1" % package.replace("-", "_")]
    if builds:
        gating_macros.extend(["_gating_%s 1" % pkg.replace("-", "_") for pkg in builds if pkg != package])

    #rebuild = "local"
    if topic and topic.startswith("feat-"):
        #rebuild = "transitive"
        newprj = parent_project + ":" + f["pr"]["target_branch"] + ":" + topic
        title = f["pr"]["topicurl"]
        pr_id = topic.replace("-","_")
        try:
            for pkg in core.meta_get_packagelist(apiurl, newprj):
                gating_macros.append("_gating_%s 1" % pkg.replace("-", "_"))
        except Exception as exc:
            pass

    prjrepos = defaultdict(list)
    try:
        logger.warn("checking parent %s" % (parent_project))
        parent_prjxml = core.http_GET(core.make_meta_url('prj', path_args=(quote_plus(parent_project),), apiurl=apiurl)).read()
        parent_tree = ElementTree.fromstring(parent_prjxml)
        parent_elm = parent_tree.find('build')

        logger.warn("getting repos of %s" % parent_project)
        for r in core.get_repos_of_project(apiurl, parent_project):
            prjrepos[r.name].append(r.arch)

        repostr = "".join(['<repository name="%s" linkedbuild="localdep" block="local"><path project="%s" repository="%s"/>%s</repository>' % (reponame, parent_project, reponame, "".join(["<arch>%s</arch>" % a for a in archs])) for reponame, archs in prjrepos.items()])
        prjxml = '<project name="%s"><title>%s</title><description/><build><disable/></build><link project="%s"/>%s</project>' % (newprj, title, parent_project, repostr)

        tree = ElementTree.fromstring(prjxml)
        elm = tree.find('build')
        if elm is None:
            elm = ElementTree.SubElement(tree, 'build')
        elm.clear()
        if not parent_elm is None:
            elm.extend(list(parent_elm))

        prjxml = ElementTree.tostring(tree)
        logger.warn("creating project %s" % newprj)
        core.http_PUT(core.make_meta_url('prj', path_args=quote_plus(newprj), apiurl=apiurl), data=prjxml)

        logger.warn("setting prjconf for project %s" % newprj)
        macros = """
%%define _gating 1
%s
releasesuffix: .gating.%s

Macros:
%%_gating 1
%s

%%_source_payload w3.gzdio
%%_binary_payload w3.gzdio
:Macros
""" % ("\n".join(["%define " + macro for macro in gating_macros]), pr_id.replace(":","."), "\n".join(["%" + macro for macro in gating_macros]))

        core.http_PUT(core.make_meta_url('prjconf', path_args=quote_plus(newprj), apiurl=apiurl), data=macros)

        logger.warn("getting %s %s meta" % (parent_project, package))
        pkgxml = core.http_GET(core.make_meta_url('pkg', path_args=(quote_plus(parent_project), quote_plus(package)), apiurl=apiurl)).read()

        tree = ElementTree.fromstring(pkgxml)
        tree.attrib["project"] = newprj
        title = tree.find("title")
        if not title is None:
            title.text = title
        pkgxml = ElementTree.tostring(tree)

        logger.warn("creating package %s %s" % (newprj, package))
        core.http_PUT(core.make_meta_url('pkg', path_args=(quote_plus(newprj), quote_plus(package)), apiurl=apiurl), data=pkgxml)

    except (HTTPError, HTTPException, URLError) as exc:
        body=""
        if hasattr(exc, 'read'):
            body = exc.read()
        logger.warn("%s creating %s %s branch: %s" % (exc, parent_project, package, body))
        raise self.retry(exc=exc, countdown=self.request.retries ** 2, max_retries=40)

    f["project"] = newprj
    f["parent_project"] = parent_project
    workitem["payload"] = f
    return workitem


@app.task(bind=True, acks_late=True)
def disable_branch(self, workitem):
    f = workitem["payload"]
    project = f["project"]
    package = f["package"]
    conf.get_config()
    apiurl = conf.config["apiurl_aliases"].get(f["ev"]["namespace"], conf.config["apiurl"])
    #topic = f["pr"].get("topic", None)
    #if topic and topic.startswith("feat-"):
    #    return workitem

    try:
        logger.warn("checking branch %s" % (project))
        prjxml = core.http_GET(core.make_meta_url('prj', path_args=(quote_plus(project),), apiurl=apiurl)).read()
        tree = ElementTree.fromstring(prjxml)

        elm = tree.find('build')
        if elm is None:
            elm = ElementTree.SubElement(tree, 'build')
        elm.clear()
        ElementTree.SubElement(elm, 'disable')
        elm = tree.find('publish')
        if elm is None:
            elm = ElementTree.SubElement(tree, 'publish')
        elm.clear()
        ElementTree.SubElement(elm, 'enable')

        if len(f.get("results", [])) <= 1:
            elm = tree.find('link')
            if not elm is None:
                tree.remove(elm)

        prjxml = ElementTree.tostring(tree)
        logger.warn("disabling branch %s" % (project))
        core.http_PUT(core.make_meta_url('prj', path_args=quote_plus(project), apiurl=apiurl), data=prjxml)

        for pkg in core.meta_get_packagelist(apiurl, project):
            logger.warn("checking branch %s %s" % (project, pkg))
            pkgxml = core.http_GET(core.make_meta_url('pkg', path_args=(quote_plus(project), quote_plus(pkg)), apiurl=apiurl)).read()
            tree = ElementTree.fromstring(pkgxml)

            elm = tree.find('build')
            if not elm is None:
                elm.clear()
            pkgxml = ElementTree.tostring(tree)

            logger.warn("disabling branch %s %s" % (project, pkg))
            core.http_PUT(core.make_meta_url('pkg', path_args=(quote_plus(project), quote_plus(pkg)), apiurl=apiurl), data=pkgxml)

    except (HTTPError, HTTPException) as exc:
        logger.warn("%s" % exc)
        if hasattr(exc, 'code') and getattr(exc, 'code', 0) == 404:
            return workitem

        raise self.retry(exc=exc, countdown=self.request.retries ** 2, max_retries=20)

    return workitem

@app.task(bind=True, acks_late=True)
def delete_branch(self, workitem):

    f = workitem["payload"]
    project = f["project"]
    package = f["package"]
    pr_id = f["pr"]["id"]
    topic = f["pr"].get("topic", None)
    name = f.get("vcsname", package)

    conf.get_config()
    apiurl = conf.config["apiurl_aliases"].get(f["ev"]["namespace"], conf.config["apiurl"])
    # move to build system support classes
    target_project = project+":"+name+":"+pr_id
    if topic and topic.startswith("feat-"):
        target_project = project + ":" + f["pr"]["target_branch"] + ":" + f["pr"]["topic"]
    
    try:
        empty = False
        #if topic and topic.startswith("feat-"):
        logger.info("deleting %s %s" % (target_project, package))
        core.delete_package(apiurl, target_project, package, force=True, msg="cleanup")
        empty = len(core.meta_get_packagelist(apiurl, target_project))

        if empty:
            logger.info("deleting %s" % target_project)
            core.delete_project(apiurl, target_project, force=True, msg="cleanup")
            change, patchset = pr_id.split(":", 1)
            projects = [ prj for prj in core.meta_get_project_list(apiurl) if prj.startswith(project+":"+package+":"+change) ]
            for target_project in projects:
                logger.warn("deleting %s" % target_project)
                core.delete_project(apiurl, target_project, force=True, msg="cleanup")
    except (HTTPError, HTTPException) as exc:
        if hasattr(exc, 'code') and getattr(exc, 'code', 0) == 404:
            pass
        else:
            logger.warn("%s" % exc)
            raise self.retry(exc=exc, countdown=self.request.retries ** 2, max_retries=20)

    return workitem

# temporary placeholder until BuildService model has support for OBS and other build systems
def obs_create_request(apiurl, options_list, description, comment, supersede = False, **kwargs):

    commentElement = ElementTree.Element("comment")
    commentElement.text = comment

    state = ElementTree.Element("state")
    state.set("name", "new")
    state.append(commentElement)

    request = core.Request()
    request.description = description
    request.state = core.RequestState(state)

    supsersedereqs = []
    for item in options_list:
        if item['action'] == "submit":
            request.add_action(item['action'],
                               src_project = item['src_project'],
                               src_package = item['src_package'],
                               tgt_project = item['tgt_project'],
                               tgt_package = item['tgt_package'],
                               src_rev = core.show_upstream_rev(apiurl, item['src_project'], item['src_package']),
                               **kwargs)

            if supersede == True:
                supsersedereqs.extend(core.get_exact_request_list(apiurl, item['src_project'],
                                                                  item['tgt_project'], item['src_package'],
                                                                  item['tgt_package'], req_type='submit',
                                                                  req_state=['new','review', 'declined']))
    request.create(apiurl)

    if supersede == True and len(supsersedereqs) > 0:
        processed = []
        for req in supsersedereqs:
            if req.reqid not in processed:
                processed.append(req.reqid)
                print "req.reqid: %s - new ID: %s\n"%(req.reqid, request.reqid)
                core.change_request_state(apiurl, req.reqid,
                                          'superseded',
                                          'superseded by %s' % request.reqid,
                                          request.reqid)

    return request

@app.task
def auto_promote(workitem):

    f = workitem["payload"]
    project = f["project"]
    package = f["package"]
    pr_id = f["pr"]["id"]

    conf.get_config()
    apiurl = conf.config["apiurl_aliases"].get(f["ev"]["namespace"], conf.config["apiurl"])

    actions = [{"action" : "submit", "src_project" : project+":"+pr_id, "src_package" : package,
                        "tgt_project" : project, "tgt_package" : package}]
    comment = ""
    result = obs_create_request(apiurl, options_list=actions, description="", comment=comment, supersede=True, opt_sourceupdate="cleanup")
    return workitem

@app.task(bind=True, acks_late=True)
def accept_request(self, workitem):
    conf.get_config()
    apiurl = conf.config["apiurl_aliases"].get(workitem["payload"]["namespace"], conf.config["apiurl"])
    rid = str(workitem["payload"]["id"])

    try:
        results = core.change_request_state(apiurl, rid, "accepted", message="ok")
    except (HTTPError, HTTPException, URLError) as exc:
        logger.warn("%s accepting %s " % (exc, rid))
        raise self.retry(exc=exc, countdown=self.request.retries ** 2, max_retries=20)
    return workitem

def get_repourl(apiurl, project, repository):
    root = ElementTree.fromstring(''.join(core.show_configuration(apiurl)))
    elm = root.find('download_url')
    url_tmpl = elm.text + '/%s/%s/'
    repos = core.get_repositories_of_project(apiurl, project)
    repourls = []
    for repo in repos:
        if repo == repository:
            return url_tmpl % (project.replace(':', ':/'), repo)
    return None

def get_revdeps(apiurl, res, project=None):
    if project is None:
        project = res["project"]

    revdeps = set()
    query = ['package=%s' % quote_plus(res["package"]), 'view=revpkgnames']
    revdepurl = core.makeurl(apiurl, ['build', project, res["repository"], res["arch"], '_builddepinfo'], query=query)
    depinfoxml = core.http_GET(revdepurl).read()
    tree = ElementTree.fromstring(depinfoxml)
    for pkg in tree.findall("package"):
        for pkgdep in pkg.findall("pkgdep"):
            if pkgdep.text == "wearable-release":
                continue
            revdeps.add(pkgdep.text)
    return revdeps

@app.task(bind=True, acks_late=True)
def check_build_results(self, workitem):
    conf.get_config()
    if "ev" in workitem["payload"]:
        ns = workitem["payload"]["ev"]["namespace"]
    elif "namespace" in workitem["payload"]:
        ns = workitem["payload"]["namespace"]
    else:
        logger.warn("Don't know how to handle this workitem, ignoring ..")
        print json.dumps(workitem, indent=4)
        return workitem

    apiurl = conf.config["apiurl_aliases"].get(ns, conf.config["apiurl"])

    # by default wait for no revdeps
    project = None
    package = None
    main_packages = set()
    revdeps = set()
    if "project" in workitem["payload"]:
        project = workitem["payload"]["project"]
        package = workitem["payload"]["package"]
        main_packages.add(package)
    if "actions" in workitem["payload"]:
        project = workitem["payload"]["actions"][0]["targetproject"]
        package = workitem["payload"]["actions"][0]["targetpackage"]
        main_packages.update([action["targetpackage"] for action in workitem["payload"]["actions"]])

    if not project or not package:
        logger.warn("Don't know how to handle this workitem, ignoring ..")
        print json.dumps(workitem, indent=4)
        return workitem

    if "pr" in workitem["payload"]:
        topic = workitem["payload"]["pr"].get("topic", None)
        if topic and topic.startswith("feat-"):
            main_packages.update(core.meta_get_packagelist(apiurl, project))
        builds = workitem["payload"]["pr"].get("builds", None)
        if builds:
            main_packages.update(builds)
            
    try:
        results = core.show_results_meta(apiurl, project)
    except (HTTPError, HTTPException) as exc:
        logger.warn("%s getting results for %s" % (exc, project))
        if hasattr(exc, 'code') and getattr(exc, 'code', 0) == 404:
            return workitem
        else:
            raise self.retry(exc=exc, countdown=self.request.retries ** 2, max_retries=60)

    self.request.retries = 0

    tree = ElementTree.fromstring(''.join(results))
    main_results = []
    excluded = set()
    main_result = True
    for package in main_packages:
        logger.info("Checking %s %s state" % (project, package))

        for result in tree.findall('result'):

            if result.get("dirty") == "true":
                # If the repository is dirty state needs recalculation and
                # cannot be trusted
                exc = RuntimeError("%s %s/%s %s dirty" % (project, result.get('repository'), result.get('arch'), package))
                logger.warn("%s" % exc)
                raise self.retry(exc=exc, countdown=30, max_retries=600)

            for status in result.findall('status'):

                if not status.get("package") == package:
                    continue

                code = status.get('code')
                vote = 0

                if code == "succeeded":
                    vote = 1

                elif code == "broken":
                    try:
                        details = status.find("details")
                        if details:
                            res = core.http_POST(core.makeurl(apiurl, ['source', project, package], query={'cmd' : 'runservice'}))
                            exc = RuntimeError("%s %s/%s %s %s %s" % (project, result.get('repository'), result.get('arch'), package, code, details.text))
                            logger.warn("%s" % exc)
                            raise self.retry(exc=exc, countdown=30, max_retries=600)
                        else:
                            vote = -1
                            main_result = False
                    except (HTTPError, HTTPException, URLError) as exc:
                        logger.warn("runservice %s %s %s" % (project, package, exc))
                        exc = RuntimeError("%s %s/%s %s %s" % (project, result.get('repository'), result.get('arch'), package, code))
                        logger.warn("%s" % exc)
                        raise self.retry(exc=exc, countdown=self.request.retries ** 2, max_retries=40)

                elif code in ["excluded", "disabled"]:
                    excluded.add(status.get("package"))
                    continue

                elif code in ["failed", "unresolvable", "broken"]:
                    vote = -1
                    main_result = False

                elif code in ["blocked", "scheduled", "dispatching", "building", "signing", "finished", "unknown"]:
                    exc = RuntimeError("%s %s/%s %s %s" % (project, result.get('repository'), result.get('arch'), package, code))
                    raise self.retry(exc=exc, countdown=30, max_retries=600)

                logurl = core.makeurl(apiurl, ['package', 'live_build_log', project, package, result.get('repository'), result.get('arch')])
                desc = "%s %s %s" % (package, result.get('repository'), code)
                repourl = ""
                if code == "succeeded":
                    try:
                        repourl = get_repourl(apiurl, project, result.get('repository'))
                    except (HTTPError, HTTPException, URLError) as exc:
                        logger.warn("get repourl %s %s %s" % (project, package, exc))
                        raise self.retry(exc=exc, countdown=self.request.retries ** 2, max_retries=40)
                 
                main_results.append({"label" : "Verified", "type" : "build", "reported" : False, "vote" : vote, "desc" : desc, "logurl" : logurl, "repourl" : repourl, "project" : project, "package" : status.get("package"), "repository" : result.get('repository'), "arch" : result.get('arch')})

        self.request.retries = 0

    if not main_results:
        exc = RuntimeError("%s %s no results" % (project, package))
        raise self.retry(exc=exc, countdown=30, max_retries=600)

    self.request.retries = 0

    parent_project = workitem["payload"].get("parent_project")
    # collect revdeps for the main packages
    for res in main_results:
        try:
            if parent_project:
                revdeps.update(get_revdeps(apiurl, res, project=parent_project))
            revdeps.update(get_revdeps(apiurl, res))
        except Exception, exc:
            logger.warn("Error getting revdeps %s" % exc)
    logger.warn("Reverse deps for %s %s detected %s" % (project, package, revdeps))

    self.request.retries = 0

    revresults = []
    for result in tree.findall('result'):

        if result.get("dirty") == "true":
            # If the repository is dirty state needs recalculation and
            # cannot be trusted
            exc = RuntimeError("%s %s/%s %s dirty" % (project, result.get('repository'), result.get('arch'), package))
            logger.warn("%s" % exc)
            raise self.retry(exc=exc, countdown=30, max_retries=600)

        for status in result.findall('status'):
            code = status.get('code')
            vote = 0

            if status.get("package") in main_packages:
                continue

            if status.get("package") not in revdeps:
                continue

            if code == "broken":

                details = status.find("details")
                if details:
                    exc = RuntimeError("%s %s/%s %s %s %s" % (project, result.get('repository'), result.get('arch'), package, code, details.text))
                    logger.warn("%s" % exc)
                    raise self.retry(exc=exc, countdown=30, max_retries=600)
                else:
                    continue

            elif code in ["excluded", "disabled"]:
                excluded.add(status.get("package"))
                continue

            elif code in ["failed", "unresolvable"]:
                vote = -1

            elif code in ["blocked", "scheduled", "dispatching", "building", "signing", "finished", "unknown"]:
                exc = RuntimeError("%s %s/%s %s %s" % (project, result.get('repository'), result.get('arch'), status.get("package"), code))
                raise self.retry(exc=exc, countdown=30, max_retries=600)

            #elif code == "succeeded":
                

            logurl = core.makeurl(apiurl, ['package', 'live_build_log', project, status.get("package"), result.get('repository'), result.get('arch')])
            desc = "%s %s %s" % (status.get("package"), result.get('repository'), code)
            repourl = ""
            #if code == "succeeded":
            #    try:
            #        repourl = get_repourl(apiurl, project, result.get('repository'))
            #    except (HTTPError, HTTPException) as exc:
            #        logger.warn("get repourl %s %s %s" % (project, package, exc))
            #        raise self.retry(exc=exc, countdown=self.request.retries ** 2, max_retries=20)

            revresults.append({"label" : "Verified", "type" : "build", "reported" : False, "vote" : vote, "desc" : desc, "logurl" : logurl, "repourl" : repourl, "project" : project, "package" : status.get("package"), "repository" : result.get('repository'), "arch" : result.get('arch')})

    self.request.retries = 0

    all_results = main_results + revresults
    if revdeps and not revdeps.issubset(set(x["package"] for x in all_results)):
        missing = revdeps - set(x["package"] for x in all_results)
        exc = RuntimeError("%s %s missing results" % (project, package))
        logger.warn("%s %s" % (exc, missing))
        if not main_result:
            logger.warn("%s %s build failed, ignore missing results" % (project, package))
        else:
            for x in missing:
                if x in excluded:
                    logger.warn("%s %s is excluded, skipping" % (project, x))
                else:
                    logger.warn("%s %s waiting for missing results, pinging backend" % (project, package))
                    print app.send_task('obsbe.tasks.check_project', args=(project,), queue='obsbe') 
                    raise self.retry(exc=exc, countdown=30, max_retries=600)
    else:
        logger.warn("%s %s No missing results" % (project, package))


    workitem["payload"]["results"] = all_results

    return workitem

@app.task(bind=True, acks_late=True)
def pr_vote(self, workitem):
    # Get from db
    default_label = "Verified"

    #obs = BuildService.objects.filter(namespace=workitem["payload"]["ev"]["namespace"])
    #if obs.count():
    #    msg.append("%s/package/show/%s/%s" % (obs[0].weburl, workitem["payload"]["project"], workitem["payload"]["package"]))
    
    gitrepourl = workitem["payload"]["pr"]["source_repourl"]
    parsed_netloc = giturlparse(gitrepourl)
    gerrit = GerritClient(host=parsed_netloc.netloc, port=parsed_netloc.port)
    project = workitem["payload"]["pr"]["source_project"]
    change = workitem["payload"]["payload"]["change"]["number"]
    patchset = workitem["payload"]["payload"]["patchSet"]["number"]
    chpas = set(["%s,%s" % (change, patchset)])
    topic = workitem["payload"]["pr"].get("topic", None)
    results = workitem["payload"].get("results",[])
    labels = set([res.get("label", default_label) for res in results])

    if topic and topic.startswith("feat-"):
        try:
            result = gerrit.run_command("query topic:'%s' --current-patch-set --format=JSON" % topic)
            resstdout = result.stdout.readlines()[:-1]
            resstderr = result.stderr.readlines()
            if resstderr:
                logger.warn(resstderr)
            for line in resstdout:
                change_json = json.loads(line)
                chpas.add("%s,%s" % (change_json["number"], change_json["currentPatchSet"]["number"]))
        except GerritError, exc:
            logger.warn("%s getting changes for topic %s" % (exc, topic))
            #raise self.retry(exc=exc, countdown=self.request.retries ** 2, max_retries=20)

    for label in labels:
        msg = []
        vote = "0"
        for result in results:
            if result.get("reported", False):
                continue
            if not result.get("label", default_label) == label:
                continue

            msg.append("* %s %s" % (result["type"], result["desc"]))
            logurl = result.get("logurl")
            if logurl: msg.append("%s log : %s" % (result["type"], logurl))
            repourl = result.get("repourl")
            if repourl: msg.append("repository : %s" % repourl)
            info = result.get("info")
            if info: msg.extend(info)
            msg.append("")

            if not vote == "-1":
               if result.get("vote", 0) < 0:
                   vote = "-1"
               elif result.get("vote", 0) > 0:
                   vote = "+1"

        if not msg:
            continue

        try:
            logger.warn(" ".join((gitrepourl, project, label, vote, " ".join(chpas), " ".join(msg))))
            result = gerrit.run_command("review --message '\n\n%s' --label %s=%s %s" % ("\n".join(msg), label, vote, " ".join(chpas)))
            resstdout = result.stdout.read()
            resstderr = result.stderr.read()
            if resstdout:
                logger.warn(resstdout)
            if resstderr:
                logger.warn(resstderr)

            for result in results:
                if result.get("reported", False):
                    continue
                if not result.get("label", default_label) == label:
                    continue
                result["reported"] = True

            if vote == "-1":
                workitem["payload"]["state"] = "fail"

        except GerritError, exc:
            logger.warn("%s adding review for %s %s %s" % (exc, project, change, patchset))
            raise self.retry(exc=exc, countdown=self.request.retries ** 2, max_retries=20)

    if not labels:
        try:
            conf.get_config()
            if "ev" in workitem["payload"]:
                ns = workitem["payload"]["ev"]["namespace"]
            elif "namespace" in workitem["payload"]:
                ns = workitem["payload"]["namespace"]
            project = workitem["payload"]["project"]
            apiurl = conf.config["apiurl_aliases"].get(ns, conf.config["apiurl"])
            monitor_url = "%s/project/monitor/%s" % (apiurl, project)
            msg = ["Starting change verification, see %s" % monitor_url]
            logger.warn(" ".join((gitrepourl, project, " ".join(chpas), " ".join(msg))))
            result = gerrit.run_command("review --message '\n\n%s' %s" % ("\n".join(msg), " ".join(chpas)))
            resstdout = result.stdout.read()
            resstderr = result.stderr.read()
            if resstdout:
                logger.warn(resstdout)
            if resstderr:
                logger.warn(resstderr)

        except GerritError, exc:
            logger.warn("%s commenting for %s %s %s" % (exc, project, change, patchset))
            pass
        
    return workitem

@app.task(bind=True, acks_late=True)
def get_merged_pr(self, workitem):
    payload = workitem["payload"]["payload"]
    gerrit = payload.get("gerrit", None)
    if gerrit is None:
        logger.warn("Not a gerrit merge, bailing out")
        return workitem

    if payload['type'] != "ref-updated":
        logger.warn("Not a gerrit ref-updated, bailing out")
        return workitem
    project = payload["refUpdate"]["project"]
    rev = payload["refUpdate"]["newRev"]
    branch = payload["refUpdate"]["refName"]

    
    parsed_netloc = giturlparse(gerrit)
    gerrit = GerritClient(host=parsed_netloc.netloc, port=parsed_netloc.port, keepalive=5)
    try:
        result = gerrit.run_command("query project:%s status:merged commit:%s branch:%s --current-patch-set --format=JSON" % (project, rev, branch))
        resstdout = result.stdout.read()
        resstderr = result.stderr.read()
        if resstderr:
            logger.warn(resstderr)
        if resstdout:
            change = json.loads(resstdout.splitlines()[0])
            if change.get("status", None) is None:
                logger.warn("didn't find a usable change for this merge")
                return workitem

            payload.update(change)

    except (ValueError, GerritError), exc:
        logger.warn("%s querying patchset for %s commit %s %s" % (exc, project, branch, rev))
        raise self.retry(exc=exc, countdown=self.request.retries ** 2, max_retries=4)
    return workitem


@app.task(bind=True, acks_late=True)
def dump(self, workitem):
    print json.dumps(workitem, indent=4)
    return workitem

@app.task(bind=True, acks_late=True)
def mirrored(self, workitem):
    for mirror in workitem["payload"].get("mirrors",[]):
        target = giturlparse(mirror["target"])
        name = os.path.basename(target.path)
        ns = os.path.dirname(target.path)
        netloc = mirror["target"].replace(name, "").rpartition(ns)[0]
        print ns
        print netloc
        print name
 
        mirror_obj = Mirror.objects.get(source__repourl = mirror["source"], target__path = ns, target__service__netloc = netloc, name = name, refspecs = mirror["refspecs"])
        mirror_obj.uptodate = mirror["updated"]
        mirror_obj.log = mirror["log"]
        mirror_obj.save()
         
    return workitem
