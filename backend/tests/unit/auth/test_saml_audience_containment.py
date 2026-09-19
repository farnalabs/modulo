"""Per-provider SAML audience containment + strict Destination pinning tests.

FAR-1000 spike established that python3-saml's audience validation rejects a
response minted for Provider A when presented at Provider B's handler — but
ONLY when the response includes an AudienceRestriction element.

FAR-1010 closes the gap: ModuloSamlAuth now enforces audience containment
itself (fail closed), on top of python3-saml's conditional check.
FAR-1011 fixed strict Destination/Recipient validation: the handler request
data now derives from the real ACS URL instead of a hardcoded ``localhost``.
"""

import base64
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch

import pytest
from lxml import etree
from onelogin.saml2.response import OneLogin_Saml2_Response
from onelogin.saml2.settings import OneLogin_Saml2_Settings
from onelogin.saml2.utils import OneLogin_Saml2_Utils

from modulo.auth.saml_handler import ModuloSamlAuth, SamlAuthError

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
    destination: str | None = None,
    recipient: str | None = None,
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
    if destination:
        r.set("Destination", destination)

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
    if recipient:
        scd.set("Recipient", recipient)

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
    """THE critical pinning test for per-provider SAML (raw python3-saml layer).

    Per-provider Entity IDs are the cross-org assertion-reuse containment
    the design leans on. This test demonstrates it works — and where it
    DOESN'T (raw python3-saml; the ModuloSamlAuth enforcement layer below
    closes the gap).
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

    def test_no_audience_restriction_raw_python3saml_layer_gap(self) -> None:
        """RAW LAYER (unchanged by our fix): python3-saml itself still accepts
        a response without AudienceRestriction at any provider — its check is
        conditional. The enforcement that closes this now lives in
        ModuloSamlAuth._enforce_audience_restriction (FAR-1010), tested in
        TestModuloSamlAuthAudienceEnforcement below.
        """
        provider_a = "urn:modulo:sp:org-a:provider-1"

        xml = _build_saml_response(audience=None, add_audience_restriction=False)
        b64 = _to_b64(xml)

        assert _validate(b64, _make_settings(provider_a)) is True


class TestModuloSamlAuthAudienceEnforcement:
    """FAR-1010: ModuloSamlAuth enforces audience containment, fail closed.

    Drives the REAL ModuloSamlAuth.process_response (full python3-saml
    validation active) with ONLY the XML signature step mocked —
    ``xmlsec.SignatureContext().sign()`` cannot produce round-trippable
    signatures on Windows, so we patch ``OneLogin_Saml2_Utils.validate_sign``
    and ``process_signed_elements`` (same approach as the FAR-1000 spike).
    The handler is built with a ``http://localhost`` ACS so the FAR-1011
    Destination fix does not intersect these audience tests either way.
    """

    IDP_METADATA = (
        '<?xml version="1.0"?>'
        '<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"'
        ' entityID="https://idp.example.com">'
        '  <md:IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">'
        "    <md:SingleSignOnService"
        '     Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"'
        '     Location="https://idp.example.com/sso"/>'
        "  </md:IDPSSODescriptor>"
        "</md:EntityDescriptor>"
    )

    ENTITY_A = "urn:modulo:sp:org-a:provider-1"
    ENTITY_B = "urn:modulo:sp:org-b:provider-2"

    def _handler(self, entity_id: str) -> ModuloSamlAuth:
        return ModuloSamlAuth(entity_id=entity_id, acs_url="http://localhost", idp_metadata_xml=self.IDP_METADATA)

    def _process(self, handler: ModuloSamlAuth, xml: etree._Element) -> dict[str, Any]:
        b64 = _to_b64(xml)
        resp_elem = f"{{{NS_SAMLP}}}Response"
        with (
            patch.object(OneLogin_Saml2_Utils, "validate_sign", return_value=True),
            patch.object(OneLogin_Saml2_Response, "process_signed_elements", return_value=[resp_elem]),
        ):
            return handler.process_response(b64)

    def test_no_audience_restriction_rejected(self) -> None:
        """FAR-1010 (was the documented gap, now enforced): a response with NO
        AudienceRestriction is REJECTED by the Modulo handler.

        NOTE: the pre-FAR-1010 spike test asserted this was ACCEPTED — that
        test documented the gap on purpose. The assertion has been inverted
        deliberately: the gap is now closed in code, so the test pins the
        fix. This is the point of the change, not a weakening.
        """
        xml = _build_saml_response(audience=None, add_audience_restriction=False)
        with pytest.raises(SamlAuthError, match="missing an AudienceRestriction"):
            self._process(self._handler(self.ENTITY_A), xml)

    def test_matching_audience_accepted(self) -> None:
        xml = _build_saml_response(audience=self.ENTITY_A, add_audience_restriction=True)
        result = self._process(self._handler(self.ENTITY_A), xml)
        assert result["name_id"] == "user@example.com"

    def test_mismatched_audience_rejected(self) -> None:
        xml = _build_saml_response(audience=self.ENTITY_A, add_audience_restriction=True)
        # python3-saml's own conditional audience check rejects this first
        # (strict mode); our FAR-1010 layer would also reject it — either
        # way the response must never authenticate.
        with pytest.raises(SamlAuthError, match="SAML response validation failed"):
            self._process(self._handler(self.ENTITY_B), xml)

    def test_mismatch_rejected_by_enforcement_layer(self) -> None:
        """FAR-1010: the enforcement layer's OWN mismatch branch rejects a
        response whose AudienceRestriction does not include the resolved
        provider's Entity ID.

        Driven directly: on the full handler path python3-saml's strict-mode
        audience check rejects this response first, so this pins the
        fail-closed branch we own rather than python3-saml's.
        """
        xml = _build_saml_response(audience=self.ENTITY_A, add_audience_restriction=True)
        with pytest.raises(SamlAuthError, match="does not include SP entity ID"):
            ModuloSamlAuth._enforce_audience_restriction(_to_b64(xml), self.ENTITY_B)

    def test_unparseable_response_rejected_by_enforcement_layer(self) -> None:
        """FAR-1010: a response that cannot be parsed fails closed."""
        bad = base64.b64encode(b"this is not xml <<<").decode()
        with pytest.raises(SamlAuthError, match="audience could not be parsed"):
            ModuloSamlAuth._enforce_audience_restriction(bad, self.ENTITY_A)


class TestRequestDataAcsDerivation:
    """FAR-1011: ``_get_request_data`` derives the strict-mode current URL
    from the configured ACS URL, with a legacy localhost fallback."""

    def test_acs_url_derives_host_path_and_scheme(self) -> None:
        data = ModuloSamlAuth._get_request_data("https://app.example.com/api/v1/auth/saml/acs")
        assert data["http_host"] == "app.example.com"
        assert data["script_name"] == "/api/v1/auth/saml/acs"
        assert data["https"] == "on"

    def test_no_acs_url_falls_back_to_legacy_localhost(self) -> None:
        data = ModuloSamlAuth._get_request_data()
        assert data["http_host"] == "localhost"
        assert not data["script_name"]
        assert data["https"] == "off"


class TestStrictDestinationRealAcs:
    """FAR-1011: the REAL handler must accept Destination = the real ACS URL.

    Reproduction established 2026-09-19: with a real https ACS URL, the real
    ModuloSamlAuth.process_response REJECTED a response whose Destination
    matched the ACS URL exactly, because ``current_url`` was built from a
    hardcoded ``http_host="localhost"`` and python3-saml strict mode compares
    Destination/Recipient against it (``startswith`` fails for any real
    https host). Fix derives http_host/script_name/scheme from the ACS URL —
    strict XSD, conditions, Destination and Recipient checks stay ACTIVE.
    """

    IDP_METADATA = (
        '<?xml version="1.0"?>'
        '<md:EntityDescriptor xmlns:md="urn:oasis:names:tc:SAML:2.0:metadata"'
        ' entityID="https://idp.example.com">'
        '  <md:IDPSSODescriptor protocolSupportEnumeration="urn:oasis:names:tc:SAML:2.0:protocol">'
        "    <md:SingleSignOnService"
        '     Binding="urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"'
        '     Location="https://idp.example.com/sso"/>'
        "  </md:IDPSSODescriptor>"
        "</md:EntityDescriptor>"
    )

    ACS = "https://app.example.com/api/v1/auth/saml/acs"
    ENTITY = "urn:modulo:sp:org-a:provider-1"

    def _handler(self) -> ModuloSamlAuth:
        return ModuloSamlAuth(
            entity_id=self.ENTITY,
            acs_url=self.ACS,
            idp_metadata_xml=self.IDP_METADATA,
        )

    def _process(self, xml: etree._Element) -> dict[str, Any]:
        b64 = _to_b64(xml)
        resp_elem = f"{{{NS_SAMLP}}}Response"
        with (
            patch.object(OneLogin_Saml2_Utils, "validate_sign", return_value=True),
            patch.object(OneLogin_Saml2_Response, "process_signed_elements", return_value=[resp_elem]),
        ):
            return self._handler().process_response(b64)

    def test_real_acs_destination_and_recipient_accepted(self) -> None:
        """The pre-fix REJECTED case: a real IdP response with Destination and
        SubjectConfirmationData Recipient equal to the actual ACS URL must be
        ACCEPTED through the real handler."""
        xml = _build_saml_response(
            audience=self.ENTITY,
            add_audience_restriction=True,
            destination=self.ACS,
            recipient=self.ACS,
        )
        result = self._process(xml)
        assert result["name_id"] == "user@example.com"

    def test_real_acs_destination_absent_accepted(self) -> None:
        """Some IdPs legitimately omit Destination; strict mode skips the
        check and the response must still be accepted."""
        xml = _build_saml_response(audience=self.ENTITY, add_audience_restriction=True)
        result = self._process(xml)
        assert result["name_id"] == "user@example.com"

    def test_other_host_destination_rejected(self) -> None:
        xml = _build_saml_response(
            audience=self.ENTITY,
            add_audience_restriction=True,
            destination="https://evil.example.com/api/v1/auth/saml/acs",
        )
        with pytest.raises(SamlAuthError):
            self._process(xml)
