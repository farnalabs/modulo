# ORM Research – Async, Multi-Backend, UUID/JSON/RLS

> Compiled: 26 June 2026
> Sources: PyPI JSON API, GitHub REST API, ORM documentation

---

## 1. SQLAlchemy 2.0/2.1 – Async + MariaDB

### Version & Status

| Field | Value |
|---|---|
| **Latest version** | 2.0.51 (released 15 Jun 2026) |
| **GitHub stars** | 11,950 |
| **Open issues** | 205 |
| **Last commit** | 25 Jun 2026 (active) |
| **License** | MIT |
| **PyPI monthly downloads** | ~45M+ (most-downloaded Python lib) |
| **Python** | >=3.7 |

### Async MariaDB Support

**Short answer: No official MariaDB async driver exists.** SQLAlchemy's async support is through:

| Driver | MariaDB compatible? | Status |
|---|---|---|
| **aiomysql** | Partial – `aiomysql` is technically a MySQL driver. It works with MariaDB if you stick to MySQL-compatible features. **Not officially tested or supported** by the aiomysql project for MariaDB. |
| **asyncmy** | Rejected: GHSA-qhqw-rrw9-25rm affects every released version through 0.2.11 with no patched release. |
| **asyncpg** | PostgreSQL only. No relation to MariaDB. |
| **aiosqlite** | SQLite only. |

**Key issues with aiomysql on MariaDB:**
- Character set handling differs (utf8mb4 vs utf8)
- `information_schema` queries can return different results
- JSON column type handling differs
- UUID type support is absent in both MySQL and MariaDB (they have no native UUID column type – you store as CHAR(32/36) or BINARY(16))
- MariaDB 10.7+ has a native `UUID` data type, but `aiomysql` doesn't know about it

**Verdict:** If you need async MariaDB, you are in uncharted territory. SQLAlchemy 2.0's async extension (`create_async_engine`) supports `aiomysql`, but it is not tested against MariaDB. `asyncmy` is excluded because it has no release patched for GHSA-qhqw-rrw9-25rm. Expect subtle breakage with JSON, UUID, and connection pooling.

### Can you use the same models across asyncpg, aiomysql, aiosqlite with just a connection string change?

**Yes – mostly.** SQLAlchemy 2.0's declarative models are backend-agnostic at the model definition level:

```python
# Same model file – just swap the connection string
class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))
```

Connection string swap:

```python
# SQLite
engine = create_async_engine("sqlite+aiosqlite:///db.sqlite")
# PostgreSQL
engine = create_async_engine("postgresql+asyncpg://user:pass@host/db")
# MySQL (maybe MariaDB)
engine = create_async_engine("mysql+aiomysql://user:pass@host/db")
```

**But there are caveats:**

#### UUID columns cross-backend
- **PostgreSQL**: Has native `UUID` type. SQLAlchemy's `Uuid` type maps to `PG UUID`.
- **SQLite**: No native UUID. Stored as `BLOB` or `VARCHAR(32)` depending on the type variant used.
- **MySQL/MariaDB**: No native UUID (MariaDB 10.7+ has `UUID` but drivers ignore it). Stored as `CHAR(32)` or `BINARY(16)`.
- **SQLAlchemy `Uuid` type** (2.0.23+): The `Uuid` type handles this, defaulting to `CHAR(32)` on MySQL/SQLite and native `UUID` on PostgreSQL. You can force a variant:
  ```python
  from sqlalchemy import Uuid

  id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True))
  ```
  This **does** work cross-backend with the right driver support. MySQL/MariaDB will store as `CHAR(32)`.

#### JSON columns cross-backend
- **PostgreSQL**: Native `JSONB` (best) / `JSON`.
- **MySQL/MariaDB**: Native `JSON` type (since MySQL 5.7, MariaDB 10.2).
- **SQLite**: No native JSON. Stored as `TEXT` with JSON validation (since SQLite 3.9+).
- **SQLAlchemy `JSON` type**: Works across all three. PostgreSQL uses `JSONB` by default if you use `postgresql.JSONB`. SQLite stores as TEXT. MySQL/MariaDB use `JSON` column type.

#### RLS (Row Level Security)
- **PostgreSQL**: Native RLS (`CREATE POLICY`). SQLAlchemy has no ORM-level abstraction for RLS. You must use raw SQL or use a library like `sqlalchemy_pg_ext`.
- **MySQL/MariaDB**: No RLS equivalent (MySQL has row-level constraints via views, but not PostgreSQL-style policies).
- **SQLite**: No RLS.
- **Cross-backend RLS is not possible.** You would need to implement application-level row filtering.

### Summary: SQLAlchemy cross-backend matrix

| Feature | PostgreSQL | MySQL | MariaDB | SQLite |
|---|---|---|---|---|
| **Async driver** | asyncpg (excellent) | aiomysql (stable) | aiomysql (untested) | aiosqlite (excellent) |
| **Native UUID** | Yes | No | Partial (10.7+) | No |
| **SQLAlchemy UUID cross-backend** | Native UUID | CHAR(32) | CHAR(32) | CHAR(32)/BLOB |
| **Native JSON** | JSONB | JSON | JSON | TEXT |
| **RLS** | Yes | No | No | No |
| **One-line swap** | – | Mostly | Risky | Mostly |

---

## 2. sqlalchemy-utils

| Field | Value |
|---|---|
| **PyPI** | `SQLAlchemy-Utils` v0.42.1 |
| **GitHub** | `kvesteri/sqlalchemy-utils` |
| **Stars** | 1,339 |
| **Open issues** | 228 |
| **Last release** | 13 Dec 2025 |
| **Last commit** | 19 May 2026 |
| **Maintenance** | Low activity. 228 open issues suggests maintenance is lagging. |

### Does it provide cross-database UUID types?

**Yes.** `sqlalchemy-utils` provides `UUIDType` which is a cross-backend UUID column type:

```python
from sqlalchemy_utils import UUIDType
import uuid


class MyModel(Base):
    __tablename__ = "my_model"
    id = Column(UUIDType(binary=False), primary_key=True)
```

However:
- `UUIDType` predates SQLAlchemy 2.0's built-in `Uuid` type.
- With SQLAlchemy 2.0's native `Uuid` type (added in 2.0.23), `sqlalchemy-utils` is mostly redundant for UUID.
- `sqlalchemy-utils` also provides `JSONType`, `EmailType`, `IPAddressType`, `PasswordType`, `PhoneNumber`, `TimezoneType`, etc.
- The package is in **maintenance mode** – many of its features have been superseded by SQLAlchemy 2.0 native types or separate libraries.

---

## 3. Comprehensive ORM Comparison

### Legend
- ✅ = Full native support
- ⚠️ = Partial / hacky / driver-dependent
- ❌ = Not supported
- 🚫 = Abandoned / archived

### ORM Table

| ORM | Latest | Last Release | Async | Backends (Async) | Stars | Open Issues | Maintained? | UUID Cross-Backend | JSON Cross-Backend | RLS | One-Line Swap |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **SQLAlchemy** | 2.0.51 | 15 Jun 2026 | ✅ Native | PG (asyncpg), MySQL (aiomysql), SQLite (aiosqlite), MSSQL (aioodbc) | 11,950 | 205 | ✅ Very active | ✅ (Uuid type) | ✅ (JSON type) | ❌ (PG only, no abstraction) | ✅ Mostly |
| **Peewee** | 4.1.0 | 2026 | ✅ Native async | PG (asyncpg), MySQL (aiomysql), SQLite (aiosqlite) | 11,984 | 0 | ✅ Active | ⚠️ (UUIDField, stored as text on non-PG) | ⚠️ (JSONField, stored as text on SQLite) | ❌ | ✅ (db proxy) |
| **SQLModel** | 0.0.39 | 2026 | ✅ (via SA 2.0) | Same as SQLAlchemy (wraps SA) | 18,148 | 64 | ✅ Active | ✅ (via SA Uuid) | ✅ (via SA JSON) | ❌ (via SA) | ✅ (via SA) |
| **Tortoise ORM** | 1.1.7 | 2026 | ✅ Native | PG (asyncpg), MySQL (aiomysql), SQLite (aiosqlite), MSSQL (asyncodbc), Oracle | 5,593 | 519 | ✅ Active | ✅ (UUIDField) | ✅ (JSONField) | ❌ | ✅ (db_url swap) |
| **Piccolo** | 1.34.0 | 26 May 2026 | ✅ Native | PG (asyncpg), SQLite (aiosqlite) | 1,915 | 36 | ✅ Active | ✅ (UUID column) | ✅ (JSON column) | ❌ | ⚠️ (PG only for production) |
| **Ormar** | 0.26.0 | 2026 | ✅ (via SA + databases) | PG (asyncpg), MySQL (aiomysql), SQLite (aiosqlite) | ~600 (est) | N/A (rate-limited) | ✅ Active | ✅ (UUID field) | ✅ (JSON field) | ❌ | ✅ (via SA) |
| **Edgy** | 0.35.10 | 2026 | ✅ (via SQLAlchemy core) | PG (asyncpg), MySQL (aiomysql), SQLite (aiosqlite), MSSQL | 434 | 3 | ✅ Active | ✅ (UUIDField) | ✅ (JSONField) | ❌ | ✅ (connection string) |
| **Django ORM** | 6.0.6 | 2026 | ⚠️ (database sync, async views via ASGI but DB calls are sync)* | PG, MySQL, SQLite, Oracle | 87,997 | 454 | ✅ Very active | ⚠️ (django-uuidfield or custom) | ✅ (JSONField since 3.1) | ❌ | ⚠️ (settings change, but backend-specific features) |
| **Pony ORM** | 0.7.19 | 2026 | ❌ (sync only) | PG, MySQL, SQLite, Oracle | 3,823 | 358 | ⚠️ (low activity) | ⚠️ | ❌ (no JSON type) | ❌ | ⚠️ |
| **GINO** | 1.0.1 | 2022 (last commit) | ✅ (native, PG-only) | PG only (asyncpg) | 2,802 | 55 | 🚫 **Abandoned** (last commit Feb 2022) | ❌ | ✅ (JSONB only) | ❌ | 🚫 PG only |
| **Peewee** | 4.1.0 | 2026 | ✅ | PG (asyncpg), MySQL (aiomysql), SQLite (aiosqlite) | 11,984 | 0 | ✅ Active | ⚠️ | ⚠️ | ❌ | ✅ |

*Django 5.0+ has async ORM support but it's shallow – individual queries run via sync-to-async wrappers, not a true async driver stack.

### Detailed Per-ORM Notes

#### SQLAlchemy (2.0.51)
- **The gold standard** for multi-backend ORM work in Python.
- `Uuid` type (since 2.0.23) handles cross-backend UUID storage:
  - PG → native UUID
  - SQLite → CHAR(32) or BLOB
  - MySQL → CHAR(32)
- `JSON` / `JSONB` type works cross-backend.
- Async via `create_async_engine()` with the `[asyncio]` extra.
- **RLS**: No built-in abstraction. You must use `Session.execute(text("SET ..."))` or PostgreSQL advisory locks manually.
- **Gotcha**: MariaDB + aiomysql is unsupported territory. Use with extreme caution.
- **Alembic** for migrations – mature, well-documented.

#### Peewee (4.1.0)
- Single Python file. Minimal dependencies.
- Async via `playhouse.pwasyncio` – provides `AsyncPostgresqlDatabase`, `AsyncMySQLDatabase`, `AsyncSqliteDatabase`.
- UUID field stores as `VARCHAR` on non-PG backends.
- JSON field stores as `TEXT` on SQLite.
- **Zero open issues** – author is very responsive.
- **Gotcha**: The async layer is newer and less battle-tested. Some edge cases with connection pooling.

#### SQLModel (0.0.39)
- Thin layer over SQLAlchemy 2.0 + Pydantic.
- **18K stars** – very popular because of FastAPI.
- Async support comes entirely from SQLAlchemy 2.0 – same engine, same session patterns.
- Model definitions use Python type annotations (`id: int = Field(primary_key=True)`).
- **Gotchas**:
  - Still at version 0.0.x (pre-1.0). Breaking changes expected.
  - Tied to specific SQLAlchemy versions (`>=2.0.14,<2.1.0`).
  - The `session.exec()` pattern is SQLModel-specific, not standard SQLAlchemy.
  - No native UUID field type – you drop down to SQLAlchemy's `Uuid` mapped_column.
  - Pydantic v2+ compatibility was rocky for a while.

#### Tortoise ORM (1.1.7)
- **Django-inspired async-native ORM.** First-class async from day one.
- Supports PG, MySQL, SQLite, MSSQL, Oracle.
- Has `UUIDField` as a built-in field type.
- Has `JSONField` for JSON columns.
- Connection string swap: `sqlite://`, `postgres://`, `mysql://` – same models.
- Built-in migration system (Aerich-like CLI).
- **Gotchas**:
  - 519 open issues – project may be understaffed for its popularity.
  - Default branch is `develop`, not `main`.
  - Schema generation in production requires migration tool (built-in, but not as mature as Alembic).
  - Query API is Django-like (double-underscore filters), not SQLAlchemy-like.

#### Piccolo (1.34.0)
- **Fast, modern, user-friendly.** Full async support.
- Only supports PG and SQLite for production (no MySQL/MariaDB async driver support listed).
- Has `UUID` column type, `JSON` column type.
- Built-in migrations, admin GUI, authentication.
- **Gotcha**: PG-only for production deployments. No MySQL async support.
- Small but responsive team (36 open issues, 1.9K stars).

#### Ormar (0.26.0)
- Built on SQLAlchemy core + Pydantic. Async-native.
- Supports PG (asyncpg), MySQL (aiomysql), SQLite (aiosqlite).
- Has `UUID()`, `JSON()` fields built in.
- Uses `Alembic` for migrations.
- **Gotcha**: Beta-quality. Breaking changes on minor versions. Documentation is good but the API is less mature than alternatives.

#### Edgy (0.35.10)
- Newer ORM, built on SQLAlchemy core + Pydantic v2.
- Supports PG, MySQL, SQLite, MSSQL.
- Has `UUIDField`, `JSONField`.
- Built-in Alembic-based migrations.
- **Very few open issues** (3) – but also very few stars (434). Small community.
- **Gotcha**: Young project (started 2023). May have undiscovered edge cases.

#### Django ORM (6.0.6)
- **Most popular Python ORM by far** (88K stars).
- Async support in 5.0+ is still shallow – ORM queries run via `asgiref.sync.sync_to_async`. Not true async DB drivers.
- Standalone usage is possible but awkward (requires `django.setup()`, `DJANGO_SETTINGS_MODULE`).
- Multi-backend: change `DATABASES` setting.
- UUID: `UUIDField` (native PG, CHAR(32) elsewhere).
- JSON: `JSONField` (since Django 3.1).
- RLS: No abstraction.
- **Gotcha**: Heavy framework. Standalone ORM usage is not idiomatic. Migrations (`manage.py makemigrations`) work but the tooling assumes a Django project structure.

#### Pony ORM (0.7.19)
- **Unique generator-expression query syntax:** `select(p for p in Product if p.name.startswith('A'))`.
- **No async support.** Development seems to have stalled (358 open issues).
- No JSON type. Limited UUID support.
- **Recommendation:** Avoid for new projects.

#### GINO (1.0.1)
- **Abandoned.** Last commit Feb 2022. Last PyPI release is 1.0.1.
- PostgreSQL only. Built on SQLAlchemy <1.4 core.
- No longer compatible with SQLAlchemy 2.0.
- **Recommendation:** Do not use.

---

## 4. SQLAlchemy Cross-Backend Patterns

### UUID Column (Recommendations)

**SQLAlchemy 2.0+ way (preferred):**

```python
from sqlalchemy import Uuid, String
from sqlalchemy.orm import Mapped, mapped_column
import uuid


class Item(Base):
    __tablename__ = "items"

    # Cross-backend UUID – stores as CHAR(32) on MySQL/SQLite, native UUID on PG
    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)

    # Or force string-based UUID (works everywhere)
    id_str: Mapped[str] = mapped_column(String(32), primary_key=True)
```

**If using `sqlalchemy-utils`:**

```python
from sqlalchemy_utils import UUIDType


class Item(Base):
    id = Column(UUIDType(binary=False), primary_key=True)
```

But `sqlalchemy-utils` is in maintenance mode. Stick with SQLAlchemy's built-in `Uuid`.

### JSON Column (Recommendations)

```python
from sqlalchemy import JSON


class Item(Base):
    __tablename__ = "items"
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
```

**Backend behavior:**
- PG → `JSON` column (use `sqlalchemy.dialects.postgresql.JSONB` for indexed JSONB)
- MySQL/MariaDB → `JSON` column
- SQLite → `TEXT` column with JSON validation

### RLS (Row Level Security)

**No ORM-level cross-backend RLS exists.** If you need RLS:
- **PostgreSQL**: Use raw SQL policies and `SET session_prefs` before queries.
- **MySQL/MariaDB**: No RLS. Use application-level filtering or views.
- **SQLite**: No RLS.

For PG-only RLS with SQLAlchemy:

```python
# Enable RLS on table
from sqlalchemy import text

async with async_session() as session:
    await session.execute(text("SELECT set_config('app.current_tenant', :tid, false)"), {"tid": tenant_id})
    result = await session.execute(select(Item).where(Item.tenant_id == tenant_id))
```

---

## 5. Decision Matrix

| If you need... | Choose... |
|---|---|
| Most mature, proven, widely used | **SQLAlchemy 2.0** |
| FastAPI + Pydantic integration | **SQLModel** (wraps SA) or **SQLAlchemy 2.0** directly |
| True async-native from scratch | **Tortoise ORM** or **Piccolo** |
| Simplest, smallest ORM | **Peewee** |
| Django ecosystem | **Django ORM** (but standalone is painful) |
| Multi-backend production (PG + MySQL + SQLite) | **SQLAlchemy 2.0** or **Tortoise ORM** |
| Async MariaDB support | **None are well-tested.** Closest: SQLAlchemy 2.0 + aiomysql, but expect issues |
| RLS support | **PostgreSQL only.** Use SQLAlchemy + raw SQL policies |
| Zero dependencies / single file | **Peewee** |
| Modern, batteries-included | **Piccolo** or **Edgy** |

---

## 6. Key Takeaways

1. **SQLAlchemy 2.0 is the safest bet** for multi-backend async Python ORM work. Its `Uuid` and `JSON` types are genuinely cross-backend. But async MariaDB is not officially supported.

2. **Async MariaDB doesn't have a strong ecosystem.** `aiomysql` is not tested against MariaDB, and `asyncmy` is excluded due to its unpatched SQL-injection advisory. If MariaDB is a requirement, consider using synchronous `pymysql`/`mysqlclient` with SQLAlchemy 2.0's sync engine, or use Tortoise ORM (which lists async MySQL drivers but also doesn't specifically claim MariaDB support).

3. **No ORM abstracts RLS.** RLS is a PostgreSQL-only feature, and no ORM provides a cross-backend abstraction for it. You need application-level row filtering for cross-backend portability.

4. **SQLAlchemy's native `Uuid` type** (2.0.23+) makes `sqlalchemy-utils` mostly obsolete for UUID needs.

5. **SQLModel is not production-1.0** (v0.0.39). Breaking changes are expected. Use SQLAlchemy directly if you want stability.

6. **GINO is dead.** Don't start new projects with it.

7. **Piccolo and Tortoise are the most promising async-native alternatives** to SQLAlchemy, but Tortoise has a high open-issue count.
