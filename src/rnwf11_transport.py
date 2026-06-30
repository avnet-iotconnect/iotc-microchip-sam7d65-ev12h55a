# SPDX-License-Identifier: MIT
# Copyright (C) 2026 Avnet
# Bridges paho-mqtt's TCP transport over the EV12H55A/RNWF11 WiFi module's raw AT socket commands.

import re
import select
import socket
import ssl
import struct
import threading
import time
from collections import deque
from urllib.parse import urlparse

import serial

DEFAULT_PORT = "/dev/ttyS1"
DEFAULT_BAUD = 230400

_WRITE_CHUNK_MAX = 512   # conservative chunk size for AT+SOCKWR
_READ_CHUNK_MAX  = 1460  # RNWF11 delivers at most one MSS per AT+SOCKRD


class Rnwf11Error(Exception):
    """Raised when the RNWF11 module returns an error or an unexpected response."""


class Rnwf11Uart:
    """Low-level synchronous AT-command driver for the RNWF11 WiFi module."""

    def __init__(self, port=DEFAULT_PORT, baud=DEFAULT_BAUD):
        self._ser = serial.Serial(port, baud, timeout=0.05)
        self._buf = bytearray()
        self._pending_events = deque()

    def close(self):
        self._ser.close()

    # ---- low level byte/line helpers ----

    def _read_more(self, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = self._ser.read(4096)
            if chunk:
                self._buf.extend(chunk)
                return True
        return False

    def _read_line(self, timeout):
        # Strips stray '>' idle-prompt bytes the module emits between commands.
        deadline = time.monotonic() + timeout
        while True:
            idx = self._buf.find(b"\r\n")
            if idx != -1:
                raw = bytes(self._buf[:idx])
                del self._buf[:idx + 2]
                line = raw.replace(b">", b"").strip()
                if line:
                    return line.decode(errors="replace")
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Rnwf11Error("Timeout waiting for a response line")
            self._read_more(min(remaining, 0.2))

    def _wait_for_marker(self, marker: bytes, timeout):
        deadline = time.monotonic() + timeout
        while True:
            idx = self._buf.find(marker)
            if idx != -1:
                # Rescue any async event lines buried before the marker so they
                # aren't silently dropped (e.g. +SOCKRXT arriving during AT+SOCKRD).
                before = bytes(self._buf[:idx])
                del self._buf[:idx + len(marker)]
                for part in before.split(b'\r\n'):
                    line = part.replace(b'>', b'').strip()
                    if line and line.startswith(b'+'):
                        self._pending_events.append(line.decode(errors='replace'))
                return
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Rnwf11Error("Timeout waiting for %r prompt" % marker)
            self._read_more(min(remaining, 0.2))

    def _read_exact(self, n: int, timeout) -> bytes:
        deadline = time.monotonic() + timeout
        while len(self._buf) < n:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Rnwf11Error("Timeout reading %d raw bytes" % n)
            self._read_more(min(remaining, 0.2))
        data = bytes(self._buf[:n])
        del self._buf[:n]
        return data

    def _expect_ok(self, timeout):
        deadline = time.monotonic() + timeout
        while True:
            remaining = max(0.1, deadline - time.monotonic())
            line = self._read_line(remaining)
            if line.startswith("AT+"):
                continue  # command echo
            if line == "OK":
                return
            if line.startswith("ERROR"):
                raise Rnwf11Error(line)
            if line.startswith("+"):
                self._pending_events.append(line)
                continue
            # unrecognized stray text; ignore it

    # ---- plain command/response (no raw payload involved) ----

    def command(self, cmd: str, timeout=5.0) -> list:
        self._ser.write((cmd + "\r\n").encode())
        lines = []
        deadline = time.monotonic() + timeout
        while True:
            remaining = max(0.1, deadline - time.monotonic())
            line = self._read_line(remaining)
            if line == cmd or line.startswith("AT+"):
                continue  # command echo
            if line == "OK":
                return lines
            if line.startswith("ERROR"):
                raise Rnwf11Error(line)
            if line.startswith("+"):
                self._pending_events.append(line)
            else:
                lines.append(line)

    def wait_for_event(self, prefixes, timeout=10.0) -> str:
        prefixes = tuple(prefixes)
        deadline = time.monotonic() + timeout
        while True:
            for line in list(self._pending_events):
                if line.startswith(prefixes):
                    self._pending_events.remove(line)
                    return line
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Rnwf11Error("Timeout waiting for one of %r" % (prefixes,))
            try:
                line = self._read_line(min(remaining, 0.5))
            except Rnwf11Error:
                continue
            if line.startswith(prefixes):
                return line
            if line.startswith("+"):
                self._pending_events.append(line)

    def poll_event(self, timeout=0.05):
        if self._pending_events:
            return self._pending_events.popleft()
        try:
            line = self._read_line(timeout)
        except Rnwf11Error:
            return None
        if line.startswith("+"):
            return line
        return None

    # ---- raw socket payload write/read ----

    def socket_write(self, sock_id: int, payload: bytes, timeout=10.0):
        self._ser.write(("AT+SOCKWR=%d,%d\r\n" % (sock_id, len(payload))).encode())
        self._wait_for_marker(b"#", timeout)
        self._ser.write(payload)
        self._expect_ok(timeout)

    def socket_read(self, sock_id: int, n: int, timeout=10.0) -> bytes:
        n = min(n, _READ_CHUNK_MAX)
        self._ser.write(("AT+SOCKRD=%d,2,%d\r\n" % (sock_id, n)).encode())
        self._wait_for_marker(b"#", timeout)
        data = self._read_exact(n, timeout)
        self._expect_ok(timeout)
        # The module resets its cumulative +SOCKRXT counter after AT+SOCKRD, so any
        # buffered +SOCKRXT values for this socket are now stale by exactly n bytes.
        updated = deque()
        for ev in self._pending_events:
            m = re.match(r"\+SOCKRXT:(%d),(\d+)" % sock_id, ev)
            if m:
                remaining = int(m.group(2)) - n
                if remaining > 0:
                    updated.append("+SOCKRXT:%d,%d" % (sock_id, remaining))
            else:
                updated.append(ev)
        self._pending_events = updated
        return data

    # ---- WiFi join ----

    def wifi_is_associated(self) -> bool:
        try:
            self.command("AT+ASSOC?")
            return True
        except Rnwf11Error:
            return False

    def wifi_scan_security(self, ssid: str, timeout=8.0) -> int:
        self._ser.write(b"AT+WSCN=0\r\n")
        deadline = time.monotonic() + timeout
        security = None
        while True:
            remaining = max(0.1, deadline - time.monotonic())
            line = self._read_line(remaining)
            if line.startswith("AT+") or line == "OK":
                continue
            if line.startswith("+WSCNIND:"):
                m = re.match(r'\+WSCNIND:(-?\d+),(\d+),(\d+),"([^"]*)","?(.*?)"?$', line)
                if m and m.group(5) == ssid:
                    security = int(m.group(2))
                continue
            if line.startswith("+WSCNDONE:"):
                break
        if security is None:
            raise Rnwf11Error("SSID %r not found in scan results" % ssid)
        return security

    def wifi_connect(self, ssid: str, password: str, security=None, timeout=20.0) -> str:
        if security is None:
            security = self.wifi_scan_security(ssid)
        self.command('AT+WSTAC=1,"%s"' % ssid)
        self.command("AT+WSTAC=2,%d" % security)
        self.command('AT+WSTAC=3,"%s"' % password)
        self.command("AT+WSTAC=4,0")
        self.command("AT+WSTA=1")
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Rnwf11Error("Timeout waiting for WiFi connection")
            event = self.wait_for_event(("+WSTAAIP:", "+WSTAERR:"), timeout=remaining)
            if event.startswith("+WSTAERR:"):
                raise Rnwf11Error("WiFi connection failed: %s" % event)
            # +WSTAAIP:<id>,"<ip>" -- skip the IPv6 link-local one, wait for IPv4
            m = re.match(r'\+WSTAAIP:\d+,"([^"]+)"', event)
            if m and "." in m.group(1):
                return m.group(1)

    def connect_wifi_if_needed(self, ssid: str, password: str, security=None, timeout=20.0) -> None:
        if not self.wifi_is_associated():
            self.wifi_connect(ssid, password, security=security, timeout=timeout)

    # ---- raw TCP socket lifecycle ----

    def socket_open_tcp(self) -> int:
        # AT+SOCKO's own reply line ("+SOCKO:<id>") starts with '+', so command()
        # files it into _pending_events alongside genuine async notifications --
        # check there too, not just the plain (non-'+') line list.
        lines = self.command("AT+SOCKO=2,4")
        candidates = lines + list(self._pending_events)
        for line in candidates:
            m = re.match(r"\+SOCKO:(\d+)", line)
            if m:
                if line in self._pending_events:
                    self._pending_events.remove(line)
                return int(m.group(1))
        raise Rnwf11Error("AT+SOCKO did not return a socket id: %r" % candidates)

    def socket_connect(self, sock_id: int, host: str, port: int, timeout=10.0):
        self.command('AT+SOCKBR=%d,"%s",%d' % (sock_id, host, port))
        event = self.wait_for_event(("+SOCKIND:", "+SOCKERR:"), timeout=timeout)
        if event.startswith("+SOCKERR:"):
            raise Rnwf11Error("Socket connect failed: %s" % event)

    def socket_close(self, sock_id: int):
        try:
            self.command("AT+SOCKCL=%d" % sock_id)
        except Rnwf11Error:
            pass

    # ---- DNS and HTTPS ----

    def _collect_socket_events(self, sock_id: int, timeout: float):
        """Drain +SOCKRXT/+SOCKCL events for sock_id. Returns (max_n, closed)."""
        max_n = None
        closed = False
        deadline = time.monotonic() + max(timeout, 0.1)
        unrelated = deque()

        # Drain pre-buffered events without rotating them back in (avoids spinning on
        # non-socket events that block serial reads in poll_event's fast-path).
        while self._pending_events:
            event = self._pending_events.popleft()
            m = re.match(r"\+SOCKRXT:(\d+),(\d+)", event)
            if m and int(m.group(1)) == sock_id:
                n = int(m.group(2))
                if max_n is None or n > max_n:
                    max_n = n
            elif event.startswith("+SOCKCL:%d" % sock_id):
                closed = True
            else:
                unrelated.append(event)

        # Read from serial until we find our event or timeout.
        while (max_n is None and not closed) and time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            try:
                line = self._read_line(min(0.5, remaining))
            except Rnwf11Error:
                continue
            if not line.startswith("+"):
                continue
            m = re.match(r"\+SOCKRXT:(\d+),(\d+)", line)
            if m and int(m.group(1)) == sock_id:
                n = int(m.group(2))
                if max_n is None or n > max_n:
                    max_n = n
            elif line.startswith("+SOCKCL:%d" % sock_id):
                closed = True
            else:
                unrelated.append(line)

        # Re-insert the event so socket_read()'s stale-adjustment can decrement it
        # across chunk reads. Without this, reading 1460 of N bytes loses the remaining
        # N-1460 bytes (no new +SOCKRXT arrives for data already in the module's buffer).
        if max_n is not None:
            self._pending_events.appendleft("+SOCKRXT:%d,%d" % (sock_id, max_n))
        self._pending_events.extendleft(reversed(unrelated))
        return max_n, closed

    def dns_resolve(self, hostname: str, timeout: float = 10.0) -> str:
        """Resolve hostname to IPv4 via DNS-over-TCP to 8.8.8.8:53 (RFC 1035 §4.2.2)."""
        # build a minimal A-record query
        header = struct.pack('>HHHHHH', 0x1234, 0x0100, 1, 0, 0, 0)
        qname = b''.join(bytes([len(p)]) + p.encode() for p in hostname.split('.')) + b'\x00'
        query = header + qname + struct.pack('>HH', 1, 1)  # QTYPE=A, QCLASS=IN
        # DNS-over-TCP wraps the query in a 2-byte length prefix
        payload = struct.pack('>H', len(query)) + query

        sock_id = self.socket_open_tcp()
        try:
            self.socket_connect(sock_id, '8.8.8.8', 53)
            self.socket_write(sock_id, payload)
            max_n, _ = self._collect_socket_events(sock_id, timeout)
            if not max_n:
                raise Rnwf11Error("DNS-over-TCP timed out for %r" % hostname)
            response = self.socket_read(sock_id, max_n)
        finally:
            self.socket_close(sock_id)

        if len(response) < 14:
            raise Rnwf11Error("DNS response too short for %r" % hostname)
        # skip the 2-byte TCP length prefix then parse the DNS message
        dns = response[2:]
        _, flags, _, ancount = struct.unpack('>HHHH', dns[:8])
        if flags & 0x000F:
            raise Rnwf11Error("DNS error %d for %r" % (flags & 0xF, hostname))
        if ancount == 0:
            raise Rnwf11Error("DNS returned no records for %r" % hostname)
        # skip question section: name labels + QTYPE(2) + QCLASS(2)
        i = 12
        while i < len(dns) and dns[i]:
            i += 1 + dns[i]
        i += 5  # null label + QTYPE + QCLASS
        # scan answer records for first A record
        for _ in range(ancount):
            if i + 10 > len(dns):
                break
            if dns[i] >= 0xC0:  # compressed name pointer
                i += 2
            else:
                while i < len(dns) and dns[i]:
                    i += 1 + dns[i]
                i += 1
            rtype, _, _, rdlen = struct.unpack('>HHIH', dns[i:i + 10])
            i += 10
            if rtype == 1 and rdlen == 4:  # A record, IPv4
                return '%d.%d.%d.%d' % tuple(dns[i:i + 4])
            i += rdlen
        raise Rnwf11Error("No A record found for %r" % hostname)

    def https_get(self, url: str, timeout: float = 30.0) -> bytes:
        """Make an HTTPS GET request over the RNWF11 module, returning the response body."""
        parsed = urlparse(url)
        host = parsed.hostname
        port = parsed.port or 443
        path = (parsed.path or '/') + ('?' + parsed.query if parsed.query else '')

        ip = self.dns_resolve(host)

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        inbio, outbio = ssl.MemoryBIO(), ssl.MemoryBIO()
        sslobj = ctx.wrap_bio(inbio, outbio, server_hostname=host)

        sock_id = self.socket_open_tcp()
        try:
            self.socket_connect(sock_id, ip, port)
            deadline = time.monotonic() + timeout

            # TLS handshake — loop until do_handshake() succeeds.
            # socket_read() is capped at _READ_CHUNK_MAX; stale +SOCKRXT counts in
            # _pending_events are adjusted down by socket_read so subsequent
            # _collect_socket_events calls return correct remaining byte counts.
            while time.monotonic() < deadline:
                try:
                    sslobj.do_handshake()
                    break
                except ssl.SSLWantReadError:
                    pass
                out = outbio.read()
                if out:
                    self.socket_write(sock_id, out)
                max_n, _ = self._collect_socket_events(sock_id, deadline - time.monotonic())
                if max_n:
                    inbio.write(self.socket_read(sock_id, max_n))
            else:
                raise Rnwf11Error("TLS handshake timed out for %s" % host)
            out = outbio.read()
            if out:
                self.socket_write(sock_id, out)

            # HTTP/1.0 avoids chunked transfer encoding; body arrives as raw bytes
            sslobj.write(('GET %s HTTP/1.0\r\nHost: %s\r\n\r\n' % (path, host)).encode())
            out = outbio.read()
            if out:
                self.socket_write(sock_id, out)

            # Read response until server closes connection
            plaintext = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise Rnwf11Error("HTTPS response timed out from %s" % host)
                max_n, closed = self._collect_socket_events(sock_id, remaining)
                if max_n:
                    inbio.write(self.socket_read(sock_id, max_n))
                    try:
                        plaintext.extend(sslobj.read(65536))
                    except ssl.SSLWantReadError:
                        pass
                if closed:
                    break
        finally:
            self.socket_close(sock_id)

        raw = bytes(plaintext)
        if b'\r\n\r\n' in raw:
            raw = raw.split(b'\r\n\r\n', 1)[1]
        return raw


class Rnwf11MqttTransport:
    """Pumps bytes between a local socketpair() end and an RNWF11 raw TCP socket."""

    def __init__(self, uart: Rnwf11Uart, sock_id: int):
        self._uart = uart
        self._sock_id = sock_id
        self.local_socket, self._bridge_socket = socket.socketpair()
        self._bridge_socket.setblocking(False)
        self._thread = None
        self._running = False

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._uart.socket_close(self._sock_id)
        try:
            self.local_socket.close()
        except OSError:
            pass

    def _run(self):
        try:
            while self._running:
                self._pump_outgoing()
                if not self._drain_incoming_events():
                    break
        except Exception as exc:  # noqa: BLE001 - surface to console, then stop the bridge
            print("RNWF11 transport bridge stopped:", exc)
        finally:
            try:
                self._bridge_socket.close()
            except OSError:
                pass

    def _pump_outgoing(self):
        readable, _, _ = select.select([self._bridge_socket], [], [], 0.05)
        if not readable:
            return
        try:
            data = self._bridge_socket.recv(65536)
        except BlockingIOError:
            return
        if not data:
            # paho closed its end of the socketpair -- propagate as a clean stop
            self._running = False
            return
        for offset in range(0, len(data), _WRITE_CHUNK_MAX):
            self._uart.socket_write(self._sock_id, data[offset:offset + _WRITE_CHUNK_MAX])

    def _drain_incoming_events(self) -> bool:
        # +SOCKRXT reports cumulative bytes available, not new bytes -- drain all pending
        # notifications and read the largest value once to avoid stranding data.
        max_n = None
        closed = False
        while True:
            event = self._uart.poll_event(timeout=0.05)
            if event is None:
                break
            m = re.match(r"\+SOCKRXT:(\d+),(\d+)", event)
            if m and int(m.group(1)) == self._sock_id:
                n = int(m.group(2))
                if max_n is None or n > max_n:
                    max_n = n
                continue
            if event.startswith("+SOCKCL:%d" % self._sock_id):
                closed = True
                continue
            # unrelated event (e.g. a different socket, or +ASSOC:/+WSTA*: noise); ignore
        if max_n:
            # Read in chunks: the module delivers at most _READ_CHUNK_MAX bytes per
            # AT+SOCKRD call, so loop until all buffered bytes are sent to paho.
            remaining = max_n
            while remaining > 0:
                chunk = min(remaining, _READ_CHUNK_MAX)
                data = self._uart.socket_read(self._sock_id, chunk)
                if data:
                    self._bridge_socket.sendall(data)
                remaining -= chunk
        return not closed


def patch_paho_transport(mqtt_client, transport: Rnwf11MqttTransport):
    mqtt_client._create_socket_connection = lambda: transport.local_socket
