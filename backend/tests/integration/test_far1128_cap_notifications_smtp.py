"""FAR-1128 cap-notifications: real SMTP delivery over a loopback transport.

Runs ``modulo.core.email_service.send_email`` against a real, TLS-capable SMTP
server bound to 127.0.0.1 (a raw threaded socketserver with a self-signed
certificate) — and against a genuinely refused/closed loopback port. No
``aiosmtpd``, no smtplib mocks. This pins the production code's real
behaviour:

* a healthy server captures the delivered message over STARTTLS;
* a stalled first connection (no greeting) triggers the OSError retry path and
  the second attempt succeeds;
* a closed port exhausts the retry budget and raises ``EmailSendingError``;
* a refused RCPT (550) delivers nothing and surfaces as an SMTP-recipient
  error. Python >= 3.13 makes ``SMTPException`` an ``OSError`` subclass, so
  ``send_email`` retries it and finally wraps it in ``EmailSendingError``;
  on 3.12 the ``SMTPRecipientsRefused`` propagates unwrapped.
"""

from __future__ import annotations

import contextlib
import ipaddress
import smtplib
import socket
import socketserver
import ssl
import threading
import time
from collections.abc import Generator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import BinaryIO

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from modulo.core.email_service import EmailSendingError, send_email


def _generate_self_signed_cert() -> tuple[bytes, bytes]:
    """Return (cert-pem, key-pem) for an ad-hoc loopback SMTP server."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    return cert.public_bytes(serialization.Encoding.PEM), key_pem


@pytest.fixture(scope="session")
def tls_material(tmp_path_factory: pytest.TempPathFactory) -> tuple[str, str]:
    """Write a self-signed cert/key pair once and return their file paths."""
    cert_pem, key_pem = _generate_self_signed_cert()
    cert_dir = tmp_path_factory.mktemp("smtp-tls")
    cert_path = cert_dir / "server.crt"
    key_path = cert_dir / "server.key"
    cert_path.write_bytes(cert_pem)
    key_path.write_bytes(key_pem)
    return str(cert_path), str(key_path)


class _SmtpServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        cert_path: str,
        key_path: str,
        *,
        stall_first: bool = False,
        refuse_recipient: bool = False,
    ) -> None:
        super().__init__(("127.0.0.1", 0), _SmtpHandler)
        self.cert_path = cert_path
        self.key_path = key_path
        self.delivered: list[tuple[str, str, bytes]] = []
        self.stall_first = stall_first
        self.stall_used = False
        self.stall_duration = 5.0
        self.refuse_recipient = refuse_recipient

    @property
    def port(self) -> int:
        return self.server_address[1]

    def start(self) -> None:
        self._serve_thread = threading.Thread(target=self.serve_forever, daemon=True)
        self._serve_thread.start()

    def stop(self) -> None:
        self.shutdown()
        self.server_close()
        self._serve_thread.join(timeout=5)


class _SmtpHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        sock = self.request
        sock.settimeout(10)

        if self.server.stall_first and not self.server.stall_used:
            self.server.stall_used = True
            time.sleep(self.server.stall_duration)
            return

        active: socket.socket | ssl.SSLSocket = sock
        stream: BinaryIO = active.makefile("rb")

        def _send(text: str) -> None:
            active.sendall(text.encode() + b"\r\n")

        _send("220 loopback ESMTP modulo-test")
        mail_from = ""
        rcpt_to = ""
        try:
            while True:
                line = stream.readline()
                if not line:
                    return
                verb = line.decode("ascii", "ignore").strip().upper()
                if verb.startswith(("EHLO", "HELO")):
                    _send("250-localhost")
                    _send("250 STARTTLS")
                elif verb == "STARTTLS":
                    _send("220 Ready to start TLS")
                    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                    context.load_cert_chain(self.server.cert_path, self.server.key_path)
                    active = context.wrap_socket(active, server_side=True)
                    stream = active.makefile("rb")
                elif verb.startswith("MAIL FROM"):
                    mail_from = verb
                    _send("250 OK")
                elif verb.startswith("RCPT TO"):
                    if self.server.refuse_recipient:
                        _send("550 No such user here")
                    else:
                        rcpt_to = verb
                        _send("250 OK")
                elif verb == "DATA":
                    _send("354 End data with <CR><LF>.<CR><LF>")
                    body = b""
                    while True:
                        data = stream.readline()
                        if data.strip() == b".":
                            break
                        body += data
                    if rcpt_to and not self.server.refuse_recipient:
                        self.server.delivered.append((mail_from, rcpt_to, body))
                    _send("250 OK: queued as modulo-test")
                elif verb == "QUIT":
                    _send("221 Bye")
                    return
                else:
                    _send("250 OK")
        finally:
            with contextlib.suppress(OSError):
                active.close()


@pytest.fixture
def smtp_server(tls_material: tuple[str, str]) -> Generator[_SmtpServer, None, None]:
    server = _SmtpServer(tls_material[0], tls_material[1])
    server.start()
    try:
        yield server
    finally:
        server.stop()


def _settings(port: int) -> SimpleNamespace:
    return SimpleNamespace(
        smtp_host="127.0.0.1",
        smtp_port=port,
        smtp_timeout=2,
        smtp_username="",
        smtp_password="",
        email_from="ops@modulo.test",
    )


class TestCapNotificationsRealDelivery:
    def test_message_is_delivered_over_starttls(self, smtp_server: _SmtpServer) -> None:
        sent = send_email(
            _settings(smtp_server.port),
            ["analyst@modulo.test"],
            "Scheduled report ready",
            "<html><body>Your starship report is ready.</body></html>",
            body_text="Your starship report is ready.",
        )

        assert sent is True
        assert len(smtp_server.delivered) == 1
        _mail_from, rcpt_to, body = smtp_server.delivered[0]
        assert "rcpt to:<analyst@modulo.test>" in rcpt_to.lower()
        assert "Subject: Scheduled report ready" in body.decode()
        assert "Your starship report is ready." in body.decode()

    def test_stalled_first_connection_retries_and_succeeds(self, tls_material: tuple[str, str]) -> None:
        server = _SmtpServer(tls_material[0], tls_material[1], stall_first=True)
        server.start()
        try:
            sent = send_email(
                _settings(server.port),
                ["analyst@modulo.test"],
                "Retry works",
                "<p>delivered on second attempt</p>",
            )

            assert sent is True
            assert len(server.delivered) == 1
            assert "Retry works" in server.delivered[0][2].decode()
            assert server.stall_used is True
        finally:
            server.stop()

    def test_closed_port_raises_email_sending_error(self) -> None:
        closed = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        closed.close()

        with pytest.raises(EmailSendingError):
            send_email(
                _settings(port),
                ["analyst@modulo.test"],
                "Will not land",
                "<p>refused connection</p>",
            )

    def test_refused_recipient_surfaces_smtp_exception(self, tls_material: tuple[str, str]) -> None:
        server = _SmtpServer(tls_material[0], tls_material[1], refuse_recipient=True)
        server.start()
        # Python >= 3.13: SMTPException subclasses OSError, so send_email
        # retries and wraps the refusal in EmailSendingError. On 3.12 the
        # SMTPRecipientsRefused propagates unwrapped. Pin the current
        # interpreter's deterministic behaviour.
        expected = EmailSendingError if issubclass(smtplib.SMTPException, OSError) else smtplib.SMTPRecipientsRefused
        try:
            with pytest.raises(expected):
                send_email(
                    _settings(server.port),
                    ["ghost@modulo.test"],
                    "Bounced",
                    "<p>refused recipient</p>",
                )
            assert not server.delivered
        finally:
            server.stop()
