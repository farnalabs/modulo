# Edge Case & Boundary Coverage

## Critical
- Vault path traversal via `_secret_path` — key like `../../other/secret` bypasses the path prefix and reads/writes/deletes secrets outside the intended `modulo/secrets/` namespace. `validate_key` only checks non-empty, with no restriction on `/`, `..`, or other path-sensitive characters (vault.py:93-94)

## Major
- Fernet `_read_org_id_from_session` bare `except Exception: pass` — swallows ALL DB errors (connection failure, syntax error, `ProgrammingError` on SQLite, `OperationalError` on broken connections) silently, then falls through to `session.info` which may also be empty, producing a misleading "RLS context not set" error instead of the actual DB error (fernet.py:110-111)
- Fernet `get_secret` does not guard against `None` encrypted_value — if the DB column is NULL, `self._fernet.decrypt(None)` raises `TypeError` which is not caught by any handler, producing a raw 500 (fernet.py:77)
- Vault AppRole auth failure during `_ensure_client` propagates as an unhandled exception — if AppRole login fails (bad credentials, expired secret, network error), the exception escapes before entering the try/except block, causing a 500 instead of a structured error (vault.py:82-86)
- Vault env vars (`VAULT_ADDR`, `VAULT_MOUNT_POINT`, `VAULT_PATH_PREFIX`) are used without `.strip()` — trailing whitespace in env var values causes silent connection failures or path mismatches with no validation feedback (vault.py:52-60)
- AWS `set_secret` TOCTOU window between `create_secret` and `update_secret` — if the secret is deleted by a concurrent caller between the `ResourceExistsException` catch and the `update_secret` call, the update raises an unhandled exception caught only by the generic `except Exception` wrapper with a misleading "unexpected error" message (aws.py:125-133)

## Minor
- AWS env vars (`AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`) are not stripped — trailing whitespace causes boto3 credential lookup failures or region parsing errors (aws.py:51-54)
- Vault and AWS error messages include raw exception text (`f"...: {exc}"`) — against the ABC contract "must not log or leak secret values in exception messages" (vault.py:117, aws.py:99, aws.py:137, aws.py:158)
- Vault `_secret_path` does not normalize path separators — if `_path_prefix` ends with `/` and key is normal, the resulting path has a double slash `prefix//key` (vault.py:93-94)
- AWS `set_secret` update call inside the `except ResourceExistsException` block has no dedicated error handling — if the update fails with its own `ResourceExistsException` (concurrent race) it is caught by the outer generic handler with a misleading "unexpected error" message (aws.py:126-133)
- Factory does not validate that `fernet_key` is valid base64 — invalid keys crash at `Fernet(fernet_key.encode())` with a raw `binascii.Error` or `ValueError` from cryptography, not a friendly configuration error (fernet.py:42, __init__.py:87)
- `validate_key` type annotation says `str` but no runtime guard against `None` — `None.strip()` raises `AttributeError` if a caller bypasses type hints (__init__.py:43)
