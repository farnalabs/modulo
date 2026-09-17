"""One-time cassette recorder for npm and pypi connector tests.

To record (requires network access and a machine with valid SSL):

    cd backend
    VCR_RECORD_MODE=once uv run pytest tests/connectors/_record_cassettes.py -v

The committed cassettes in tests/cassettes/ are the replay source for CI.
"""
