from __future__ import absolute_import
import os, sys, re, StringIO, string, tempfile, shutil, itertools, time
from celery import Celery
from subprocess import Popen, PIPE, CalledProcessError, STDOUT, check_output
from osc import conf, core
from StringIO import StringIO
import xml.etree.cElementTree as ElementTree
import pika
from celery.signals import task_success
import json
from rpmUtils.miscutils import splitFilename
from urllib2 import HTTPError, URLError
from httplib import HTTPException

app = Celery(main='autodoc', backend='amqp', broker='amqp://celery:celery@127.0.0.1//')

app.conf.update(
    CELERY_IGNORE_RESULT=True,
    CELERY_TASK_SERIALIZER='json',
    CELERY_ACCEPT_CONTENT = ['json'],
    CELERYD_PREFETCH_MULTIPLIER = 1,
    CELERY_SEND_TASK_ERROR_EMAILS = True,
    SERVER_EMAIL = "celery@webhooks-docker",
    EMAIL_HOST = "localhost",
    CELERY_TIMEZONE = 'Europe/Helsinki',
    CELERY_ENABLE_UTC = True,
)


def extract_rpm(rpm_file, work_dir, patterns=None):
    """Extract rpm package contents.

    :param rpm_file: RPM file name
    :param work_dir: The RPM is extracted under this direcory.
            Also rpm filename can be given relative to this dir
    :param patterns: List of filename patterns to extract. Extract all if None
    :returns: List of extracted filenames (relative to work_dir)
    :raises: subprocess.CalledProcessError if extraction failed
    """

    tmp_patterns = None
    rpm2cpio_args = ["rpm2cpio", rpm_file]
    cpio_args = ["cpio", '-idv']
    if patterns:
        tmp_patterns = tempfile.NamedTemporaryFile(mode="w")
        tmp_patterns.file.writelines([pat + "\n" for pat in patterns])
        tmp_patterns.file.flush()
        cpio_args += ["-E", tmp_patterns.name]

    p_convert = Popen(rpm2cpio_args, stdout=PIPE, cwd=work_dir)
    p_extract = Popen(cpio_args,
        stdin=p_convert.stdout, stderr=PIPE, cwd=work_dir)
    # Close our copy of the fd after p_extract forked it
    p_convert.stdout.close()

    _, std_err = p_extract.communicate()
    p_convert.wait()

    if tmp_patterns:
        tmp_patterns.close()

    if p_convert.returncode:
        raise CalledProcessError(p_convert.returncode, rpm2cpio_args)
    if p_extract.returncode:
        raise CalledProcessError(p_extract.returncode, cpio_args)

    file_list = std_err.strip().split('\n')
    # cpio reports blocks on the last line
    return [line.strip() for line in file_list[:-1] if not line.startswith("cpio:")]

def log_to_html(apiurl, result):

    project = result["project"]
    repo = result["repository"]
    arch = result["arch"]
    package = result["package"]
    #FIXME
    wd = "/home/autodoc/adoc"
    secpattern = re.compile("^.*\ \#\#\#\#\ (?P<sec>.+)$")
    infopattern = re.compile("^.*\ \#\#\#\#\ INFO\ (?P<info>.+)$")
    warnpattern = re.compile("^.*\ warning.*$", re.I)
    errorpattern = re.compile("^.*\ error.*$", re.I)
    cursec = None
    cursubsec = None
    errorfound = False
    info = []
    # to protect us against control characters
    all_bytes = string.maketrans('', '')
    remove_bytes = all_bytes[:8] + all_bytes[14:32] # accept tabs and newlines

    query = {'nostream' : '1', 'last' : '1'}
    u = core.makeurl(apiurl, ['build', project, repo, arch, package, '_log'], query=query)
    log = tempfile.NamedTemporaryFile(dir=wd, delete=False)
    log.write("= %s build log\n:webfonts!:\n:toc2:\n:toc-title!:\n\n== Start\n" % (package))

    for data in core.streamfile(u, bufsize="line"):
        line = data.translate(all_bytes, remove_bytes).strip()
        marker = secpattern.search(line)
        line = line.replace("++", "\\++")
        if not marker:
             if warnpattern.match(line):
                 log.write("\nWARNING: `%s` +\n\n" % line)
             elif errorpattern.match(line):
                 if not errorfound:
                     log.write("[[error,Error]]")
                     errorfound = True
                 log.write("\nIMPORTANT: `%s` +\n\n" % line)
             else:
                 log.write("`%s` +\n" % line)
        else:
            infoline = infopattern.search(line)
            if infoline:
                log.write("\nTIP: `%s` +\n\n" % infoline.group("info"))
                info.append(infoline.group("info"))
                continue
            (newsec, _, newsubsec) = marker.group("sec").partition(" ")
            if cursec != newsec:
                log.write("\n== %s\n" % newsec)
                cursec = newsec
            if newsubsec:
                log.write("\n=== %s\n" % newsubsec)
                cursubsec = newsubsec

    log.write("\n== End\n")
    log.close()
    #FIXME
    logroot_dir="/data/autodoc/docs/logs"
    logroot_url="http://wm-build.health.ge.com/docs/logs"
    logdir = os.path.join(logroot_dir, project.replace(":", ":/"), repo, arch)
    html = os.path.join(logdir, "%s.html" % package)
    htmlurl = os.path.join(logroot_url, project.replace(":", ":/"), repo, arch, "%s.html" % package)
    try:
        os.makedirs(logdir)

    except Exception as exc:
        print exc

    try:
        args = ['asciidoctor', '-a', 'theme=volnitsky', '-a', 'icons', '-a', 'data-uri', '-a', 'docinfo1', '-o', html, log.name]
        print check_output(args)
        if os.path.exists(html):
            result["logurl"] = htmlurl + '#_end'
        if not result.get("info"):
            result["info"] = []
        result["info"].extend(info)
    except Exception as exc:
        print exc
    finally:
        os.remove(log.name)

@app.task(name='autodoc.tasks.fancy_buildlog', bind=True, acks_late=True)
def fancy_buildlog(self, workitem):
    conf.get_config()
    ns = workitem["payload"].get("namespace", None)
    if ns is None:
        ns = workitem["payload"]["ev"]["namespace"]
    
    apiurl = conf.config["apiurl_aliases"].get(ns, conf.config["apiurl"])
    project = workitem["payload"]["project"]
    package = workitem["payload"]["package"]
    results = workitem["payload"].get("results",[])
    if not results:
        repository = workitem["payload"].get("repository")
        arch = workitem["payload"].get("arch")
        if repository and arch:
            results = [{"type":"build", "project":project, "package":package, "repository":repository, "arch":arch}]

    for result in results:
        if result.get("type") != "build":
            continue
        try:
            log_to_html(apiurl, result)
        except (HTTPError, HTTPException) as exc:
            if getattr(exc, "code", 0) == 404:
                pass
            
            #raise self.retry(exc=exc, countdown=self.request.retries ** 2, max_retries=4)

    return workitem

@app.task(name='autodoc.tasks.deploy', bind=True, acks_late=True)
def deploy(self, workitem):
    conf.get_config()
    ns = workitem["payload"].get("namespace", None)
    if ns is None:
        ns = workitem["payload"]["ev"]["namespace"]

    apiurl = conf.config["apiurl_aliases"].get(ns, conf.config["apiurl"])
    project = workitem["payload"]["project"]
    u = core.makeurl(apiurl, ["source", project, '_attribute'])
    f = core.http_GET(u)
    root = ElementTree.parse(f).getroot()
    attributes = root.findall('.//attribute[@name="VeryImportantProject"]') 
    if not attributes:
        return workitem

    package = workitem["payload"]["package"]
    repository = workitem["payload"].get("repository")
    arch = workitem["payload"].get("arch")

    ext_rpm = [".rpm"]
    ext_other = [".txt", ".zip", ".ipk", ".manifest", ".sh", ".gz", ".swu"]
    binaries = core.get_binarylist(apiurl, project, repository, arch, package=package, verbose=False)
    print binaries
    binaries = filter(lambda x: (os.path.splitext(x)[1] in ext_rpm + ext_other and not x.endswith(".src.rpm")), binaries)
    print binaries
    if not binaries:
        return workitem

    #FIXME: get from settings
    docroot_dir="/data/autodoc/docs"
    binroot_dir="/data/autodoc/builds"
    doctmp_dir = tempfile.mkdtemp(dir=docroot_dir+"/.tmp")
    bintmp_dir = tempfile.mkdtemp(dir=binroot_dir+"/.tmp")
    tmp_deploydir = tempfile.mkdtemp()

    version = str(int(time.time()))
    release = "1"
    suffixes = ("results", "guide", "docs", "release", "version")
    filelist = []
    binlist = []

    for bina in binaries:
        if bina.endswith(".rpm"):
            tmp_filename = '%s/%s' % (doctmp_dir, bina)
            name, version, release, epoch, rarch = splitFilename(bina)
            if name.endswith(suffixes):
                print "getting binary %s %s %s %s %s %s %s" % (apiurl, project, repository, arch, bina, package, tmp_filename)
                core.get_binary_file(apiurl, project, repository, arch, bina, package=package, target_filename=tmp_filename, progress_meter=False)
                filelist.extend(extract_rpm(tmp_filename, tmp_deploydir))

    for bina in binaries:
        if os.path.splitext(bina)[1] in ext_other:
            tmp_filename = '%s/%s' % (bintmp_dir, bina)
            core.get_binary_file(apiurl, project, repository, arch, bina, package=package, target_filename=tmp_filename, progress_meter=False)
            binlist.append(tmp_filename)

    docdeploy_dir=os.path.join(docroot_dir, package, "%s-%s" % (version, release), "%s-%s" % (repository, arch), "")
    bindeploy_dir=os.path.join(binroot_dir, package, "%s-%s" % (version, release), "%s-%s" % (repository, arch), "")
    latestdoclink=os.path.join(docroot_dir, package, "latest")
    latestbinlink=os.path.join(binroot_dir, package, "latest")

    if filelist:
        if not os.path.isdir(docdeploy_dir):
            os.makedirs(docdeploy_dir)
    
        for dirpath, dirnames, filenames in os.walk(tmp_deploydir):
            if len(dirnames) > 1 or filenames:
                for item in itertools.chain(dirnames, filenames):
                    try:
                        shutil.move(os.path.join(dirpath, item), docdeploy_dir)
                    except shutil.Error, exc:
                        print exc
                break

    if binlist:
        if not os.path.isdir(bindeploy_dir):
            os.makedirs(bindeploy_dir)

        for bina in binlist:
            shutil.move(bina, bindeploy_dir)

    if filelist:
        try:
            os.symlink("%s-%s" % (version, release), latestdoclink + "-%s-%s" % (version, release))
            os.rename(latestdoclink + "-%s-%s" % (version, release), latestdoclink)
            with open(latestdoclink + ".txt", 'w') as f:
                f.write("%s-%s\n" % (version, release))
        except Exception as exc:
            print exc
            pass

    if binlist:
        try:
            os.symlink("%s-%s" % (version, release), latestbinlink + "-%s-%s" % (version, release))
            os.rename(latestbinlink + "-%s-%s" % (version, release), latestbinlink)
            with open(latestbinlink + ".txt", 'w') as f:
                f.write("%s-%s\n" % (version, release))
        except Exception as exc:
            print exc
            pass

    shutil.rmtree(doctmp_dir, True)
    shutil.rmtree(bintmp_dir, True)
    shutil.rmtree(tmp_deploydir, True)
    return workitem


@task_success.connect
def handle_task_success(sender=None, **kwargs):
    """Report task results back to workflow engine."""

    credentials = pika.PlainCredentials('celery', 'celery')
    parameters = pika.ConnectionParameters(host="127.0.0.1",credentials=credentials)
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
