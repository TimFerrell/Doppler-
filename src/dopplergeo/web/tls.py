"""Self-signed TLS certificate so the web app can also be served over https on the LAN.

Browsers only allow microphone access on secure pages (https, or localhost). Serving a second https
listener with a self-signed certificate lets other machines on the LAN record: the browser shows a
certificate warning once, which the user accepts for this server.
"""
from __future__ import annotations

import datetime as dt
import ipaddress
import os
import socket
from pathlib import Path


def _local_names() -> tuple[list[str], list[str]]:
    names = {"localhost", socket.gethostname()}
    ips = {"127.0.0.1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None):
            ips.add(info[4][0])
    except OSError:
        pass
    for extra in os.environ.get("DOPPLERGEO_TLS_HOSTS", "").split(","):
        extra = extra.strip()
        if not extra:
            continue
        try:
            ipaddress.ip_address(extra)
            ips.add(extra)
        except ValueError:
            names.add(extra)
    return sorted(names), sorted(i for i in ips if ":" not in i or i == "::1")


def ensure_cert(directory: str | Path) -> tuple[Path, Path]:
    """(cert, key) paths; created on first use, valid 10 years."""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    cert, key = d / "dopplergeo-selfsigned.crt", d / "dopplergeo-selfsigned.key"
    if cert.exists() and key.exists():
        return cert, key
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    k = ec.generate_private_key(ec.SECP256R1())
    names, ips = _local_names()
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "dopplergeo (self-signed, LAN)")])
    now = dt.datetime.now(dt.timezone.utc)
    san = [x509.DNSName(n) for n in names] + [x509.IPAddress(ipaddress.ip_address(i)) for i in ips]
    c = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(k.public_key())
         .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
         .not_valid_after(now + dt.timedelta(days=3650)).add_extension(x509.SubjectAlternativeName(san), critical=False)
         .sign(k, hashes.SHA256()))
    key.write_bytes(k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption()))
    os.chmod(key, 0o600)
    cert.write_bytes(c.public_bytes(serialization.Encoding.PEM))
    return cert, key
