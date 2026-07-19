"""Provider credential compartment backed by the operating-system vault.

Configuration and ordinary AetherState services retain opaque references only.  Raw values enter
through one Console save or an explicit environment variable and are resolved only when a provider
transport is about to construct its Authorization header.
"""
from __future__ import annotations

import logging
import os
import re
import uuid
from dataclasses import dataclass
from typing import Any, Callable

log = logging.getLogger("aetherstate.secret_store")

_SERVICE_NAME = "AetherState Provider Credentials"
_REFERENCE_RE = re.compile(r"^cred_[0-9a-f]{32}$")
_SECURE_BACKEND_MARKERS = ("windows", "macos", "secretservice", "kwallet")
_MAX_SECRET_CHARS = 16_384


class CredentialStoreError(RuntimeError):
    """A content-free credential storage or lookup failure."""


class CredentialStoreUnavailable(CredentialStoreError):
    """No approved operating-system credential vault is available."""


class CredentialReferenceError(CredentialStoreError):
    """An opaque credential reference is malformed or missing."""


def _approved_backend(backend: Any) -> Any | None:
    """Return an approved OS-vault backend, descending through keyring's chainer.

    File, plaintext, null, fail, and unknown third-party backends are deliberately refused.  A
    synthetic test backend may opt in with ``aetherstate_secure = True``.
    """
    if bool(getattr(backend, "aetherstate_secure", False)):
        return backend
    identity = f"{type(backend).__module__}.{type(backend).__name__}".lower()
    if "chainer" in identity:
        try:
            for candidate in backend.backends:
                approved = _approved_backend(candidate)
                if approved is not None:
                    return approved
        except Exception:
            return None
        return None
    if not any(marker in identity for marker in _SECURE_BACKEND_MARKERS):
        return None
    try:
        if float(backend.priority) <= 0:
            return None
    except Exception:
        return None
    return backend


class CredentialStore:
    """Small fail-closed wrapper around an approved keyring backend."""

    def __init__(self, backend: Any | None = None):
        self._backend = backend

    def _get_backend(self) -> Any:
        backend = self._backend
        if backend is None:
            try:
                import keyring

                backend = keyring.get_keyring()
            except Exception as exc:
                raise CredentialStoreUnavailable(
                    "secure credential storage is unavailable"
                ) from exc
        approved = _approved_backend(backend)
        if approved is None:
            raise CredentialStoreUnavailable("secure credential storage is unavailable")
        return approved

    @staticmethod
    def _validate_reference(reference: str) -> str:
        normalized = str(reference or "").strip()
        if not _REFERENCE_RE.fullmatch(normalized):
            raise CredentialReferenceError("credential reference is invalid")
        return normalized

    def put(self, value: str, *, reference: str = "") -> str:
        secret = str(value or "")
        if not secret or len(secret) > _MAX_SECRET_CHARS:
            raise CredentialStoreError("credential value is invalid")
        credential_ref = (
            self._validate_reference(reference)
            if reference
            else f"cred_{uuid.uuid4().hex}"
        )
        try:
            self._get_backend().set_password(_SERVICE_NAME, credential_ref, secret)
        except CredentialStoreError:
            raise
        except Exception as exc:
            raise CredentialStoreUnavailable(
                "secure credential storage is unavailable"
            ) from exc
        return credential_ref

    def get(self, reference: str) -> str:
        credential_ref = self._validate_reference(reference)
        try:
            value = self._get_backend().get_password(_SERVICE_NAME, credential_ref)
        except CredentialStoreError:
            raise
        except Exception as exc:
            raise CredentialStoreUnavailable(
                "secure credential storage is unavailable"
            ) from exc
        if not value:
            raise CredentialReferenceError("saved credential is unavailable")
        return str(value)

    def delete(self, reference: str) -> None:
        credential_ref = self._validate_reference(reference)
        try:
            self._get_backend().delete_password(_SERVICE_NAME, credential_ref)
        except CredentialStoreError:
            raise
        except Exception as exc:
            # Keyring backends use backend-specific exceptions for an already-missing item.
            # Confirm absence without ever including backend text or a secret in the error.
            try:
                if self._get_backend().get_password(_SERVICE_NAME, credential_ref) is None:
                    return
            except Exception:
                pass
            raise CredentialStoreUnavailable(
                "secure credential storage is unavailable"
            ) from exc


_default_store: CredentialStore | None = None


def default_credential_store() -> CredentialStore:
    global _default_store
    if _default_store is None:
        _default_store = CredentialStore()
    return _default_store


def has_configured_key(connection: Any) -> bool:
    return bool(
        str(getattr(connection, "api_key", "") or "")
        or str(getattr(connection, "credential_ref", "") or "")
    )


def resolve_api_key(connection: Any, store: CredentialStore | None = None) -> str:
    """Resolve one connection at the final provider boundary.

    ``api_key`` remains a transient compatibility field for environment injection and an unmigrated
    legacy file. New Console saves clear it immediately after vault storage.
    """
    transient = str(getattr(connection, "api_key", "") or "")
    if transient:
        return transient
    reference = str(getattr(connection, "credential_ref", "") or "")
    if not reference:
        return ""
    try:
        return (store or default_credential_store()).get(reference)
    except CredentialStoreError:
        log.warning("saved provider credential is unavailable")
        return ""


@dataclass(frozen=True)
class CredentialMigrationResult:
    migrated: int = 0
    failed: bool = False


def migrate_legacy_credentials(
    cfg: Any,
    persist: Callable[..., bool],
    *,
    store: CredentialStore | None = None,
) -> CredentialMigrationResult:
    """Move writable legacy plaintext config values into the OS vault atomically enough to recover.

    The source file remains the last usable copy until every vault write succeeds and sanitized
    config plus backup persistence completes. On failure, runtime objects are restored and newly
    created vault items are removed best-effort.
    """
    if not bool(getattr(cfg, "persistence_enabled", False)) or not str(
        getattr(cfg, "source_path", "") or ""
    ):
        return CredentialMigrationResult()

    candidates: list[Any] = []
    upstream = getattr(cfg, "upstream", None)
    if (
        upstream is not None
        and not os.environ.get("AETHERSTATE_UPSTREAM__API_KEY")
        and str(getattr(upstream, "api_key", "") or "")
        and not str(getattr(upstream, "credential_ref", "") or "")
    ):
        candidates.append(upstream)
    for endpoint in list(getattr(getattr(cfg, "assist", None), "endpoints", ()) or ()):
        if (
            str(getattr(endpoint, "api_key", "") or "")
            and not str(getattr(endpoint, "credential_ref", "") or "")
        ):
            candidates.append(endpoint)
    if not candidates:
        return CredentialMigrationResult()

    vault = store or default_credential_store()
    snapshots = [
        (item, str(item.api_key or ""), str(getattr(item, "credential_ref", "") or ""))
        for item in candidates
    ]
    created: list[str] = []
    try:
        for item, secret, _reference in snapshots:
            credential_ref = vault.put(secret)
            created.append(credential_ref)
            item.credential_ref = credential_ref
            item.api_key = ""
        if not bool(persist(cfg, refresh_backup=True)):
            raise CredentialStoreError("sanitized configuration could not be persisted")
        return CredentialMigrationResult(migrated=len(created))
    except Exception:
        for item, secret, reference in snapshots:
            item.api_key = secret
            item.credential_ref = reference
        for credential_ref in created:
            try:
                vault.delete(credential_ref)
            except CredentialStoreError:
                pass
        log.warning("legacy provider credential migration was not completed")
        return CredentialMigrationResult(failed=True)
