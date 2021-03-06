#!/usr/bin/python2.7
"""
Copyright (c) 2014, ICFLIX Media FZ LLC All rights reserved.
Use of this source code is governed by a BSD-style license that can be
found in the LICENSE file.

Desc: Execute % check_multi;, "encrypt" its STDOUT, HTTP POST it to the server
"""
import argparse
import hashlib
import logging
import logging.handlers
import os
import pyinotify
import Queue
import requests
import signal
from socket import getfqdn
import subprocess
import sys
import threading
import traceback

# Interval of sending results in sec
DEFAULT_INTERVAL = 60
DEFAULT_ENVIRONMENT = 'development'
# Path to check_multi script
CHECK_MULTI_BIN = '/usr/lib/nagios/plugins/check_multi'
# Directory with check_multi commands; no trailing '/'!
CHECK_MULTI_DIR = '/etc/check_multi'
# Log format
LOG_FORMAT = '%(asctime)s %(levelname)-10s %(threadName)-11s %(message)s'

# check_multi commands to execute in order to send config/results
CMD_GET_CONFIG = [
    CHECK_MULTI_BIN,
    '-f',
    CHECK_MULTI_DIR,
    '-s',
    'HOSTNAME="%s"' % (getfqdn()),
    '-r',
    '2048'
]
CMD_GET_RESULTS = [
    CHECK_MULTI_BIN,
    '-f',
    CHECK_MULTI_DIR,
    '-r',
    '256'
]

# MyPie {{{
class MyPie(pyinotify.ProcessEvent):
    """Class to Process Inotify Events."""

    def __init__(self, mqueue):
        pyinotify.ProcessEvent.__init__(self)
        self.mqueue = mqueue

    def _enqueue(self, event_desc, filename):
        """Enqueue events for MainThread."""
        try:
            self.mqueue.put((event_desc, filename))
        except Queue.Full:
            # We're not going to signal back any failures
            pass

    def process_IN_DELETE(self, event):
        """Deleted."""
        self._enqueue('deleted', os.path.join(event.path, event.name))

    def process_IN_CREATE(self, event):
        """Created."""
        self._enqueue('created', os.path.join(event.path, event.name))

    def process_IN_MODIFY(self, event):
        """Modified."""
        self._enqueue('modified', os.path.join(event.path, event.name))

    def process_IN_ATTRIB(self, event):
        """Attribute change."""
        self._enqueue('attr_change', os.path.join(event.path, event.name))

    def process_IN_MOVED_FROM(self, event):
        """Moved from somewhere."""
        self._enqueue('moved_from', os.path.join(event.path, event.name))

    def process_IN_CLOSE_WRITE(self, event):
        """Written to."""
        self._enqueue('written', os.path.join(event.path, event.name))

    def process_IN_MOVED_TO(self, event):
        """Moved to."""
        self._enqueue('moved_to', os.path.join(event.path, event.name))


# }}}
# NagiosRunit {{{
class NagiosRunit(object):
    """Nagios Client/Node executed from runit."""

    def __init__(self):
        self.inotifier = None
        self.mqueue = Queue.Queue(0)
        self._stop = threading.Event()
        #
        self.config_uri = None
        self.environment = DEFAULT_ENVIRONMENT
        self.interval = DEFAULT_INTERVAL
        self.https_verify = True
        self.results_uri = None
        self.shared_key = None
        self.url = None

    def _stop_inotifier(self):
        """Stop inotify thread."""
        try:
            self.inotifier.stop()
            self.inotifier.join(5)
        except Exception:
            pass

    def handler_signal(self, signum, frame):
        """Handle signals, resp. set event."""
        self._stop.set()

    def run(self):
        """Main."""
        logging.info('Starting up.')
        signal.signal(signal.SIGHUP, self.handler_signal)
        signal.signal(signal.SIGINT, self.handler_signal)
        signal.signal(signal.SIGTERM, self.handler_signal)
        vm = pyinotify.WatchManager()
        mask = pyinotify.ALL_EVENTS
        self.inotifier = pyinotify.ThreadedNotifier(vm, MyPie(self.mqueue))
        self.inotifier.start()
        vm.add_watch(CHECK_MULTI_DIR, mask, rec=False)
        while self._stop.isSet() is False:
            self.send_results()
            self.send_config()
            self._stop.wait(self.interval)

        self.stop()

    def send_results(self):
        """Send results to remote Nagios Host."""
        sender = NagiosSender()
        sender.set_command(CMD_GET_RESULTS)
        sender.set_https_verification(self.https_verify)
        sender.set_url('%s%s' % (self.url, self.results_uri))
        sender.set_shared_key(self.shared_key)
        try:
            sender.run()
        except Exception:
            logging.error(traceback.format_exc())
            self._stop.set()

        del sender

    def send_config(self):
        """Send updates done to CHECK_MULTI_DIR to remote Host."""
        if self.mqueue.qsize() < 1:
            return

        while True:
            try:
                (event_desc, fpath) = self.mqueue.get(1, True)
                logging.info('Event %s on file %s', event_desc, fpath)
            except Queue.Empty:
                break

        sender = NagiosSender()
        sender.set_command(CMD_GET_CONFIG)
        sender.set_https_verification(self.https_verify)
        sender.set_shared_key(self.shared_key)
        sender.set_url('%s%s' % (self.url, self.config_uri))
        try:
            sender.run()
        except Exception:
            logging.error(traceback.format_exc())
            self._stop.set()

        del sender

    def set_config_uri(self, config_uri):
        """Set URI for posting Config."""
        self.config_uri = config_uri

    def set_environment(self, environment):
        """Set working environment."""
        self.environment = environment

    def set_https_verification(self, verify):
        """Set HTTPS SSL verification."""
        if verify:
            self.https_verify = True
        else:
            self.https_verify = False

    def set_interval(self, interval):
        """Set interval for sending results."""
        self.interval = interval

    def set_results_uri(self, results_uri):
        """Set URI for posting Results."""
        self.results_uri = results_uri

    def set_shared_key(self, shared_key):
        """Set shared key for scrambling data."""
        self.shared_key = shared_key

    def set_url(self, url):
        """Set Nagios Host URL."""
        self.url = url

    def stop(self):
        """Stop everything."""
        self._stop.set()
        self._stop_inotifier()


# }}}
# NagiosSender {{{
class NagiosSender(object):
    """Nagios Client/Node class - accept STDIN, encrypt and HTTP POST it."""

    def __init__(self):
        self.command = None
        self.url = None
        self.shared_key = None
        self.http_timeout = 15
        self.https_verify = True

    def run(self):
        """Go, go, go!"""
        logging.debug('Command %s', ' '.join(self.command))
        stdin = self.run_command(self.command)
        if stdin is None:
            logging.error('Command %s has returned empty STDIN!',
                          ' '.join(self.command))
            return

        logging.debug('CMD: %s', self.command)
        logging.debug('Output: %s', repr(stdin))

        checksum = hashlib.sha256(stdin).hexdigest()
        data = 'CHECKSUM: %s\n' % (checksum)
        data += 'KEY: %s\n' % (hashlib.sha256(
            '%s%s' % (checksum, self.shared_key)).hexdigest())
        data += 'FQDN: %s\n' % (getfqdn())
        data += '---\n'
        data += stdin
        headers = {'content-type': 'text/plain'}
        rsp = requests.post(self.url, data=data, headers=headers,
                            timeout=self.http_timeout, verify=self.https_verify)

        try:
            status_code = int(rsp.status_code)
        except Exception:
            status_code = 0

        logging.debug('Status code is %i', status_code)
        if status_code != 200:
            logging.error('HTTP Code is %i, expected 200', status_code)

        logging.debug('HTTP Raw Response: %s', rsp.raw.read())
        rsp.close()
        del rsp

    def run_command(self, cmd):
        """Run given command and return its STDOUT."""
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE)
        except Exception as exception:
            logging.error('Failed to execute command: %s', exception)
            return None

        (stdout_lines, stderr_lines) = proc.communicate()
        if stderr_lines is not None:
            logging.error('Command has returned some errors.')
            return None

        if stdout_lines is None or len(stdout_lines) < 1:
            logging.error('Command has returned no lines.')
            return None

        return stdout_lines

    def set_command(self, command):
        """Set command to execute - list is expected."""
        self.command = command

    def set_https_verification(self, verify):
        """Set HTTPS SSL verification."""
        if verify:
            self.https_verify = True
        else:
            self.https_verify = False

    def set_shared_key(self, shared_key):
        """Set shared key for scrambling message."""
        self.shared_key = shared_key

    def set_url(self, url):
        """Set URL we're going to POST to."""
        self.url = url


# }}}
def get_actions():
    """Return list of available actions."""
    return ['runit', 'send_config', 'send_results']

def get_environments():
    """Return list of supported environments."""
    return ['production', 'staging', 'development']

def main():
    """Main function - setup logging and launch instance of NagiosSender."""
    logging.basicConfig(format=LOG_FORMAT, stream=sys.stdout)
    logging.getLogger().setLevel(logging.INFO)
    args = parse_cli_args()
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Yes, we want to crash loud if some of those aren't provided
    shared_key = os.environ['NAGIOS_SHARED_KEY']
    nagios_host = os.environ['NAGIOS_HOST']
    config_uri = os.environ['NAGIOS_CONFIG_URI']
    results_uri = os.environ['NAGIOS_RESULTS_URI']
    logging.debug('Environment: %s', args.environment)
    logging.debug('RHost: %s', nagios_host)
    logging.debug('Shared Key: %s', shared_key)
    logging.debug('Config URI: %s', config_uri)
    logging.debug('Results URI: %s', results_uri)

    if args.action == 'send_config':
        nagios_sender = NagiosSender()
        nagios_sender.set_command(CMD_GET_CONFIG)
        nagios_sender.set_https_verification(args.ssl_verify)
        nagios_sender.set_shared_key(shared_key)
        nagios_sender.set_url('%s%s' % (nagios_host, config_uri))
        nagios_sender.run()
    elif args.action == 'send_results':
        nagios_sender = NagiosSender()
        nagios_sender.set_command(CMD_GET_RESULTS)
        nagios_sender.set_https_verification(args.ssl_verify)
        nagios_sender.set_url('%s%s' % (nagios_host, results_uri))
        nagios_sender.set_shared_key(shared_key)
        nagios_sender.run()
    elif args.action == 'runit':
        nagios_runit = NagiosRunit()
        nagios_runit.set_config_uri(config_uri)
        nagios_runit.set_environment(args.environment)
        nagios_runit.set_https_verification(args.ssl_verify)
        nagios_runit.set_interval(args.interval)
        nagios_runit.set_results_uri(results_uri)
        nagios_runit.set_shared_key(shared_key)
        nagios_runit.set_url(nagios_host)
        nagios_runit.run()

    logging.shutdown()

def parse_cli_args():
    """Return parsed CLI args."""
    parser = argparse.ArgumentParser()
    parser.add_argument('-a',
                        dest='action', type=str, choices=get_actions(),
                        help='Action to do.')
    parser.add_argument('-e',
                        dest='environment', type=str,
                        choices=get_environments(), default=DEFAULT_ENVIRONMENT,
                        help='Set environment.')
    parser.add_argument('-i',
                        dest='interval', type=int, default=DEFAULT_INTERVAL,
                        help='How often to send check results.')
    parser.add_argument('--no-check-certificate',
                        dest='ssl_verify', action='store_false', default=True,
                        help="Don't check SSL certificate.")
    parser.add_argument('-v',
                        dest='verbose', action='store_true',
                        help='Increase logging verbosity.')
    return parser.parse_args()

if __name__ == '__main__':
    main()
