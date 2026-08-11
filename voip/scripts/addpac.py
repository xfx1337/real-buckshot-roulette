#!/usr/bin/env python3
"""Run CLI commands on the AddPac AP1100F gateway over telnet.

The gateway refuses telnet on its LAN side (10.1.1.1) but accepts it on the
WAN side, which is the segment this Mac is cabled to.

    ./addpac.py "show voice port summary" "show sip"

Commands run in order and each reply is printed. The "-- more --" pager is
answered automatically, so full output comes back.
"""
import re
import socket
import sys
import time

HOST = "192.168.100.3"
USER = "root"
PASSWORD = "router"

IAC, DO, DONT, WILL, WONT = 255, 253, 254, 251, 252
PROMPT = re.compile(rb"AP1100F[^\r\n]*[#>]\s*$")


class Gateway:
    def __init__(self, host=HOST):
        self.sock = socket.create_connection((host, 23), timeout=6)
        self.sock.settimeout(0.4)

    def _strip_iac(self, data):
        """Answer telnet option negotiation, return the payload bytes."""
        out = b""
        i = 0
        while i < len(data):
            if data[i] == IAC and i + 2 < len(data):
                cmd, opt = data[i + 1], data[i + 2]
                if cmd == DO:
                    self.sock.sendall(bytes([IAC, WONT, opt]))
                elif cmd == WILL:
                    self.sock.sendall(bytes([IAC, DONT, opt]))
                i += 3
            elif data[i] == IAC:
                i += 2
            else:
                out += bytes([data[i]])
                i += 1
        return out

    def read_until(self, pattern, limit=8.0):
        out = b""
        end = time.time() + limit
        while time.time() < end:
            try:
                chunk = self.sock.recv(4096)
            except socket.timeout:
                if b"more" in out[-60:].lower():
                    self.sock.sendall(b" ")
                    end = time.time() + limit
                    continue
                if pattern and pattern.search(out[-80:]):
                    break
                continue
            if not chunk:
                break
            out += self._strip_iac(chunk)
            if pattern and pattern.search(out[-80:]):
                break
        return out

    def login(self):
        self.read_until(re.compile(rb"login:"))
        self.sock.sendall(USER.encode() + b"\r\n")
        self.read_until(re.compile(rb"[Pp]assword:"))
        self.sock.sendall(PASSWORD.encode() + b"\r\n")
        self.read_until(PROMPT)

    def run(self, command, limit=8.0):
        self.sock.sendall(command.encode() + b"\r\n")
        raw = self.read_until(PROMPT, limit)
        return raw.decode("latin-1", "replace").replace("\r", "")

    def close(self):
        self.sock.close()


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    gw = Gateway()
    gw.login()
    for command in sys.argv[1:]:
        print(f"--- {command}")
        print(gw.run(command))
    gw.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
