"""Pydantic config models for ``modulo apply`` (FAR-681, slice 1).

Mirrors the API-layer Create/Update shapes for the entity set covered by
this slice (schemas + versions, model_backends). Pipelines and triggers are
slices 2/3 and are intentionally absent here.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

API_VERSION_PREFIX = "modulo.dev/v"
API_VERSION_SUPPORTED_MAJOR = 1

ENV_REF_PATTERN = re.compile(r"^\$\{env:([A-Z_][A-Z0-9_]*)\}$")
SECRET_REF_PATTERN = re.compile(r"^secretref://\S+$")


class ApplyConfigError(ValueError):
    """Raised when an apply config is structurally/semantically invalid."""


def check_api_version(raw: str) -> None:
    """Gate the declared api_version.

    Major must match the supported major exactly (hard error). Any minor
    suffix is accepted leniently.
    """
    if not isinstance(raw, str) or not raw.startswith(API_VERSION_PREFIX):
        msg = f"api_version must start with {API_VERSION_PREFIX!r}, got {raw!r}"
        raise ApplyConfigError(msg)
    major_part = raw[len(API_VERSION_PREFIX) :].split(".", maxsplit=1)[0]
    try:
        declared_major = int(major_part)
    except ValueError:
        msg = f"api_version {raw!r} is not parseable"
        raise ApplyConfigError(msg) from None
    if declared_major != API_VERSION_SUPPORTED_MAJOR:
        msg = (
            "api_version major mismatch: config declares modulo.dev/v"
            f"{declared_major}, this client supports modulo.dev/v"
            f"{API_VERSION_SUPPORTED_MAJOR}"
        )
        raise ApplyConfigError(msg)


class ApplyVersionSpec(BaseModel):
    """A schema version requested inside a SchemaEntity."""

    model_config = ConfigDict(extra="forbid")

    version: str = Field(min_length=1, max_length=64)
    version_number: int = Field(ge=0)
    definition_json: dict[str, Any] = Field(min_length=1)
    published: bool = False

    def managed_view(self) -> dict[str, Any]:
        """Canonical managed-field view used for hashing (version + number included)."""
        return {
            "version": self.version,
            "version_number": self.version_number,
            "definition_json": self.definition_json,
            "published": self.published,
        }


class SchemaEntity(BaseModel):
    """A declarative schema (mirrors SchemaCreate + SchemaCreateVersionCreate)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=2000)
    abstract_name: str | None = None
    versions: list[ApplyVersionSpec] = Field(default_factory=list)

    def managed_view(self) -> dict[str, Any]:
        """Canonical managed-field view used for hashing (name excluded)."""
        return {
            "description": self.description,
            "abstract_name": self.abstract_name,
            "versions": [v.managed_view() for v in sorted(self.versions, key=lambda s: (s.version, s.version_number))],
        }


class ModelBackendEntity(BaseModel):
    """A declarative model backend (mirrors ModelBackendCreate).

    ``api_key`` is write-only and MUST be a reference:
    ``${env:VAR}`` (resolved client-side at apply time) or ``secretref://<key>``
    (passed through unresolved to the SecretsBackend store). Inline secret
    values are forbidden.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    display_name: str = Field(min_length=1, max_length=255)
    provider: str = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=128)
    api_key: str = Field(min_length=1)
    default_params: dict[str, Any] = Field(default_factory=dict)
    visibility: str = Field(default="org")
    tier: Literal["native", "preview", "in_dev"] = "native"

    @field_validator("api_key")
    @classmethod
    def _api_key_must_be_ref(cls, value: str) -> str:
        if not ENV_REF_PATTERN.match(value) and not SECRET_REF_PATTERN.match(value):
            msg = (
                "api_key must be a reference (${env:VAR_NAME} or "
                f"secretref://<key>), got inline value {value[:4]!r}... "
                "inline secret values are forbidden"
            )
            raise ValueError(msg)
        return value

    def env_ref_var(self) -> str | None:
        """Return the env var name when api_key is an ${env:...} ref, else None."""
        match = ENV_REF_PATTERN.match(self.api_key)
        return match.group(1) if match else None

    def managed_view(self, *, include_api_key: bool = False) -> dict[str, Any]:
        """Canonical managed-field view used for hashing.

        ``api_key`` is write-only and is never included by default.
        """
        payload = {
            "display_name": self.display_name,
            "provider": self.provider,
            "model_id": self.model_id,
            "default_params": self.default_params,
            "visibility": self.visibility,
            "tier": self.tier,
        }
        if include_api_key:
            payload["api_key"] = self.api_key
        return payload


class EntitySet(BaseModel):
    """Entities declared for a single apply document."""

    model_config = ConfigDict(extra="forbid")

    schemas: list[SchemaEntity] = Field(default_factory=list)
    model_backends: list[ModelBackendEntity] = Field(default_factory=list)


class ApplyConfig(BaseModel):
    """Top-level document shape. api_version gate enforced by a validator."""

    model_config = ConfigDict(extra="forbid")

    api_version: str
    entities: EntitySet = Field(default_factory=EntitySet)

    @model_validator(mode="after")
    def _gate_api_version(self) -> ApplyConfig:
        check_api_version(self.api_version)
        return self

    @model_validator(mode="after")
    def _unique_names_per_kind(self) -> ApplyConfig:
        schema_names = [s.name for s in self.entities.schemas]
        backend_names = [b.name for b in self.entities.model_backends]
        for label, names in (("schemas", schema_names), ("model_backends", backend_names)):
            seen: set[str] = set()
            for name in names:
                if name in seen:
                    msg = f"duplicate {label} entity {name!r} in apply config"
                    raise ApplyConfigError(msg)
                seen.add(name)
        return self

    def merge_entities(self, other: ApplyConfig) -> ApplyConfig:
        """Combine entities from another document (for multi-doc YAML).

        Raises ApplyConfigError on api_version major mismatch or duplicate
        entity names of the same kind.
        """
        if self.api_version != other.api_version:
            msg = f"api_version differs across YAML documents: {self.api_version!r} vs {other.api_version!r}"
            raise ApplyConfigError(msg)
        merged_entities = EntitySet(
            schemas=[*self.entities.schemas, *other.entities.schemas],
            model_backends=[
                *self.entities.model_backends,
                *other.entities.model_backends,
            ],
        )
        # Collect duplicate names across the merged set up front (the model
        # validators only check single documents at construction time).
        for label, names in (
            ("schemas", [s.name for s in merged_entities.schemas]),
            ("model_backends", [b.name for b in merged_entities.model_backends]),
        ):
            if len(names) != len(set(names)):
                msg = f"duplicate {label} entity across YAML documents"
                raise ApplyConfigError(msg)
        return ApplyConfig(api_version=self.api_version, entities=merged_entities)
