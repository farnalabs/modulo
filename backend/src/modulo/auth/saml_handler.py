"""SAML 2.0 handling using python3-saml with XML signature verification.

python3-saml is declared in pyproject.toml but was never imported or used.
This module wraps OneLogin_Saml2_Auth to provide AuthnRequest generation
and SAML Response parsing with full XML digital signature verification.
"""

import base64
import logging
from typing import Any
from urllib.parse import urlsplit

from defusedxml import ElementTree
from onelogin.saml2.auth import OneLogin_Saml2_Auth
from onelogin.saml2.idp_metadata_parser import OneLogin_Saml2_IdPMetadataParser
from onelogin.saml2.settings import OneLogin_Saml2_Settings

_log = logging.getLogger(__name__)

_SAML_ASSERTION_NS = "{urn:oasis:names:tc:SAML:2.0:assertion}"


class SamlAuthError(Exception):
    """Raised when SAML authentication or validation fails."""


class ModuloSamlAuth:
    """Wrapper around python3-saml for Modulo's SAML SSO flow.

    Handles AuthnRequest generation (with optional SP signing) and SAML
    Response signature verification using the IdP's X.509 certificate from
    metadata. This replaces the previous implementation that used raw
    defusedxml.ElementTree parsing without any XML signature validation.
    """

    def __init__(
        self,
        entity_id: str,
        acs_url: str,
        idp_metadata_xml: str,
        sp_private_key: str | None = None,
        sp_x509_cert: str | None = None,
    ) -> None:
        self._settings_dict = self._build_settings_dict(
            entity_id=entity_id,
            acs_url=acs_url,
            idp_metadata_xml=idp_metadata_xml,
            sp_private_key=sp_private_key,
            sp_x509_cert=sp_x509_cert,
        )
        self._entity_id = entity_id
        self._acs_url = acs_url

    @staticmethod
    def _build_settings_dict(
        entity_id: str,
        acs_url: str,
        idp_metadata_xml: str,
        sp_private_key: str | None = None,
        sp_x509_cert: str | None = None,
    ) -> dict[str, Any]:
        """Build a python3-saml settings dictionary from IdP metadata XML.

        Uses OneLogin_Saml2_IdPMetadataParser to extract IdP SSO URL,
        entity ID, and X.509 signing certificate from the metadata XML.
        """
        parsed = OneLogin_Saml2_IdPMetadataParser.parse(idp_metadata_xml)
        idp_settings = parsed.get("idp", {})

        sp_settings: dict[str, Any] = {
            "entityId": entity_id,
            "assertionConsumerService": {
                "url": acs_url,
                "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST",
            },
            "NameIDFormat": "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
        }

        if sp_private_key:
            sp_settings["privateKey"] = sp_private_key
        if sp_x509_cert:
            sp_settings["x509cert"] = sp_x509_cert

        return {
            "sp": sp_settings,
            "idp": idp_settings,
        }

    @staticmethod
    def _get_request_data(
        acs_url: str | None = None,
        query_params: dict[str, str] | None = None,
        post_data: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Build request data dict in the format python3-saml expects.

        python3-saml was designed for Django/Flask where request data comes
        from the framework's request object. We construct the equivalent
        dict directly.

        When ``acs_url`` is given, ``http_host``/``script_name``/``https`` are
        derived from it so python3-saml's strict-mode ``current_url`` equals
        the real public ACS URL (FAR-1011). Under strict mode python3-saml
        compares the Response ``Destination`` and SubjectConfirmation
        ``Recipient`` against the URL reconstructed from these keys — a
        hardcoded ``localhost`` host made any real-IdP response (Destination
        = the actual ACS URL) fail with ``invalid_response``. Deriving from
        the ACS URL keeps strict XSD, conditions, Destination and Recipient
        checks ACTIVE rather than disabling strict mode.
        """
        if acs_url:
            parts = urlsplit(acs_url)
            http_host = parts.netloc
            script_name = parts.path
            https_flag = "on" if parts.scheme == "https" else "off"
        else:
            # Legacy default used before FAR-1011: satisfies python3-saml's
            # request-dict shape when no ACS URL is available.
            http_host = "localhost"
            script_name = ""
            https_flag = "off"
        return {
            "http_host": http_host,
            "script_name": script_name,
            "https": https_flag,
            "get_data": query_params or {},
            "post_data": post_data or {},
        }

    def get_auth_url(self) -> str:
        """Generate an AuthnRequest and return the IdP SSO redirect URL.

        python3-saml handles:
        - AuthnRequest XML construction with proper namespacing
        - IssueInstant and ID generation
        - Optional signature generation (if SP private key is configured)

        Returns:
            The IdP single sign-on URL with the SAMLRequest parameter.

        """
        auth = OneLogin_Saml2_Auth(
            self._get_request_data(self._acs_url),
            self._settings_dict,
        )
        return auth.login()  # type: ignore[no-any-return]

    def process_response(self, saml_response: str) -> dict[str, Any]:
        """Validate a SAML Response including XML signature verification.

        python3-saml validates:
        - XML digital signature using the IdP's X.509 certificate from metadata
        - Response and assertion conditions (NotBefore, NotOnOrAfter)
        - Audience restriction (matches SP entity ID)
        - Destination (matches ACS URL)
        - Subject confirmation data
        - XSD schema (strict mode, which python3-saml defaults to True)

        Args:
            saml_response: The base64-encoded SAML Response XML from the IdP.

        Returns:
            A dict with 'name_id' (str) and 'attributes' (dict of
            attribute_name -> list of values).

        Raises:
            SamlAuthError: If signature validation, conditions checks, audience
                containment (FAR-1010) or any other SAML processing step fails.

        """
        auth = OneLogin_Saml2_Auth(
            self._get_request_data(
                self._acs_url,
                post_data={"SAMLResponse": saml_response},
            ),
            self._settings_dict,
        )
        auth.process_response()

        errors = auth.get_errors()
        if errors:
            reason = auth.get_last_error_reason() if hasattr(auth, "get_last_error_reason") else ""
            _log.warning(
                "saml.process_response_failed",
                extra={"errors": errors, "reason": reason},
            )
            raise SamlAuthError(f"SAML response validation failed: {'; '.join(errors)}")

        if not auth.is_authenticated():
            _log.warning("saml.not_authenticated_after_validation")
            raise SamlAuthError("SAML authentication failed: user not authenticated after response processing")

        self._enforce_audience_restriction(saml_response, self._entity_id)

        return {
            "name_id": auth.get_nameid() or "",
            "attributes": auth.get_attributes() or {},
        }

    @staticmethod
    def _enforce_audience_restriction(saml_response: str, entity_id: str) -> None:
        """FAR-1010: fail closed when an assertion has no audience containment.

        python3-saml's audience check is conditional — it only runs when an
        AudienceRestriction is present, so a response WITHOUT one is accepted
        at ANY provider. Cross-org assertion reuse (minted for org A,
        replayed against org B) is then a property of each customer's IdP
        configuration rather than of our code. We enforce it ourselves:
        every assertion in the response MUST carry an AudienceRestriction
        whose Audience includes the resolved provider's SP Entity ID.

        Back-compat: some IdPs omit AudienceRestriction. This is fail CLOSED
        by design — no escape hatch. The remediation is IdP configuration:
        set the IdP's audience/recipient to the SP Entity ID, which the SP
        metadata route (/api/v1/auth/saml/metadata) already emits. Doing so
        is also required by the per-org SAML design (FAR-1000).

        Runs AFTER python3-saml's own validation, so signature verification
        (which parses the response) has already succeeded; a parse failure
        here is therefore impossible in practice and fails closed anyway.
        """
        try:
            raw = base64.b64decode(saml_response, validate=False)
            root = ElementTree.fromstring(raw)
        except (ValueError, ElementTree.ParseError) as exc:
            raise SamlAuthError("SAML response rejected: assertion audience could not be parsed") from exc

        # Fail closed when there is no plaintext Assertion to inspect. An
        # encrypted-only response (an ``EncryptedAssertion`` element, which
        # python3-saml decrypts internally when an SP private key is
        # configured) yields zero matches here; iterating zero times would
        # ACCEPT the response with no audience containment — the exact escape
        # hatch this method exists to close. Modulo does not configure SP
        # decryption, so such a response cannot be audience-checked and is
        # rejected rather than trusted.
        assertions = list(root.iter(f"{_SAML_ASSERTION_NS}Assertion"))
        if not assertions:
            _log.warning("sso.saml_assertion_missing", extra={"entity_id": entity_id})
            raise SamlAuthError(
                "SAML response rejected: no plaintext Assertion found — cannot enforce "
                f"audience containment for SP entity ID {entity_id!r} "
                "(encrypted-only or assertion-less response)"
            )
        for assertion in assertions:
            audiences = [
                (aud.text or "").strip()
                for aud in assertion.findall(
                    f"{_SAML_ASSERTION_NS}Conditions/{_SAML_ASSERTION_NS}AudienceRestriction"
                    f"/{_SAML_ASSERTION_NS}Audience"
                )
            ]
            if not any(audiences):
                _log.warning("sso.saml_audience_restriction_missing", extra={"entity_id": entity_id})
                raise SamlAuthError(
                    "SAML response rejected: assertion is missing an AudienceRestriction "
                    f"for SP entity ID {entity_id!r}; configure the IdP to set the "
                    "audience to the SP Entity ID (see SP metadata)"
                )
            if entity_id not in audiences:
                _log.warning(
                    "sso.saml_audience_mismatch",
                    extra={"entity_id": entity_id, "audiences": audiences},
                )
                raise SamlAuthError(f"SAML response audience {audiences} does not include SP entity ID {entity_id!r}")

    def get_sp_metadata(self) -> str:
        """Return SP metadata XML for IdP configuration."""
        saml_settings = OneLogin_Saml2_Settings(self._settings_dict)
        return saml_settings.get_sp_metadata()  # type: ignore[no-any-return]
