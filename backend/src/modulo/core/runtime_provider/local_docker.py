"""Compatibility import for the canonical async Docker runtime provider."""

from modulo.core.runtime_provider.docker import DockerRuntimeProvider

LocalDockerRuntimeProvider = DockerRuntimeProvider

__all__ = ["LocalDockerRuntimeProvider"]
