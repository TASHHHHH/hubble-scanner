"""Same-network worker invitations. TLS identity lives for one desktop session."""
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import socket
import ssl
import tempfile
import threading

from coordinator import make_server


def lan_addresses():
    addresses = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    return list(dict.fromkeys(item[4][0] for item in addresses
                              if not ipaddress.ip_address(item[4][0]).is_loopback))


def start_listener(queue, address, port, secret):
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    ip = ipaddress.IPv4Address(address)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Hubble Scanner')])
    now = datetime.now(timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5)).not_valid_after(now + timedelta(days=365))
            .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ip)]), critical=False)
            .sign(key, hashes.SHA256()))
    pem = cert.public_bytes(serialization.Encoding.PEM)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    with tempfile.TemporaryDirectory() as tmp:
        chain = Path(tmp) / 'listener.pem'
        chain.write_bytes(pem + key.private_bytes(serialization.Encoding.PEM,
                          serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        context.load_cert_chain(chain)
    server = make_server(queue, str(ip), port, secret, restricted=True)
    try:
        server.socket = context.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
    except Exception:
        server.server_close()
        raise
    return server, pem.decode('ascii')


def read_invite(path, workspace):
    from cryptography import x509
    with Path(path).open('rb') as stream:
        raw = stream.read(16385)
    if len(raw) > 16384:
        raise ValueError('Invite file is too large.')
    invite = json.loads(raw)
    if not isinstance(invite, dict) or invite.get('format') != 'hubble-lan-1':
        raise ValueError('Choose a Hubble Scanner worker invite.')
    address = ipaddress.IPv4Address(invite['address'])
    token = invite['token']
    if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', token):
        raise ValueError('Invalid worker credential in invite.')
    cert = x509.load_pem_x509_certificate(invite['certificate'].encode('ascii'))
    from cryptography.hazmat.primitives.serialization import Encoding
    pem = cert.public_bytes(Encoding.PEM)
    ca = Path(workspace) / ('lan-' + hashlib.sha256(pem).hexdigest() + '.pem')
    ca.write_bytes(pem)
    return f'https://{address}:8766', token, ca
