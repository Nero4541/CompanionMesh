"""The public listener's own TLS certificate (no domain or certificate authority needed).

The server makes a self-signed certificate once and its boards trust exactly
that certificate (the installer copies it to them), so the connection is
encrypted and a board cannot be fooled into sending its token to anyone else,
whatever address it uses (an IP address, a dynamic DNS name, IPv6).

It is valid from 1970 on, because a board without network has no correct
clock yet when it first connects (Sipeed's image starts in 1970), and for a
very long time, because nobody renews it: replacing it means setting up the
boards again (``companion edge install``).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

CERT_NAME = "public.crt"
KEY_NAME = "public.key"


@dataclass(frozen=True)
class Certificate:
    cert_file: Path
    key_file: Path
    sha256: str  # of the DER certificate, as "AB:CD:..."

    def pem(self) -> str:
        return self.cert_file.read_text(encoding="ascii")


def fingerprint(cert_pem: bytes) -> str:
    der = x509.load_pem_x509_certificate(cert_pem).public_bytes(serialization.Encoding.DER)
    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def ensure_certificate(folder: Path) -> Certificate:
    """The certificate in ``folder``, made on first use."""
    cert_file, key_file = folder / CERT_NAME, folder / KEY_NAME
    if not (cert_file.is_file() and key_file.is_file()):
        folder.mkdir(parents=True, exist_ok=True)
        key = ec.generate_private_key(ec.SECP256R1())
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Companion Server")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(dt.datetime(1970, 1, 1, tzinfo=dt.UTC))
            .not_valid_after(dt.datetime(9999, 12, 31, tzinfo=dt.UTC))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName("companion-server")]), critical=False
            )
            .sign(key, hashes.SHA256())
        )
        key_bytes = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        tmp = key_file.with_suffix(".tmp")
        tmp.write_bytes(key_bytes)
        if os.name != "nt":
            tmp.chmod(0o600)
        os.replace(tmp, key_file)
        cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return Certificate(cert_file, key_file, fingerprint(cert_file.read_bytes()))
