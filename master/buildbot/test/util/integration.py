# This file is part of Buildbot.  Buildbot is free software: you can
# redistribute it and/or modify it under the terms of the GNU General Public
# License as published by the Free Software Foundation, version 2.
#
# This program is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE.  See the GNU General Public License for more
# details.
#
# You should have received a copy of the GNU General Public License along with
# this program; if not, write to the Free Software Foundation, Inc., 51
# Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#
# Copyright Buildbot Team Members

import mock
import os
import textwrap

from twisted.internet import defer
from twisted.internet import reactor
from twisted.trial import unittest

from buildbot.master import BuildMaster
from buildbot.status.results import statusToString
from buildbot.test.util import dirs
from buildbot.test.util import www
from buildslave.bot import BuildSlave


class RunMasterBase(dirs.DirsMixin, www.RequiresWwwMixin, unittest.TestCase):

    @defer.inlineCallbacks
    def setUp(self):
        self.basedir = os.path.abspath('basdir')
        self.setUpDirs(self.basedir)
        self.configfile = os.path.join(self.basedir, 'master.cfg')

        # We create a master.cfg, which loads the configuration from the
        # test module. Only the slave config is kept there, as it should not
        # be changed
        open(self.configfile, "w").write(textwrap.dedent("""
            from buildbot.buildslave import BuildSlave
            from %s import masterConfig
            c = BuildmasterConfig = masterConfig()
            c['slaves'] = [BuildSlave("local1", "localpw")]
            c['protocols'] = {"pb": {"port": "tcp:0:interface=127.0.0.1"}}
            """ % self.__class__.__module__))
        # create the master and set its config
        m = BuildMaster(self.basedir, self.configfile)
        self.master = m

        # update the DB
        yield m.db.setup(check_version=False)
        yield m.db.model.upgrade()

        # stub out m.db.setup since it was already called above
        m.db.setup = lambda: None

        # mock reactor.stop (which trial *really* doesn't
        # like test code to call!)
        mock_reactor = mock.Mock(spec=reactor)
        mock_reactor.callWhenRunning = reactor.callWhenRunning

        # start the service
        yield m.startService(_reactor=mock_reactor)
        self.failIf(mock_reactor.stop.called,
                    "startService tried to stop the reactor; check logs")

        # We find out the slave port automatically
        slavePort = m.pbmanager.dispatchers.values()[0].port.getHost().port

        # create a slave, and attach it to the master, it will be started, and stopped
        # along with the master
        s = BuildSlave("127.0.0.1", slavePort, "local1", "localpw", self.basedir, False, False)
        s.setServiceParent(m)

    @defer.inlineCallbacks
    def tearDown(self):
        m = self.master
        # stop the service
        yield m.stopService()

        # and shutdown the db threadpool, as is normally done at reactor stop
        m.db.pool.shutdown()

        # (trial will verify all reactor-based timers have been cleared, etc.)
        self.tearDownDirs()

    @defer.inlineCallbacks
    def doForceBuild(self, wantSteps=False, wantProperties=False,
                     wantLogs=False, useChange=False):

        # force a build, and wait until it is finished
        d = defer.Deferred()

        # in order to allow trigger based integration tests
        # we wait until the first started build is finished
        self.firstBuildId = None

        def newCallback(_, data):
            if self.firstBuildId is None:
                self.firstBuildId = data['buildid']
                newConsumer.stopConsuming()

        def finishedCallback(_, data):
            if self.firstBuildId == data['buildid']:
                d.callback(data)

        newConsumer = yield self.master.mq.startConsuming(
            newCallback,
            ('builds', None, 'new'))

        finishedConsumer = yield self.master.mq.startConsuming(
            finishedCallback,
            ('builds', None, 'finished'))

        if useChange is False:
            # use data api to force a build
            yield self.master.data.control("force", {}, ("forceschedulers", "force"))
        else:
            # use data api to force a build, via a new change
            yield self.master.data.updates.addChange(**useChange)

        # wait until we receive the build finished event
        build = yield d
        finishedConsumer.stopConsuming()
        yield self.enrichBuild(build, wantSteps, wantProperties, wantLogs)
        defer.returnValue(build)

    @defer.inlineCallbacks
    def enrichBuild(self, build, wantSteps=False, wantProperties=False, wantLogs=False):
        # enrich the build result, with the step results
        if wantSteps:
            build["steps"] = yield self.master.data.get(("builds", build['buildid'], "steps"))
            # enrich the step result, with the logs results
            if wantLogs:
                build["steps"] = list(build["steps"])
                for step in build["steps"]:
                    step['logs'] = yield self.master.data.get(("steps", step['stepid'], "logs"))
                    step["logs"] = list(step['logs'])
                    for log in step["logs"]:
                        log['contents'] = yield self.master.data.get(("logs", log['logid'], "contents"))

        if wantProperties:
            build["properties"] = yield self.master.data.get(("builds", build['buildid'], "properties"))

    @defer.inlineCallbacks
    def printBuild(self, build):
        # helper for debugging: print a build
        yield self.enrichBuild(build, wantSteps=True, wantProperties=True, wantLogs=True)
        print "*** BUILD %d *** ==> %s (%s)" % (build['buildid'], build['state_string'],
                                                statusToString(build['results']))
        for step in build['steps']:
            print "    *** STEP %s *** ==> %s (%s)" % (step['name'], step['state_string'],
                                                       statusToString(build['results']))
            for url in step['urls']:
                print "       url:%s (%s)" % (url['name'], url['url'])
            for log in step['logs']:
                print "        log:%s (%d)" % (log['name'], log['num_lines'])
