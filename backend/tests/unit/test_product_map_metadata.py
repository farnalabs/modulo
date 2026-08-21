"""Unit tests for product map frontmatter parsing.

Replaces `tools/tests/test-product-map-metadata.ps1`. The parsing functions now
live in `scripts/_product_map.py` (shared by `run_graph_validate.py` and
`run_graph_query.py`), so this pytest exercises the Python implementation
directly.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "scripts"))

from _product_map import get_prd_references, get_prd_sections


def test_parses_scalar_comma_separated_references():
    refs = get_prd_references("id: example\nprd: 6.2, 12\nstatus: partial")
    assert ",".join(refs) == "6.2,12"


def test_parses_block_list_references():
    frontmatter = "id: example\nprd:\n  - 9.2\n  - 9.4\nstatus: partial"
    refs = get_prd_references(frontmatter)
    assert ",".join(refs) == "9.2,9.4"


def test_preserves_explicit_na_reference():
    refs = get_prd_references("id: example\nprd: N/A\nstatus: partial")
    assert ",".join(refs) == "N/A"


def test_recognises_nested_numbered_prd_headings():
    metadata = get_prd_sections(["## 8. Features", "### 8.25 Error Tracking", "#### 8.25.1 Frontend Monitoring"])
    assert "8.25.1" in metadata["sections"]
    assert metadata["names"]["8.25.1"] == "Frontend Monitoring"
