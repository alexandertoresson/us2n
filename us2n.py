# us2n.py

import json
import time
import select
import socket
import machine
import network
import sys

print_ = print
VERBOSE = 1
def print(*args, **kwargs):
    if VERBOSE:
        print_(*args, **kwargs)


def read_config(filename='us2n.json', obj=None, default=None):
    with open(filename, 'r') as f:
        config = json.load(f)
        if obj is None:
            return config
        return config.get(obj, default)


def parse_bind_address(addr, default=None):
    if addr is None:
        return default
    args = addr
    if not isinstance(args, (list, tuple)):
        args = addr.rsplit(':', 1)
    host = '' if len(args) == 1 or args[0] == '0' else args[0]
    port = int(args[1])
    return host, port

class LineReader:
    def __init__(self, maxsize):
        self.data = bytearray()
        self.maxsize = maxsize

    def feed(self, data):
        echo = bytearray()
        for i, c in enumerate(data):
            if c == 13 or c == 10:
                ret = self.data
                self.data = bytearray()
                echo.append(c)
                return ret, data[i+1:], echo
            elif c == 8 or c == 127:
                if len(self.data):
                    echo.extend(b'\x08\x1b[K')
                    self.data = self.data[:-1]
            elif len(self.data) < self.maxsize:
                echo.append(c)
                self.data.append(c)
        return None, b'', echo

class RingBuffer:
    def __init__(self, size):
        self.data = bytearray(size)
        self.size = size
        self.index_put = 0
        self.index_get = 0
        self.index_rewind = 0
        self.wrapped = False

    def put(self, data):
        cur_idx = 0
        while cur_idx < len(data):
            min_idx = min(self.index_put+len(data)-cur_idx, self.size)
            self.data[self.index_put:min_idx] = data[cur_idx:min_idx-self.index_put+cur_idx]
            cur_idx += min_idx-self.index_put
            if self.index_get > self.index_put:
                self.index_get = max(min_idx+1, self.index_get)
                if self.index_get >= self.size:
                    self.index_get -= self.size
            self.index_put = min_idx
            if self.index_put == self.size:
                self.index_put = 0
                self.wrapped = True
                if self.index_get == 0:
                    self.index_get = 1

    def get(self, numbytes):
        data = bytearray()
        while len(data) < numbytes:
            start = self.index_get
            min_idx = min(self.index_get+numbytes-len(data), self.size)
            if self.index_put >= self.index_get:
                min_idx = min(min_idx, self.index_put)
            data.extend(self.data[start:min_idx])
            self.index_get = min_idx
            if self.index_get == self.size:
                self.index_get = 0
            if self.index_get == self.index_put:
                break
        return data

    def has_data(self):
        return self.index_get != self.index_put

    def rewind(self):
        if self.wrapped:
            self.index_get = (self.index_put+1) % self.size
        else:
            self.index_get = 0

def UART(config):
    config = dict(config)
    uart_type = config.pop('type') if 'type' in config.keys() else 'hw'
    if 'tx' in config:
        config['tx'] = machine.Pin(config['tx'])
    if 'rx' in config:
        config['rx'] = machine.Pin(config['rx'])
    port = config.pop('port')
    if uart_type == 'SoftUART':
        print('Using SoftUART...')
        uart = machine.SoftUART(config.pop('tx'),config.pop('rx'),timeout=config.pop('timeout'),timeout_char=config.pop('timeout_char'),baudrate=config.pop('baudrate'))
    else:
        print('Using HW UART...')
        uart = machine.UART(port)
        uart.init(**config)
    return uart


class Bridge:

    def __init__(self, config):
        super().__init__()
        self.config = config
        self.uart = None
        self.uart_port = config['uart']['port']
        self.tcp = None
        self.address = parse_bind_address(config['tcp']['bind'])
        self.bind_port = self.address[1]
        self.client = None
        self.client_address = None
        self.ring_buffer = RingBuffer(16 * 1024)
        self.cur_line = bytearray()
        self.state = 'listening'
        self.uart = UART(self.config['uart'])
        self.escape = self.config.get('escape', 2)
        print('UART opened ', self.uart)
        print(self.config)

    def bind(self):
        tcp = socket.socket()
        tcp.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    #    tcp.setblocking(False)
        tcp.bind(self.address)
        tcp.listen(5)
        print('Bridge listening at TCP({0}) for UART({1})'
              .format(self.bind_port, self.uart_port))
        self.tcp = tcp
        if 'ssl' in self.config:
            import ntptime
            ntptime.host = "pool.ntp.org"
            while True:
                try:
                    ntptime.settime()
                except OSError as e:
                    print(f"NTP synchronization failed, {e}")
                    time.sleep(15)
                    continue
                print(f"NTP synchronization succeeded, {time.time()}")
                print(time.gmtime())
                break

        return tcp

    def fill(self, fds):
        if self.uart is not None:
            fds.append(self.uart)
        if self.tcp is not None:
            fds.append(self.tcp)
        if self.client is not None:
            fds.append(self.client)
        return fds

    def recv(self, sock, n):
        if hasattr(sock, 'recv'):
            return sock.recv(n)
        else:
            # SSL-wrapped sockets don't have recv(), use read() instead
            # TODO: Read more than 1 byte? Probably needs non-blocking sockets
            return sock.read(1)

    def sendall(self, sock, bytes):
        if hasattr(sock, 'sendall'):
            return sock.sendall(bytes)
        else:
            # SSL-wrapped sockets don't have sendall(), use write() instead
            return sock.write(bytes)

    def shortcommand(self, cmd):
        self.state = 'authenticated'
        if cmd == bytes([self.escape]):
            self.state = 'echochar'
            return bytes([self.escape])
        elif cmd == b'd':
            self.client.close()
        elif cmd == b'x':
            sys.exit()
        return b''

    def longcommand(self, cmd):
        cmd = cmd.split(b' ')
        self.state = 'authenticated'
        if cmd[0] == b'disconnect':
            self.client.close()
        elif cmd[0] == b'restart':
            sys.exit()
        elif cmd[0] == b'logout':
            self.state = 'enterpassword'
            self.sendall(self.client, "\r\nLogout\r\npassword: ")

    def handle(self, fd):
        if fd == self.tcp:
            self.close_client()
            self.open_client()
        elif fd == self.client:
            data = self.recv(self.client, 4096)
            if data:
                while len(data) > 0:
                    if self.state == 'enterpassword':
                        password, data, _ = self.passwordreader.feed(data)
                        if password is not None:
                            print("Received password {0}".format(password))
                            if password.decode('utf-8') == self.config['auth']['password']:
                                self.sendall(self.client, "\r\nAuthentication succeeded\r\n")
                                self.state = 'authenticated'
                                self.ring_buffer.rewind()
                                fd = self.uart # Send all uart data
                            else:
                                self.sendall(self.client, "\r\nAuthentication failed\r\npassword: ")
                    elif self.state == 'authenticated' or self.state == 'echochar':
                        for i, c in enumerate(data):
                            if c == self.escape and not (i == 0 and self.state == 'echochar'):
                                self.state = 'escapereceived'
                                i -= 1
                                break
                        print('TCP({0})->UART({1}) {2}'.format(self.bind_port,
                                                               self.uart_port, data[:i+1]))
                        self.uart.write(data[:i+1])
                        data = data[i+1:]
                        if self.state == 'escapereceived':
                            # Remove the escape
                            data = data[1:]
                        else:
                            self.state = 'authenticated'
                    elif self.state == 'escapereceived':
                        if data[0:1] == b':':
                            self.state = 'entercommand'
                            data = data[1:]
                            self.sendall(self.client, "\r\n:")
                        else:
                            echo = self.shortcommand(data[0:1])
                            data = echo + data[1:]
                    elif self.state == 'entercommand':
                        command, data, echo = self.cmdreader.feed(data)
                        self.sendall(self.client, echo)
                        if command is not None:
                            print("Received command {0}".format(command))
                            self.longcommand(command)
            else:
                print('Client ', self.client_address, ' disconnected')
                self.close_client()
        if fd == self.uart:
            data = self.uart.read(self.uart.any())
            if data is not None:
                self.ring_buffer.put(data)
            if self.state == 'authenticated' and self.ring_buffer.has_data():
                data = self.ring_buffer.get(4096)
                print('UART({0})->TCP({1}) {2}'.format(self.uart_port,
                                                       self.bind_port, data))
                self.sendall(self.client, data)

    def close_client(self):
        if self.client is not None:
            print('Closing client ', self.client_address)
            self.client.close()
            self.client = None
            self.client_address = None
        self.state = 'listening'

    def open_client(self):
        self.client, self.client_address = self.tcp.accept()
        print('Accepted connection from ', self.client_address)
        if 'ssl' in self.config:
            import ussl
            import ubinascii
            print(time.gmtime())
            sslconf = self.config['ssl'].copy()
            for key in ['cadata', 'key', 'cert']:
                if key in sslconf:
                    with open(sslconf[key], "rb") as file:
                        sslconf[key] = file.read()
            # TODO: Setting CERT_REQUIRED produces MBEDTLS_ERR_X509_CERT_VERIFY_FAILED
            sslconf['cert_reqs'] = ussl.CERT_OPTIONAL
            self.client = ussl.wrap_socket(self.client, server_side=True, **sslconf)
        self.state = 'enterpassword' if 'auth' in self.config else 'authenticated'
        self.passwordreader = LineReader(256)
        self.cmdreader = LineReader(1024)
        if self.state == 'enterpassword':
            self.sendall(self.client, "password: ")
            print("Prompting for password")

    def close(self):
        self.close_client()
        if self.tcp is not None:
            print('Closing TCP server {0}...'.format(self.address))
            self.tcp.close()
            self.tcp = None


class S2NServer:

    def __init__(self, config):
        self.config = config

    def report_exception(self, e):
        if 'syslog' in self.config:
            try:
                import usyslog
                import io
                import sys
                stringio = io.StringIO()
                sys.print_exception(e, stringio)
                stringio.seek(0)
                e_string = stringio.read()
                s = usyslog.UDPClient(**self.config['syslog'])
                s.error(e_string)
                s.close()
            except BaseException as e2:
                sys.print_exception(e2)

    def serve_forever(self):
        while True:
            config_network(self.config.get('wlan'), self.config.get('name'))
            try:
                self._serve_forever()
            except KeyboardInterrupt:
                print('Ctrl-C pressed. Bailing out')
                break
            except BaseException as e:
                import sys
                sys.print_exception(e)
                self.report_exception(e)
                time.sleep(1)
                print("Restarting")

    def bind(self):
        bridges = []
        for config in self.config['bridges']:
            bridge = Bridge(config)
            bridge.bind()
            bridges.append(bridge)
        return bridges

    def _serve_forever(self):
        bridges = self.bind()

        try:
            while True:
                fds = []
                for bridge in bridges:
                    bridge.fill(fds)
                rlist, _, xlist = select.select(fds, (), fds)
                if xlist:
                    print('Errors. bailing out')
                    break
                for fd in rlist:
                    for bridge in bridges:
                        bridge.handle(fd)
        finally:
            for bridge in bridges:
                bridge.close()


def config_lan(config, name):
    # For a board which has LAN
    pass


def config_wlan(config, name):
    if config is None:
        return None, None
    return (WLANStation(config.get('sta'), name),
            WLANAccessPoint(config.get('ap'), name))


def WLANStation(config, name):
    if config is None:
        return
    config.setdefault('connection_attempts', -1)
    essid = config['essid']
    password = config['password']
    attempts_left = config['connection_attempts']
    sta = network.WLAN(network.STA_IF)

    if not sta.isconnected():
        while not sta.isconnected() and attempts_left != 0:
            attempts_left -= 1
            sta.disconnect()
            sta.active(False)
            sta.active(True)
            sta.connect(essid, password)
            print('Connecting to WiFi...')
            n, ms = 20, 250
            t = n*ms
            while not sta.isconnected() and n > 0:
                time.sleep_ms(ms)
                n -= 1
        if not sta.isconnected():
            print('Failed to connect wifi station after {0}ms. I give up'
                  .format(t))
            return sta
    print('Wifi station connected as {0}'.format(sta.ifconfig()))
    return sta


def WLANAccessPoint(config, name):
    if config is None:
        return
    config.setdefault('essid', name)
    config.setdefault('channel', 11)
    config.setdefault('authmode',
                      getattr(network,'AUTH_' +
                              config.get('authmode', 'OPEN').upper()))
    config.setdefault('hidden', False)
#    config.setdefault('dhcp_hostname', name)
    ap = network.WLAN(network.AP_IF)
    if not ap.isconnected():
        ap.active(True)
        n, ms = 20, 250
        t = n * ms
        while not ap.active() and n > 0:
            time.sleep_ms(ms)
            n -= 1
        if not ap.active():
            print('Failed to activate wifi access point after {0}ms. ' \
                  'I give up'.format(t))
            return ap

#    ap.config(**config)
    print('Wifi {0!r} connected as {1}'.format(ap.config('essid'),
                                               ap.ifconfig()))
    return ap


def config_network(config, name):
    config_lan(config, name)
    config_wlan(config, name)


def config_verbosity(config):
    global VERBOSE
    VERBOSE = config.setdefault('verbose', 1)


def server(config_filename='us2n.json'):
    config = read_config(config_filename)
    VERBOSE = config.setdefault('verbose', 1)
    name = config.setdefault('name', 'Tiago-ESP32')
    config_verbosity(config)
    print(50*'=')
    print('Welcome to ESP8266/32 serial <-> tcp bridge\n')
    return S2NServer(config)
