"""Replays the PTCGO client handshake against the local server, so the
protocol can be validated without launching the game."""

import hashlib
import json
import socket
import ssl
import struct
import sys

HEADER = struct.Struct(">III")
USERNAME = "testtrainer"
PASSWORD = "hunter2"


def connect(host, port):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False       # client pins nothing; it allows self-signed
    ctx.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection((host, port), timeout=10)
    return ctx.wrap_socket(raw, server_hostname=host)


def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def read_frame(sock):
    head = recv_exact(sock, 12)
    if head is None:
        return None
    length, rid, flags = HEADER.unpack(head)
    body = recv_exact(sock, length - 8) if length > 8 else b""
    return rid, flags, json.loads(body.decode()) if body else None


def write_frame(sock, obj, rid=0, flags=0):
    payload = json.dumps(obj, separators=(",", ":")).encode()
    sock.sendall(HEADER.pack(len(payload) + 8, rid, flags) + payload)


def envelope(name, value=None):
    return {"name": name, "value": value}


def expect(sock, want):
    frame = read_frame(sock)
    if frame is None:
        raise SystemExit("FAIL: server closed while waiting for %s" % want)
    _, _, obj = frame
    got = (obj or {}).get("name")
    print("   <- %s %s" % (got, json.dumps((obj or {}).get("value"))))
    if got != want:
        raise SystemExit("FAIL: expected %s, got %s" % (want, got))
    return (obj or {}).get("value")


def main():
    print("1. gateway handshake")
    gw = connect("127.0.0.1", 39389)
    print("   TLS %s" % gw.version())
    write_frame(gw, envelope("RequestConnectionServiceWithVersion",
                             {"clientVersion": "2.95.0.5815"}))
    print("   -> RequestConnectionServiceWithVersion")
    endpoint = expect(gw, "ConnectionService")["connectionEndPoint"]
    gw.close()

    host, port = endpoint.rsplit(":", 1)
    print("\n2. game server at %s:%s" % (host, port))
    gs = connect(host, int(port))
    print("   TLS %s" % gs.version())

    write_frame(gs, envelope("RequestSession", {
        "connectionInfo": {
            "hostName": "foo",
            "countryCode": "en_US",
            "clientParameters": {
                "clientVersion": "2.95.0.5815",
                "clientPlatform": "WindowsPlayer",
            },
        }
    }))
    print("   -> RequestSession")
    expect(gs, "GrantedSession")

    write_frame(gs, envelope("RequestLogin", None))
    print("   -> RequestLogin")
    auth_types = expect(gs, "RequestedAuthType")["validAuthTypes"]
    if "sha1" not in auth_types:
        raise SystemExit("FAIL: sha1 not offered")

    print("\n3. digest auth as %s" % USERNAME)
    write_frame(gs, envelope("StartAuthentication", {"authType": "sha1"}))
    print("   -> StartAuthentication sha1")
    expect(gs, "RequestedUsername")

    write_frame(gs, envelope("RequestSaltForUser", {"username": USERNAME}))
    print("   -> RequestSaltForUser")
    salt = expect(gs, "DigestSalt")["salt"]

    # client-side: sha1(password + ":" + salt), lowercase hex
    digest = hashlib.sha1((PASSWORD + ":" + salt).encode()).hexdigest()
    write_frame(gs, envelope("AuthenticateDigest",
                             {"username": USERNAME, "digest": digest}))
    print("   -> AuthenticateDigest %s" % digest)
    result = expect(gs, "AuthenticationSuccessful")

    print("\nPASS: logged in as %s (accountID %s)"
          % (result["account"]["username"], result["account"]["accountID"]))
    gs.close()


if __name__ == "__main__":
    sys.exit(main())
