"""Shared limits/constants for the cost breakdown engine.

These are DEFAULTS ONLY. Runtime reads flow through ``get_settings()`` so an
env override of a Settings knob moves the boundary EVERYWHERE (a knob's env
override is asserted by the 8g test — the constants and the knobs must not
drift apart). ``constants.py`` holds the canonical names; the corresponding
Settings knobs in ``modulo.settings`` are the runtime authority.
"""

from decimal import Decimal

# --- formula engine bounds ---
MAX_FORMULA_LENGTH = 256
MAX_FORMULA_DEPTH = 8

# --- money ceilings (the TRUE column caps, kept here so tests + validation
# reference one canonical value) ---
# runs.total_cost_usd / org_daily_run_counts.total_spend_usd Numeric(14,6)
COST_COLUMN_CAP = Decimal("99999999.999999")
# cost_components.rate_usd Numeric(18,6)
RATE_COLUMN_CAP = Decimal("999999999999.999999")
# Trigger.daily_spend_limit Numeric(12,4)
TRIGGER_LIMIT_CAP = Decimal("99999999.9999")

# --- self-report clamps (defaults; Settings knobs are the runtime authority) ---
MAX_SELF_REPORTED_USD = Decimal("10000.0")
MAX_REPORTABLE_USD_MIN = Decimal("0.000001")
# The SINGLE canonical name for the ABOVE-BAND clamp ceiling — the TOP OF THE
# SANITY BAND, the trust boundary for self-reported model cost. Shared with
# devtools ``_common.py`` (the dogfood reader applies the same clamp via this
# constant — now redundant but harmless; the backend extraction boundary is
# the single enforcement point). The dual name ``BAND_ABOVE_CEILING`` is
# dropped.
MAX_REPORTABLE_BAND_USD = Decimal("50.0")
# Value-checked across the two repos: CI asserts this constant == Decimal("50.0")
# in BOTH backend constants.py AND devtools _common.py.

# The pinned plausible node count used by the near-ceiling first-finalization
# check (MAX_SELF_REPORTED_USD x PLAUSIBLE_NODE_COUNT).
PLAUSIBLE_NODE_COUNT = 100

# The pinned node-type literal used by the node-type map derivation AND the
# self-report classification. A wrong literal would silently disable
# self-reporting for all sandbox nodes.
NODE_TYPE_SANDBOX_AGENT = "sandbox_agent"

# --- CRUD bounds ---
MAX_RATE_USD = Decimal("100000.0")
MAX_COMPONENTS_PER_ORG = 50
MAX_NAME_LENGTH = 64
MAX_DISPLAY_NAME_LENGTH = 128

# --- breakdown serialization bounds ---
MAX_BREAKDOWN_BASIS_SIZE = 2048
# raw_reported display clamp (1e6) applied at serialization so the UI money
# line cannot render 1e300; the raw value stays in the stored basis for audit.
RAW_REPORTED_DISPLAY_CLAMP = Decimal("1000000.0")
# Per-entry serialized amount_usd string clamp (never "1E+40").
AMOUNT_USD_STRING_CLAMP = "99999999.999999"
# The total_clamped marker entry prefixed to the breakdown when the summed
# total is flat-clamped to the column capacity.
TOTAL_CLAMPED_MARKER = {"total_clamped": True, "amount_usd": "0.000000"}
