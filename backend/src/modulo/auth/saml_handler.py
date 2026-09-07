"""SAML 2.0 handling using python3-saml with XML signature verification.

python3-saml is declared in pyproject.toml but was never imported or used.
This module wraps OneLogin_Saml2_Auth to provide AuthnRequest generation
and SAML Response parsing with full XML digital signature verification.
"""

import logging
from typing import Any

from onelogin.saml2.auth import OneLogin_Saml2_Auth
from onelogin.saml2.idp_metadata_parser import OneLogin_Saml2_IdPMetadataParser
from onelogin.saml2.settings import OneLogin_Saml2_Settings

_log = logging.getLogger(__name__)


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
        query_params: dict[str, str] | None = None,
        post_data: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Build request data dict in the format python3-saml expects.

        python3-saml was designed for Django/Flask where request data comes
        from the framework's request object. We construct the equivalent
        dict directly.
        """
        return {
            "http_host": "localhost",
            "script_name": "",
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
            self._get_request_data(),
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

        Args:
            saml_response: The base64-encoded SAML Response XML from the IdP.

        Returns:
            A dict with 'name_id' (str) and 'attributes' (dict of
            attribute_name -> list of values).

        Raises:
            SamlAuthError: If signature validation, conditions checks, or
                any other SAML processing step fails.

        """
        auth = OneLogin_Saml2_Auth(
            self._get_request_data(post_data={"SAMLResponse": saml_response}),
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

        return {
            "name_id": auth.get_nameid() or "",
            "attributes": auth.get_attributes() or {},
        }

    def get_sp_metadata(self) -> str:
        """Return SP metadata XML for IdP configuration."""
        saml_settings = OneLogin_Saml2_Settings(self._settings_dict)
        return saml_settings.get_sp_metadata()  # type: ignore[no-any-return]
