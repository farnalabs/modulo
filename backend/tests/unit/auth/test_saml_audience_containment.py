"""SPIKE (FAR-1000): Per-provider SAML audience containment pinning test.

Demonstrates that python3-saml's audience validation rejects a response
minted for Provider A when presented at Provider B's handler — but ONLY
when the response includes an AudienceRestriction element.

This is the cross-org assertion-reuse containment the per-provider SAML
design depends on. It must be demonstrated, not assumed.
"""

import base64
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

from lxml import etree
from onelogin.saml2.response import OneLogin_Saml2_Response
from onelogin.saml2.settings import OneLogin_Saml2_Settings
from onelogin.saml2.utils import OneLogin_Saml2_Utils

NS_SAML = "urn:oasis:names:tc:SAML:2.0:assertion"
NS_SAMLP = "urn:oasis:names:tc:SAML:2.0:protocol"
NS_STATUS = "urn:oasis:names:tc:SAML:2.0:status"
S = f"{{{NS_SAML}}}"
N = f"{{{NS_SAMLP}}}"

_CERT_B64: str | None = None


def _get_cert_b64() -> str:
    global _CERT_B64
    if _CERT_B64 is None:
        import datetime as dt

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test IdP")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(dt.datetime.now(dt.UTC))
            .not_valid_after(dt.datetime.now(dt.UTC) + dt.timedelta(days=365))
            .sign(key, hashes.SHA256())
        )
        raw = cert.public_bytes(serialization.Encoding.PEM).decode()
        _CERT_B64 = "".join(line for line in raw.splitlines() if not line.startswith("-----"))
    return _CERT_B64


def _build_saml_response(
    *,
    audience: str | None = None,
    add_audience_restriction: bool = True,
    name_id_value: str = "user@example.com",
) -> etree._Element:
    """Build a minimal but XSD-compliant SAML Response."""
    now = datetime.now(UTC)
    instant = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    nooa = (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    nb = (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")

    dummy = etree.Element("root")
    r = etree.SubElement(dummy, f"{N}Response")
    r.set("ID", "_resp1")
    r.set("Version", "2.0")
    r.set("IssueInstant", instant)

    st = etree.SubElement(r, f"{N}Status")
    sc = etree.SubElement(st, f"{N}StatusCode")
    sc.set("Value", f"{NS_STATUS}:Success")

    a = etree.SubElement(r, f"{S}Assertion")
    a.set("ID", "_assert1")
    a.set("Version", "2.0")
    a.set("IssueInstant", instant)

    ai = etree.SubElement(a, f"{S}Issuer")
    ai.text = "https://idp.example.com"

    subj = etree.SubElement(a, f"{S}Subject")
    nid = etree.SubElement(subj, f"{S}NameID")
    nid.text = name_id_value
    nid.set("Format", "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress")

    sc2 = etree.SubElement(subj, f"{S}SubjectConfirmation")
    sc2.set("Method", "urn:oasis:names:tc:SAML:2.0:cm:bearer")
    scd = etree.SubElement(sc2, f"{S}SubjectConfirmationData")
    scd.set("NotOnOrAfter", nooa)
    scd.set("Recipient", "http://localhost")

    conds = etree.SubElement(a, f"{S}Conditions")
    conds.set("NotBefore", nb)
    conds.set("NotOnOrAfter", nooa)
    if add_audience_restriction and audience is not None:
        ar = etree.SubElement(conds, f"{S}AudienceRestriction")
        au = etree.SubElement(ar, f"{S}Audience")
        au.text = audience

    asn = etree.SubElement(a, f"{S}AuthnStatement")
    asn.set("AuthnInstant", instant)
    asn.set("SessionIndex", "_s1")
    cx = etree.SubElement(asn, f"{S}AuthnContext")
    cl = etree.SubElement(cx, f"{S}AuthnContextClassRef")
    cl.text = "urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport"

    astmt = etree.SubElement(a, f"{S}AttributeStatement")
    at = etree.SubElement(astmt, f"{S}Attribute")
    at.set("Name", "email")
    v = etree.SubElement(at, f"{S}AttributeValue")
    v.text = name_id_value

    dummy.remove(r)
    return r


def _to_b64(element: etree._Element) -> str:
    xml = etree.tostring(element, xml_declaration=True, encoding="UTF-8")
    return base64.b64encode(xml).decode()


def _make_settings(entity_id: str) -> dict[str, Any]:
    return {
        "strict": True,
        "sp": {
            "entityId": entity_id,
            "assertionConsumerService": {
                "url": "http://localhost",
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
            },
            "NameIDFormat": "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
        },
        "idp": {
            "entityId": "https://idp.example.com",
            "singleSignOnService": {
                "url": "https://idp.example.com/sso",
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect",
            },
            "x509cert": _get_cert_b64(),
        },
    }


def _validate(b64: str, settings: dict[str, Any], request_id: str | None = None) -> bool:
    """Validate a SAML response through python3-saml, mocking signature."""
    saml_settings = OneLogin_Saml2_Settings(settings)
    resp = OneLogin_Saml2_Response(saml_settings, b64)
    request_data = {
        "http_host": "localhost",
        "script_name": "",
        "get_data": {},
        "post_data": {"SAMLResponse": b64},
    }
    resp_elem = f"{{{NS_SAMLP}}}Response"
    with (
        patch.object(OneLogin_Saml2_Utils, "validate_sign", return_value=True),
        patch.object(resp, "process_signed_elements", return_value=[resp_elem]),
    ):
        return resp.is_valid(request_data, request_id=request_id, raise_exceptions=False)


class TestPerProviderAudienceContainment:
    """THE critical pinning test for per-provider SAML.

    Per-provider Entity IDs are the cross-org assertion-reuse containment
    the design leans on. This test demonstrates it works — and where it
    DOESN'T.
    """

    def test_response_for_provider_a_rejected_at_provider_b(self) -> None:
        """A response audience-restricted to Provider A MUST be rejected
        when presented at Provider B's handler."""
        provider_a = "urn:modulo:sp:org-a:provider-1"
        provider_b = "urn:modulo:sp:org-b:provider-2"

        xml = _build_saml_response(audience=provider_a, add_audience_restriction=True)
        b64 = _to_b64(xml)

        # Provider B's handler must reject this
        assert _validate(b64, _make_settings(provider_b)) is False

    def test_response_for_provider_a_accepted_at_provider_a(self) -> None:
        """A response audience-restricted to Provider A IS accepted at
        Provider A's handler."""
        provider_a = "urn:modulo:sp:org-a:provider-1"

        xml = _build_saml_response(audience=provider_a, add_audience_restriction=True)
        b64 = _to_b64(xml)

        assert _validate(b64, _make_settings(provider_a)) is True

    def test_no_audience_restriction_accepted_at_any_provider(self) -> None:
        """CRITICAL GAP: without AudienceRestriction, python3-saml accepts
        the response at ANY provider. The IdP MUST be configured to include
        AudienceRestriction for per-provider containment to work.

        This is NOT a bug in python3-saml — the SAML 2.0 spec allows
        AudienceRestriction to be absent. But per-provider containment
        REQUIRES it.
        """
        provider_a = "urn:modulo:sp:org-a:provider-1"
        provider_b = "urn:modulo:sp:org-b:provider-2"

        # Same IdP-signed response, but NO audience restriction
        xml = _build_saml_response(audience=None, add_audience_restriction=False)
        b64 = _to_b64(xml)

        # Both providers accept it — the containment fails
        assert _validate(b64, _make_settings(provider_a)) is True
        assert _validate(b64, _make_settings(provider_b)) is True
