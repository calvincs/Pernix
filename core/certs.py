"""Pernix — SSL certificate generation and validation."""

import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger("pernix.certs")

CERTS_DIR = Path("data/certs")
SELF_SIGNED_CERT = CERTS_DIR / "self_signed.crt"
SELF_SIGNED_KEY = CERTS_DIR / "self_signed.key"


def ensure_ssl_certs(ssl_mode: str, cert_path: str, key_path: str) -> tuple[str, str]:
    """Return (cert_path, key_path), generating self-signed certs if needed.

    Raises RuntimeError on failure.
    """
    if ssl_mode == "custom":
        if not cert_path or not key_path:
            raise RuntimeError("Custom SSL mode requires both ssl_cert_path and ssl_key_path")
        if not Path(cert_path).is_file():
            raise RuntimeError(f"SSL certificate not found: {cert_path}")
        if not Path(key_path).is_file():
            raise RuntimeError(f"SSL key not found: {key_path}")
        return cert_path, key_path

    # Self-signed mode
    if SELF_SIGNED_CERT.is_file() and SELF_SIGNED_KEY.is_file():
        logger.info("Using existing self-signed certs in %s", CERTS_DIR)
        return str(SELF_SIGNED_CERT), str(SELF_SIGNED_KEY)

    # Generate new self-signed certificate
    if not shutil.which("openssl"):
        raise RuntimeError(
            "openssl not found — install it or use custom certificates "
            "(set ssl_mode to 'custom' and provide ssl_cert_path / ssl_key_path)"
        )

    CERTS_DIR.mkdir(parents=True, exist_ok=True)

    result = subprocess.run(
        [
            "openssl",
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-sha256",
            "-keyout",
            str(SELF_SIGNED_KEY),
            "-out",
            str(SELF_SIGNED_CERT),
            "-days",
            "365",
            "-nodes",
            "-subj",
            "/CN=Pernix",
            "-addext",
            "subjectAltName=DNS:localhost,IP:127.0.0.1",
        ],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(f"Failed to generate self-signed certs: {result.stderr}")

    os.chmod(CERTS_DIR, 0o700)  # dir: owner rwx only
    os.chmod(SELF_SIGNED_KEY, 0o600)  # key: owner rw only

    logger.info("Generated self-signed SSL certificates in %s", CERTS_DIR)
    return str(SELF_SIGNED_CERT), str(SELF_SIGNED_KEY)


def validate_ssl_config(ssl_mode: str, cert_path: str, key_path: str) -> list[str]:
    """Validate SSL configuration without generating anything.

    Returns a list of error strings (empty = valid).
    """
    errors = []

    if ssl_mode == "self_signed":
        if not shutil.which("openssl"):
            errors.append("openssl is not installed — required for self-signed certificate generation")
    elif ssl_mode == "custom":
        if not cert_path:
            errors.append("Certificate PEM path is required for custom SSL mode")
        elif not Path(cert_path).is_file():
            errors.append(f"Certificate file not found: {cert_path}")

        if not key_path:
            errors.append("Key PEM path is required for custom SSL mode")
        elif not Path(key_path).is_file():
            errors.append(f"Key file not found: {key_path}")
    else:
        errors.append(f"Unknown ssl_mode: {ssl_mode}")

    return errors
