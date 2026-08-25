# Input Validation Guide

## Principles

Every FastAPI endpoint must validate all inputs at the boundary. Trust nothing
from the wire: not path params, not query params, not the request body.

1. **Body**: Always use a Pydantic `BaseModel`. Never access `request.json()`,
   `request.form()`, or `request.body()` directly in a route handler.
2. **Query params**: Always use `fastapi.Query()` with appropriate constraints.
3. **Path params**: Always validate type (e.g. `uuid.UUID`). For string path
   params, add `min_length`/`max_length` where the domain allows.
4. **Response models**: Every GET endpoint that returns structured data should
   declare `response_model=` for API docs and type safety.

## String Length Bounds Policy

| Context | Min | Max |
|---|---|---|
| Name / display_name | 1 | 255 |
| Slug | 1 | 255 |
| Description | None | 2000 |
| URL | 1 | 2048 |
| Provider / model_id / connector_type_id | 1 | 128 |
| Email | 1 | 320 |
| Image ref | 1 | 500 |

Nullable optional strings omit `min_length` so `None` is accepted.

## Numeric Range Policy

| Context | Constraint |
|---|---|
| Page number | `ge=1` |
| Page size | `ge=1, le=100` (default 20) |
| Timeout seconds | `ge=60, le=86400` |
| Token budget | `ge=0` |
| Weight | `ge=0` |
| Claim expiry minutes | `ge=1, le=1440` |
| Export page size | `ge=1, le=1000` |

## Enum / Pattern Validation

Use `pattern=` on `str` fields with a regex that enumerates valid values:

```python
visibility: str = Field(default="org", pattern=r"^(org|team)$")
```

Or use `Literal` / `Enum` types for closed sets of string values.

## Pydantic Model Conventions

- All create models: make required fields have `...` as default, optionals have
  `None`.
- All update models: wrap every field in `| None = None` for partial updates.
- Use `Field(min_length=1)` for required non-empty strings.
- Use `Field(ge=1)` for positive integers.
- Use `model_config = {"from_attributes": True}` on response models that are
  constructed from SQLAlchemy ORM objects.

## Rejecting Raw Request Bodies

Never read `request.json()` or `request.form()` in a route handler. Always
define a Pydantic body model and accept it as a function parameter:

```python
# BAD
async def handler(request: Request):
    data = await request.json()


# GOOD
class MyRequest(BaseModel):
    name: str = Field(min_length=1)


async def handler(body: MyRequest): ...
```

The only exceptions are:
- Webhook receivers that proxy raw bytes (must still validate the `dict` shape
  after parsing).
- SAML ACS POST handlers that receive IdP form data (validated by the SAML
  library, not by Pydantic).
