from constants import *
from pritunl import app_server, openssl_lock
from config import Config
from organization import Organization
from event import Event
from log_entry import LogEntry
import uuid
import os
import signal
import time
import subprocess
import threading
import logging
import traceback
import utils
import re

logger = logging.getLogger(APP_NAME)
_threads = {}
_events = {}
_output = {}
_process = {}
_start_time = {}

class Server(Config):
    str_options = {'name', 'network', 'interface', 'protocol',
        'local_networks', 'public_address', 'primary_organization',
        'primary_user', 'organizations', 'local_network'}
    bool_options = {'otp_auth', 'lzo_compression', 'debug'}
    int_options = {'port'}
    list_options = {'organizations', 'local_networks'}

    def __init__(self, id=None, name=None, network=None, interface=None,
            port=None, protocol=None, local_networks=None, public_address=None,
            otp_auth=None, lzo_compression=None, debug=None, organizations=[]):
        Config.__init__(self)
        self._cur_event = None
        self._last_event = 0

        if id is None:
            self._initialized = False
            self.id = uuid.uuid4().hex
            self.name = name
            self.network = network
            self.interface = interface
            self.port = port
            self.protocol = protocol
            self.local_networks = local_networks
            self.public_address = public_address
            self.otp_auth = otp_auth
            self.lzo_compression = lzo_compression
            self.debug = debug
            self.organizations = organizations
        else:
            self._initialized = True
            self.id = id

        self.path = os.path.join(app_server.data_path, SERVERS_DIR, self.id)
        self.ovpn_conf_path = os.path.join(self.path, TEMP_DIR, OVPN_CONF_NAME)
        self.dh_param_path = os.path.join(self.path, DH_PARAM_NAME)
        self.ifc_pool_path = os.path.join(self.path, IFC_POOL_NAME)
        self.ca_cert_path = os.path.join(self.path, TEMP_DIR, OVPN_CA_NAME)
        self.tls_verify_path = os.path.join(self.path, TEMP_DIR,
            TLS_VERIFY_NAME)
        self.user_pass_verify_path = os.path.join(self.path, TEMP_DIR,
            USER_PASS_VERIFY_NAME)
        self.ovpn_status_path = os.path.join(self.path, TEMP_DIR,
            OVPN_STATUS_NAME)
        self.set_path(os.path.join(self.path, 'server.conf'))

        if not self._initialized:
            self._initialize()

    def __getattr__(self, name):
        if name == 'status':
            if self.id in _threads:
                return _threads[self.id].is_alive()
            return False
        elif name == 'uptime':
            if self.status and self.id in _start_time:
                return int(time.time()) - _start_time[self.id]
            return None
        elif name == 'user_count':
            return self._get_user_count()
        elif name == 'org_count':
            return self._get_org_count()

        return Config.__getattr__(self, name)

    def _upgrade_0_10_5(self):
        if self.local_network:
            logger.debug('Upgrading server to v0.10.5... %r' % {
                'server_id': self.id,
            })
            self.local_networks = [self.local_network]
            self.local_network = None
            self.commit()

    def _initialize(self):
        logger.info('Initialize new server. %r' % {
            'server_id': self.id,
        })
        os.makedirs(os.path.join(self.path, TEMP_DIR))
        try:
            self._generate_dh_param()
            self.commit()
            LogEntry(message='Created new server "%s".' % self.name)
        except:
            logger.exception('Failed to create server. %r' % {
                'server_id': self.id,
            })
            utils.rmtree(self.path)
            raise

    def _event_delay(self, type, resource_id=None):
        # Min event every 1s max event every 0.2s
        event_time = time.time()
        if event_time - self._last_event >= 1:
            self._last_event = event_time
            self._cur_event = uuid.uuid4()
            Event(type=type, resource_id=resource_id)
            return

        def _target():
            event_id = uuid.uuid4()
            self._cur_event = event_id
            time.sleep(0.2)
            if self._cur_event == event_id:
                self._last_event = time.time()
                Event(type=type, resource_id=resource_id)
        threading.Thread(target=_target).start()

    def remove(self):
        logger.info('Removing server. %r' % {
            'server_id': self.id,
        })
        name = self.name

        if self.status:
            self.stop()
            for i in xrange(20):
                if not self.status:
                    break
                time.sleep(0.1)
            if self.status:
                self.force_stop()
                time.sleep(0.5)

        self._remove_primary_user()
        utils.rmtree(self.path)
        LogEntry(message='Deleted server "%s".' % name)
        Event(type=SERVERS_UPDATED)

    def commit(self):
        Config.commit(self)
        Event(type=SERVERS_UPDATED)

    def _create_primary_user(self):
        if not self.get_orgs():
            raise ValueError('Primary user cannot be created without ' + \
                'any organizations')
        logger.debug('Creating primary user. %r' % {
            'server_id': self.id,
        })
        org = self.get_orgs()[0]
        self.primary_organization = org.id
        user = org.new_user(CERT_SERVER, SERVER_USER_PREFIX + self.id)
        self.primary_user = user.id
        try:
            self.commit()
        except:
            logger.exception('Failed to commit server conf ' + \
                'on primary user creation, removing user. %r' % {
                    'server_id': self.id,
                    'user_id': user.id,
                })
            user.remove()
            raise

    def add_org(self, org_id):
        logger.debug('Adding organization to server. %r' % {
            'server_id': self.id,
            'org_id': org_id,
        })
        org = Organization(org_id)
        if org.id in self.organizations:
            logger.debug('Organization already on server, skipping. %r' % {
                'server_id': self.id,
                'org_id': org.id,
            })
            return org
        self.organizations.append(org.id)
        self.commit()
        Event(type=SERVERS_UPDATED)
        Event(type=SERVER_ORGS_UPDATED, resource_id=self.id)
        return org

    def _remove_primary_user(self):
        logger.debug('Removing primary user. %r' % {
            'server_id': self.id,
        })
        primary_organization = self.primary_organization
        primary_user = self.primary_user
        self.primary_organization = None
        self.primary_user = None

        if not primary_organization or not primary_user:
            return

        org = Organization(primary_organization)
        user = org.get_user(primary_user)
        if not user:
            logger.debug('Primary user not found, skipping remove. %r' % {
                'server_id': self.id,
                'org_id': org.id,
                'user_id': user.id,
            })
            return

        if user:
            user.remove()

    def remove_org(self, org_id):
        if org_id not in self.organizations:
            return
        logger.debug('Removing organization from server. %r' % {
            'server_id': self.id,
            'org_id': org_id,
        })
        if self.primary_organization == org_id:
            self._remove_primary_user()
        self.organizations.remove(org_id)
        self.commit()
        Event(type=SERVERS_UPDATED)
        Event(type=SERVER_ORGS_UPDATED, resource_id=self.id)

    def get_orgs(self):
        orgs = []
        for org_id in self.organizations:
            org = Organization(org_id)
            if not os.path.isfile(org.ca_cert.cert_path):
                logger.warning('Removing non existent organization ' + \
                    'from server. %r' % {
                        'server_id': self.id,
                        'org_id': org_id,
                    })
                self.remove_org(org_id)
                continue
            orgs.append(org)
        return orgs

    def _get_user_count(self):
        server_orgs = self.get_orgs()
        users_count = 0
        for org in server_orgs:
            for user in org.get_users():
                if user.type != CERT_CLIENT:
                    continue
                users_count += 1
        return users_count

    def _get_org_count(self):
        return len(self.get_orgs())

    def _generate_dh_param(self):
        logger.debug('Generating server dh params. %r' % {
            'server_id': self.id,
        })
        args = [
            'openssl', 'dhparam',
            '-out', self.dh_param_path,
            str(app_server.dh_param_bits)
        ]
        subprocess.check_call(args, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE)

    def _parse_network(self, network):
        network_split = network.split('/')
        address = network_split[0]
        cidr = int(network_split[1])
        subnet = ('255.' * (cidr / 8)) + str(
            int(('1' * (cidr % 8)).ljust(8, '0'), 2))
        subnet += '.0' * (3 - subnet.count('.'))
        return (address, subnet)

    def generate_ca_cert(self):
        logger.debug('Generating server ca cert. %r' % {
            'server_id': self.id,
        })
        with open(self.ca_cert_path, 'w') as server_ca_cert:
            for org in self.get_orgs():
                ca_path = org.ca_cert.cert_path
                with open(ca_path, 'r') as org_ca_cert:
                    server_ca_cert.write(org_ca_cert.read())

    def _generate_tls_verify(self):
        logger.debug('Generating tls verify script. %r' % {
            'server_id': self.id,
        })
        with open(self.tls_verify_path, 'w') as tls_verify_file:
            os.chmod(self.tls_verify_path, 0755)
            data_path = app_server.data_path
            tls_verify_file.write(TLS_VERIFY_SCRIPT % (
                data_path,
                ORGS_DIR,
                AUTH_LOG_NAME,
                INDEX_NAME,
            ))

    def _generate_user_pass_verify(self):
        logger.debug('Generating user pass verify script. %r' % {
            'server_id': self.id,
        })
        with open(self.user_pass_verify_path, 'w') as user_pass_verify_file:
            os.chmod(self.user_pass_verify_path, 0755)
            data_path = app_server.data_path
            user_pass_verify_file.write(USER_PASS_VERIFY_SCRIPT % (
                data_path,
                ORGS_DIR,
                USERS_DIR,
                TEMP_DIR,
                AUTH_LOG_NAME,
                OTP_JSON_NAME,
            ))

    def _generate_ovpn_conf(self, inline=False):
        if not self.get_orgs():
            raise ValueError('Ovpn conf cannot be generated without ' + \
                'any organizations')

        logger.debug('Generating server ovpn conf. %r' % {
            'server_id': self.id,
        })

        if not self.primary_organization or not self.primary_user:
            self._create_primary_user()

        if not os.path.isfile(self.dh_param_path):
            self._generate_dh_param()

        primary_org = Organization(self.primary_organization)
        primary_user = primary_org.get_user(self.primary_user)

        self.generate_ca_cert()
        self._generate_tls_verify()
        self._generate_user_pass_verify()

        if self.local_networks:
            push = ''
            for network in self.local_networks:
                push += 'push "route %s %s"\n' % self._parse_network(network)
            push = push.rstrip()
        else:
            push = 'push "redirect-gateway"'

        if not inline:
            server_conf = OVPN_SERVER_CONF % (
                self.port,
                self.protocol,
                self.interface,
                self.ca_cert_path,
                primary_user.cert_path,
                primary_user.key_path,
                self.tls_verify_path,
                self.dh_param_path,
                '%s %s' % self._parse_network(self.network),
                self.ifc_pool_path,
                push,
                self.ovpn_status_path,
                4 if self.debug else 1,
                8 if self.debug else 3,
            )
        else:
            server_conf = OVPN_INLINE_SERVER_CONF % (
                self.port,
                self.protocol,
                self.interface,
                self.tls_verify_path,
                '%s %s' % self._parse_network(self.network),
                self.ifc_pool_path,
                push,
                self.ovpn_status_path,
                4 if self.debug else 1,
                8 if self.debug else 3,
            )

        if self.otp_auth:
            server_conf += 'auth-user-pass-verify %s via-file\n' % (
                self.user_pass_verify_path)

        if self.lzo_compression:
            server_conf += 'comp-lzo\npush "comp-lzo"\n'

        if self.local_networks:
            server_conf += 'client-to-client\n'

        if inline:
            server_conf += '<ca>\n%s\n</ca>\n' % utils.get_cert_block(
                self.ca_cert_path)
            server_conf += '<cert>\n%s\n</cert>\n' % utils.get_cert_block(
                primary_user.cert_path)
            server_conf += '<key>\n%s\n</key>\n' % open(
                primary_user.key_path).read().strip()
            server_conf += '<dh>\n%s\n</dh>\n' % open(
                self.dh_param_path).read().strip()

        with open(self.ovpn_conf_path, 'w') as ovpn_conf:
            ovpn_conf.write(server_conf)

    def _enable_ip_forwarding(self):
        try:
            subprocess.check_call(['sysctl', '-w', 'net.ipv4.ip_forward=1'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except subprocess.CalledProcessError:
            logger.exception('Failed to enable IP forwarding. %r' % {
                'server_id': self.id,
            })
            raise

    def _generate_iptable_rules(self):
        iptable_rules = []

        try:
            routes_output = utils.check_output(['route', '-n'],
                stderr=subprocess.PIPE)
        except subprocess.CalledProcessError:
            logger.exception('Failed to get IP routes. %r' % {
                'server_id': self.id,
            })
            raise

        routes = {}
        for line in routes_output.splitlines():
            line_split = line.split()
            if len(line_split) < 8 or not re.match(IP_REGEX, line_split[0]):
                continue
            routes[line_split[0]] = line_split[7]

        if '0.0.0.0' not in routes:
            logger.error('Failed to find default network interface. %r' % {
                'server_id': self.id,
            })
            raise ValueError('Failed to find default network interface')
        default_interface = routes['0.0.0.0']

        for network_address in self.local_networks or ['0.0.0.0/0']:
            args = []
            network = self._parse_network(network_address)[0]

            if network not in routes:
                logger.debug('Failed to find interface for local network ' + \
                        'route, using default route. %r' % {
                    'server_id': self.id,
                })
                interface = default_interface
            else:
                interface = routes[network]

            if network != '0.0.0.0':
                args += ['-d', network_address]

            args += ['-s', self.network, '-o', interface, '-j', 'MASQUERADE']
            iptable_rules.append(args)

        return iptable_rules

    def _exists_iptable_rules(self):
        logger.debug('Checking for iptable rules. %r' % {
            'server_id': self.id,
        })
        for iptable_rule in self._generate_iptable_rules():
            try:
                subprocess.check_call(['iptables', '-t', 'nat', '-C',
                    'POSTROUTING'] + iptable_rule,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except subprocess.CalledProcessError:
                return False
        return True

    def _set_iptable_rules(self):
        if self._exists_iptable_rules():
            return

        logger.debug('Setting iptable rules. %r' % {
            'server_id': self.id,
        })
        for iptable_rule in self._generate_iptable_rules():
            try:
                subprocess.check_call(['iptables', '-t', 'nat', '-A',
                    'POSTROUTING'] + iptable_rule,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except subprocess.CalledProcessError:
                logger.exception('Failed to apply iptables ' + \
                    'routing rules. %r' % {
                        'server_id': self.id,
                    })
                raise

    def _clear_iptable_rules(self):
        if not self._exists_iptable_rules():
            return
        logger.debug('Clearing iptable rules. %r' % {
            'server_id': self.id,
        })

        for iptable_rule in self._generate_iptable_rules():
            try:
                subprocess.check_call(['iptables', '-t', 'nat', '-D',
                    'POSTROUTING'] + iptable_rule,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            except subprocess.CalledProcessError:
                logger.exception('Failed to clear iptables ' + \
                    'routing rules. %r' % {
                        'server_id': self.id,
                    })
                raise

    def _status_thread(self):
        i = 0
        cur_client_count = 0
        while not self._interrupt:
            # Check interrupt every 0.1s check client count every 1s
            if i == 9:
                i = 0
                client_count = len(self.get_clients())
                if client_count != cur_client_count:
                    cur_client_count = client_count
                    Event(type=USERS_UPDATED)
                    Event(type=SERVERS_UPDATED)
            else:
                i += 1
            time.sleep(0.1)
        self._clear_iptable_rules()
        _events[self.id].set()
        _events.pop(self.id, None)

    def _run(self):
        logger.debug('Starting ovpn process. %r' % {
            'server_id': self.id,
        })
        self._interrupt = False
        try:
            threading.Thread(target=self._status_thread).start()

            try:
                process = subprocess.Popen(['openvpn', self.ovpn_conf_path],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                _process[self.id] = process
                _events[self.id].set()
            except OSError:
                _output[self.id] += traceback.format_exc()
                self._event_delay(type=SERVER_OUTPUT_UPDATED,
                    resource_id=self.id)
                logger.exception('Failed to start ovpn process. %r' % {
                    'server_id': self.id,
                })
                return

            while True:
                line = process.stdout.readline()
                if line == '' and process.poll() is not None:
                    break
                _output[self.id] += line
                self._event_delay(type=SERVER_OUTPUT_UPDATED,
                    resource_id=self.id)

            logger.debug('Ovpn process has ended. %r' % {
                'server_id': self.id,
            })
        finally:
            _threads.pop(self.id, None)
            _process.pop(self.id, None)
            _start_time.pop(self.id, None)
            self._interrupt = True

    def start(self, silent=False):
        if self.status:
            return
        if not self.get_orgs():
            raise ValueError('Server cannot be started without ' + \
                'any organizations')
        logger.debug('Starting server. %r' % {
            'server_id': self.id,
        })
        self._generate_ovpn_conf()
        self._enable_ip_forwarding()
        self._set_iptable_rules()
        _events[self.id] = threading.Event()
        thread = threading.Thread(target=self._run)
        thread.start()
        _threads[self.id] = thread
        _start_time[self.id] = int(time.time()) - 1
        _output[self.id] = ''
        if not _events[self.id].wait(THREAD_EVENT_TIMEOUT):
            raise ValueError('Server thread failed to return start event.')
        try:
            _events[self.id].clear()
        except KeyError:
            pass
        if not silent:
            Event(type=SERVERS_UPDATED)
            LogEntry(message='Started server "%s".' % self.name)

    def stop(self, silent=False):
        if not self.status:
            return
        logger.debug('Stopping server. %r' % {
            'server_id': self.id,
        })
        _process[self.id].send_signal(signal.SIGINT)
        if not _events[self.id].wait(THREAD_EVENT_TIMEOUT):
            raise ValueError('Server thread failed to return stop event.')
        if not silent:
            Event(type=SERVERS_UPDATED)
            LogEntry(message='Stopped server "%s".' % self.name)

    def force_stop(self, silent=False):
        if not self.status:
            return
        logger.info('Forcing stop server. %r' % {
            'server_id': self.id,
        })
        _process[self.id].send_signal(signal.SIGKILL)
        if not _events[self.id].wait(THREAD_EVENT_TIMEOUT):
            raise ValueError('Server thread failed to return stop event.')
        if not silent:
            Event(type=SERVERS_UPDATED)
            LogEntry(message='Stopped server "%s".' % self.name)

    def restart(self, silent=False):
        if not self.status:
            self.start()
        logger.debug('Restarting server. %r' % {
            'server_id': self.id,
        })
        self.stop(True)
        self.start(True)
        if not silent:
            Event(type=SERVERS_UPDATED)
            LogEntry(message='Restarted server "%s".' % self.name)

    def reload(self):
        if not self.status:
            self.start()
        logger.debug('Reloading server. %r' % {
            'server_id': self.id,
        })
        _process[self.id].send_signal(signal.SIGUSR1)
        LogEntry(message='Reloaded server "%s".' % self.name)

    def get_output(self):
        if self.id not in _output:
            return ''
        return _output[self.id]

    def clear_output(self):
        if self.id not in _output:
            return ''
        _output[self.id] = ''
        self._event_delay(type=SERVER_OUTPUT_UPDATED, resource_id=self.id)

    def get_clients(self):
        if not self.status:
            return []
        clients = {}

        if os.path.isfile(self.ovpn_status_path):
            with open(self.ovpn_status_path, 'r') as status_file:
                for line in status_file.readlines():
                    if line[:11] != 'CLIENT_LIST':
                        continue
                    line_split = line.strip('\n').split(',')
                    client_id = line_split[1]
                    real_address = line_split[2]
                    virt_address = line_split[3]
                    bytes_received = line_split[4]
                    bytes_sent = line_split[5]
                    connected_since = line_split[7]
                    clients[client_id] = {
                        'real_address': real_address,
                        'virt_address': virt_address,
                        'bytes_received': bytes_received,
                        'bytes_sent': bytes_sent,
                        'connected_since': connected_since,
                    }

        return clients

    @staticmethod
    def get_servers():
        logger.debug('Getting servers.')
        data_path = app_server.data_path
        path = os.path.join(data_path, SERVERS_DIR)
        servers = []
        if os.path.isdir(path):
            for server_id in os.listdir(path):
                server = Server(server_id)
                try:
                    server.load()
                except IOError:
                    logger.exception('Failed to load server conf, ' +
                        'ignoring server. %r' % {
                            'server_id': server_id,
                        })
                    continue
                servers.append(server)
        return servers
