# Beah - Test harness. Part of Beaker project.
#
# Copyright (C) 2009 Marian Csontos <mcsontos@redhat.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 59 Temple Place, Suite 330, Boston, MA  02111-1307  USA

from twisted.web import xmlrpc, server
from twisted.internet import reactor, protocol, stdio
from twisted.protocols import basic
import simplejson as json
import os
import os.path
import tempfile
import exceptions
import uuid
from beah.core import event
from beah.wires.internals.twmisc import (serveAnyChild, serveAnyRequest,
        JSONProtocol)
from beah.core.constants import RC

# FIXME: change log level to WARNING, use tempfile and upload log when process
# ends.
import logging
if not os.path.isdir('/tmp/var/log'):
    if not os.path.isdir('/tmp/var'):
        os.mkdir('/tmp/var')
    os.mkdir('/tmp/var/log')
logging.basicConfig(filename='/tmp/var/log/rhts_task.log', level=logging.DEBUG)

USE_DEFAULT = object()
TEST_ENV = dict(
        JOBID='321',
        RECIPESETID='4321',
        RECIPEID='54321',
        RECIPETESTID='654321',
        TESTID='321321',
        OUTPUTFILE=tempfile.mkstemp()[1],
        AVC_ERROR=tempfile.mkstemp()[1])

################################################################################
# AUXILIARY:
################################################################################
__RESULT_RHTS_TO_BEAH = {
        "pass": RC.PASS,
        "warn": RC.WARNING,
        "fail": RC.FAIL,
        "crit": RC.CRITICAL,
        "fata": RC.FATAL,
        "pani": RC.FATAL,
        }
def result_rhts_to_beah(result):
    """Translate result codes from RHTS to beah."""
    return __RESULT_RHTS_TO_BEAH.get(result.lower()[:4], RC.WARNING)

################################################################################
# CONTROLLER LINK:
################################################################################
class ControllerLink(JSONProtocol):

    from os import linesep as delimiter

    def __init__(self, main):
        self.main = main

    def connectionMade(self):
        self.main.controller_connected()

    def proc_input(self, obj):
        self.main.controller_input(obj)

    def connectionLost(self, reason):
        self.main.controller_disconnected(reason)


################################################################################
# PROCESS:
################################################################################
class RHTSTask(protocol.ProcessProtocol):

    def __init__(self, main):
        self.main = main

    def outReceived(self, data):
        self.main.task_stdout(data)

    def errReceived(self, data):
        self.main.task_stderr(data)

    def processExited(self, reason):
        self.main.task_exited(reason)

    def processEnded(self, reason):
        self.main.task_ended(reason)


################################################################################
# XML-RPC HANDLERS:
################################################################################
class RHTSResults(xmlrpc.XMLRPC):

    def __init__(self, main):
        self.main = main

    def xmlrpc_result(self, test_name, parent_string, result, result_value,
            test_version, recipe_test_id):
        logging.debug("XMLRPC: results.result(%r, %r, %r, %r, %r, %r)",
                test_name, parent_string, result, result_value, test_version,
                recipe_test_id)
        evt = event.result_ex(
                result_rhts_to_beah(result),
                handle=test_name,
                message='(%s)' % result,
                statistics={'score':result_value},
                test_version=test_version,
                recipe_test_id=recipe_test_id)
        self.main.send_evt(evt)
        return evt.id()
    xmlrpc_result.signature = [
            ['string', 'string', 'string', 'string', 'string', 'string', 'int'],
            ]

    def get_file(self, pretty_name, size=None, digest=None):
        file_id = self.main.get_file(pretty_name)
        if file_id is None:
            evt = event.file(name=pretty_name)
            file_id = evt.id()
            self.main.set_file(file_id, pretty_name)
            self.main.send_evt(evt)
            evt = event.file_meta(file_id, size=size, digest=("md5", digest),
                    codec="base64")
        return file_id

    def xmlrpc_uploadFile(self, recipe_test_id, name, size, digest, offset,
            data):
        logging.debug("XMLRPC: results.uploadFile(%r, %r, %r, %r, %r, %r)",
                recipe_test_id, name, size, digest, offset, data)
        file_id = self.get_file(name, size=size, digest=digest)
        if file_id is None:
            msg = "%s:xmlrpc_uploadFile: " % self.__class__.__name__ + \
                    "Can not create file '%s'." % name
            self.main.error(msg)
            return msg
        if offset < 0:
            evt = event.file_close(file_id)
            self.main.send_evt(evt)
            return 0
        if data:
            evt = event.file_write(file_id, data, offset=offset)
            self.main.send_evt(evt)
        return 0 # or "Failure reason"
    xmlrpc_uploadFile.signature = [
            ['int', 'int', 'string', 'int', 'int', 'int', 'string'],
            ]

    def xmlrpc_resultLog(self, log_type, result_id, pretty_name):
        logging.debug("XMLRPC: results.resultLog(%r, %r, %r)", log_type,
            result_id, pretty_name)
        file_id = self.main.get_file(pretty_name)
        if file_id is None:
            msg = "%s:xmlrpc_resultLog: " % self.__class__.__name__ + \
                    "File '%s' does not exist!" % pretty_name + \
                    " uploadFile is required before resultLog can be used."
            self.main.error(msg)
            return msg
        # FIXME! implement this!!!
        #evt = event.file_meta(file_id, log_type)
        #self.main.send_evt(evt)
        return 0 # or "Failure reason"
    xmlrpc_resultLog.signature = [
            ['int', 'string', 'int', 'string'],
            ]

    def xmlrpc_recipeTestRpms(self, test_id, pkg_list):
        logging.debug("XMLRPC: results.recipeTestRpms(%r, %r)",
                test_id, pkg_list)
        # FIXME! implement this!!!
        return 0 # or "Failure reason"
    xmlrpc_recipeTestRpms.signature = [
            ['int', 'int', 'string', 'int', 'int', 'int', 'string'],
            ]


class RHTSWatchdog(xmlrpc.XMLRPC):

    def __init__(self, main):
        self.main = main

    def xmlrpc_abortJob(self, job_id):
        logging.debug("XMLRPC: watchdog.abortJob(%r)", job_id)
        # FIXME: implement this
        return 0 # or "Failure reason"
    xmlrpc_abortJob.signature = [['int', 'int']]

    def xmlrpc_abortRecipeSet(self, recipe_set_id):
        logging.debug("XMLRPC: watchdog.abortRecipeSet(%r)", recipe_set_id)
        # FIXME: implement this
        return 0 # or "Failure reason"
    xmlrpc_abortRecipeSet.signature = [['int', 'int']]

    def xmlrpc_abortRecipe(self, recipe_id):
        logging.debug("XMLRPC: watchdog.abortRecipe(%r)", recipe_id)
        # FIXME: implement this
        return 0 # or "Failure reason"
    xmlrpc_abortRecipe.signature = [['int', 'int']]

    def xmlrpc_testCheckin(self, hostname, job_id, test, kill_time, test_id):
        logging.debug("XMLRPC: watchdog.testCheckin(%r, %r, %r, %r, %r)", hostname, job_id, test, kill_time, test_id)
        # FIXME: implement this
        return 1 # or "Failure reason"
    xmlrpc_testCheckin.signature = [['int', 'string', 'int', 'string', 'int', 'int']]



class RHTSWorkflows(xmlrpc.XMLRPC):

    def __init__(self, main):
        self.main = main

    def xmlrpc_add_comment_to_recipe(self, submitter, recipe_id, comment):
        logging.debug("XMLRPC: workflows.add_comment_to_recipe(%r, %r, %r)",
                submitter, recipe_id, comment)
        # FIXME: implement this...
        return 0 # or "Failure reason"


class RHTSTest(xmlrpc.XMLRPC):

    def __init__(self, main):
        self.main = main

    def xmlrpc_testCheckin(self, test_id, call_type):
        logging.debug("XMLRPC: test.testCheckin(%r, %r)", test_id, call_type)
        # FIXME: implement this!!!
        return 0 # or "Failure reason"
    xmlrpc_testCheckin.signature = [['int', 'int', 'string']]


class RHTSSync(xmlrpc.XMLRPC):

    def __init__(self, main):
        self.main = main

    def xmlrpc_set(self, recipe_set_id, test_order, result_server, hostname,
            state):
        logging.debug("XMLRPC: sync.set(%r, %r, %r, %r, %r)", recipe_set_id,
                test_order, result_server, hostname, state)
        # FIXME: implement this!!!
        # Requires async communication with LC - should return Deferred object
        return 0 # or "Failure reason"
    xmlrpc_set.signature = [['int', 'int', 'int', 'string', 'string', 'string']]

    def xmlrpc_block(self, recipe_set_id, test_order, result_server, states,
            hostnames):
        logging.debug("XMLRPC: sync.block(%r, %r, %r, %r, %r)", recipe_set_id,
                test_order, result_server, states, hostnames)
        answ = []
        # FIXME: implement this!!!
        for name in hostnames:
            # Requires async communication with LC - should return Deferred
            # object
            answ.append(states[0])
        return answ
    xmlrpc_block.signature = [['list', 'int', 'int', 'string', 'list', 'list']]


class RHTSHandler(xmlrpc.XMLRPC):

    """A root XML-RPC handler.

    It does handle only unhandled calls. Other calls should be handled by
    subhandlers."""

    def __init__(self, main, *args, **kwargs):
        self.main = main
        xmlrpc.XMLRPC.__init__(self, *args, **kwargs)
        self.putSubHandler('sync', RHTSSync(main))
        self.putSubHandler('workflows', RHTSWorkflows(main))
        self.putSubHandler('watchdog', RHTSWatchdog(main))
        self.putSubHandler('results', RHTSResults(main))
        self.putSubHandler('test', RHTSTest(main))
        xmlrpc.addIntrospection(self)

    def catch_xmlrpc(self, method, *args):
        """Handler for unhandled requests."""
        logging.error("ERROR: Missing method: %s%r", method, args)
        #raise xmlrpc.Fault(123, "Undefined procedure %s." % method)
        self.main.send_evt(event.output(("ERROR: UNHANDLED RPC" ,method, args),
            out_handle='xmlrpc'))
        return "Error: Server can not handle command %s" % method

serveAnyChild(RHTSHandler)
serveAnyRequest(RHTSHandler, 'catch_xmlrpc', xmlrpc.XMLRPC)

class RHTSServer(server.Site):

    def __init__(self, main, logPath=None, timeout=60 * 60 * 12):
        self.main = main
        self.handler = RHTSHandler(main)
        server.Site.__init__(self, self.handler, logPath=logPath, timeout=timeout)

    def startFactory(self):
        server.Site.startFactory(self)
        # FIXME: waiting for server would be better:
        # - e.g. send a request and wait until it is served...
        self.main.server_started()


################################################################################
# MAIN:
################################################################################
class RHTSMain(object):

    def __init__(self, task_path, env):
        self.controller = ControllerLink(self)
        self.task = RHTSTask(self)
        self.server = RHTSServer(self)
        self.process = None
        self.listener = None
        self.task_path = task_path
        self.__done = False
        self.__files = {}

        # FIXME: is return value of any use?
        stdio.StandardIO(self.controller)

        # FIXME: randomize port(?) - use configurable range of ports.
        port = 7080

        # FIXME: is inheriting the whole environment desirable?
        self.env = dict(env if env is not USE_DEFAULT else os.environ)

        # FIXME: Any other env.variables to set?
        # FIXME: What values should be used here?
        # - some values could be received from LC when task is scheduled, but
        #   it would create a dependency!
        #   - let's use fake values, and let the Backend translate it (if
        #     supported)
        #     - e.g. JOBID, RECIPESETID, RECIPEID are not interesting at all
        #     - use task_id for RECIPESETID, and BE (or LC eventually) should
        #       be able to find about the rest...
        self.env.update(
                # RESULT_SERVER - host:port[/prefixpath]
                RESULT_SERVER="%s:%s%s" % ("localhost", port, ""),
                TESTORDER='123', # FIXME: What should go here???
                )

        # FIXME: should any checks go here?
        # e.g. does Makefile PURPOSE exist? try running `make testinfo.desc`? ...

        # FIXME: is return value of any use?
        reactor.listenTCP(port, self.server, interface='localhost')

    def on_exit(self):
        # FIXME! handling!
        # should submit captured files (AVC_ERROR, OUTPUTFILE)
        logging.info("quitting...")
        reactor.callLater(2, reactor.stop)
        self.__done = True

    def __controller_output(self, data):
        self.controller.sendLine(data)

    def send_evt(self, evt):
        logging.debug("sending evt: %r", evt)
        self.__controller_output(json.dumps(evt))

    TEST_RUNNER = 'rhts-test-runner.sh'
    def server_started(self):
        # FIXME: Install rhts-test-runner.sh somewhere!
        self.process = reactor.callLater(2, reactor.spawnProcess, self.task,
                self.TEST_RUNNER,
                args=[self.TEST_RUNNER],
                env=self.env, path=self.env.get('TESTPATH','/tmp'))


    def controller_input(self, cmd):
        # FIXME: process commands on input
        # - allowed commands: sync-set, sync-block, kill
        # - anything else?
        pass

    def controller_connected(self):
        pass

    def controller_disconnected(self, reason):
        if not self.__done:
            logging.error("Connection to controller was lost! reason=%s", reason)
            self.on_exit()


    def task_stdout(self, data):
        # FIXME: RHTS Task can send an event! Handle it!
        self.send_evt(event.stdout(data))

    def task_stderr(self, data):
        # FIXME: RHTS Task can send an event! Handle it!
        self.send_evt(event.stderr(data))

    def task_exited(self, reason):
        if not self.__done:
            logging.info("task_exited(%s)", reason)
            self.send_evt(event.linfo("task_exited", reason=str(reason)))
            self.on_exit()

    def task_ended(self, reason):
        if not self.__done:
            logging.info("task_ended(%s)", reason)
            self.send_evt(event.linfo("task_ended", reason=str(reason)))
            self.on_exit()

    def set_file(self, id, name):
        if id is None or name is None:
            return False
        tmp_id = self.get_file(name)
        if tmp_id is not None:
            if tmp_id != id:
                msg = "File '%s' already exists!" % name
                self.error(msg)
            return False
        self.__files[name] = id
        return id

    def get_file(self, name):
        return self.__files.get(name, None)

    def error(msg):
        logging.error(msg)
        evt = event.error(message=msg)
        self.send_evt(evt)


def main(task_path=None):
    from sys import argv
    if task_path is None:
        if len(argv) > 1:
            task_path = argv[1]
        #else:
        #    logging.error("Test directory not provided.", reason)
        #    raise exceptions.RuntimeError("Test directory not provided.")
    RHTSMain(task_path, USE_DEFAULT)
    reactor.run()


################################################################################
# MAIN:
################################################################################
if __name__ == '__main__':
    main()

