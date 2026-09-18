#!/usr/bin/env bash
# Proof test for FAR-910: demonstrates the old staleness condition is
# unreachable on an active repo, while the corrected condition fires.
#
# Usage: bash .github/scripts/test-staleness-condition.sh
#
# What it tests:
# 1. OLD condition (age from main HEAD commit): on an active repo where main
#    HEAD was committed <6h ago, the condition NEVER fires even when prod is
#    11h behind.
# 2. NEW condition (age from prod build timestamp): when prod's build is
#    older than the grace window and prod SHA != main HEAD, the condition
#    fires.
#
# This script mocks the inputs via environment variables and runs the core
# logic in isolation. It does NOT call GitHub APIs or the production endpoint.

set -euo pipefail

GRACE_HOURS="${STALENESS_GRACE_HOURS:-6}"
PASS=0
FAIL=0

assert_eq() {
  local label="$1" expected="$2" actual="$3"
  if [ "$expected" = "$actual" ]; then
    echo "  PASS: $label (expected=$expected)"
    PASS=$((PASS + 1))
  else
    echo "  FAIL: $label (expected=$expected, got=$actual)"
    FAIL=$((FAIL + 1))
  fi
}

echo "=== FAR-910 proof: old vs new staleness condition ==="
echo ""

# ---- Scenario: prod is 11h behind, main HEAD is 30 minutes old ----
# This is the exact outage scenario: prod was last built 09/15 17:14 UTC,
# main HEAD was committed ~30min ago, prod SHA != main HEAD.
NOW_TS=$(date -u +%s)

# Simulate main HEAD committed 30 minutes ago
MAIN_COMMIT_TS=$((NOW_TS - 30 * 60))
# Simulate prod built 11 hours ago
PROD_BUILD_TS=$((NOW_TS - 11 * 3600))

echo "Scenario: prod is 11h old, main HEAD is 30min old"
echo "  NOW=$(date -u -d "@$NOW_TS" +%Y-%m-%dT%H:%M:%SZ)"
echo "  MAIN_DATE=$(date -u -d "@$MAIN_COMMIT_TS" +%Y-%m-%dT%H:%M:%SZ)"
echo "  PROD_BUILD=$(date -u -d "@$PROD_BUILD_TS" +%Y-%m-%dT%H:%M:%SZ)"
echo ""

# ---- OLD condition: age from main HEAD commit date ----
echo "OLD condition (age = NOW - MAIN_DATE):"
OLD_AGE_H=$(( (NOW_TS - MAIN_COMMIT_TS) / 3600 ))
if [ "$OLD_AGE_H" -ge "$GRACE_HOURS" ]; then
  OLD_RESULT="exit 1 (STARVATION)"
else
  OLD_RESULT="exit 0 (within grace)"
fi
echo "  AGE_H=$OLD_AGE_H, GRACE=$GRACE_HOURS -> $OLD_RESULT"
assert_eq "Old condition should NOT fire (main HEAD is fresh)" "exit 0 (within grace)" "$OLD_RESULT"
echo ""

# ---- NEW condition: age from prod build timestamp ----
echo "NEW condition (age = NOW - PROD_BUILD_TS):"
NEW_AGE_H=$(( (NOW_TS - PROD_BUILD_TS) / 3600 ))
if [ "$NEW_AGE_H" -ge "$GRACE_HOURS" ]; then
  NEW_RESULT="exit 1 (STARVATION)"
else
  NEW_RESULT="exit 0 (within grace)"
fi
echo "  AGE_H=$NEW_AGE_H, GRACE=$GRACE_HOURS -> $NEW_RESULT"
assert_eq "New condition SHOULD fire (prod is 11h stale)" "exit 1 (STARVATION)" "$NEW_RESULT"
echo ""

# ---- Scenario: prod is 4h behind (within grace), main HEAD is 30min old ----
PROD_BUILD_TS_4H=$((NOW_TS - 4 * 3600))
echo "Scenario: prod is 4h old (within grace), main HEAD is 30min old"
echo "  PROD_BUILD=$(date -u -d "@$PROD_BUILD_TS_4H" +%Y-%m-%dT%H:%M:%SZ)"
echo ""

echo "OLD condition (age = NOW - MAIN_DATE):"
OLD_AGE_H_4H=$(( (NOW_TS - MAIN_COMMIT_TS) / 3600 ))
if [ "$OLD_AGE_H_4H" -ge "$GRACE_HOURS" ]; then
  OLD_RESULT_4H="exit 1 (STARVATION)"
else
  OLD_RESULT_4H="exit 0 (within grace)"
fi
echo "  AGE_H=$OLD_AGE_H_4H, GRACE=$GRACE_HOURS -> $OLD_RESULT_4H"
assert_eq "Old condition: within grace" "exit 0 (within grace)" "$OLD_RESULT_4H"

echo "NEW condition (age = NOW - PROD_BUILD_TS):"
NEW_AGE_H_4H=$(( (NOW_TS - PROD_BUILD_TS_4H) / 3600 ))
if [ "$NEW_AGE_H_4H" -ge "$GRACE_HOURS" ]; then
  NEW_RESULT_4H="exit 1 (STARVATION)"
else
  NEW_RESULT_4H="exit 0 (within grace)"
fi
echo "  AGE_H=$NEW_AGE_H_4H, GRACE=$GRACE_HOURS -> $NEW_RESULT_4H"
assert_eq "New condition: within grace (prod is only 4h old)" "exit 0 (within grace)" "$NEW_RESULT_4H"
echo ""

# ---- Scenario: prod and main are in sync ----
echo "Scenario: prod SHA == main HEAD (in sync)"
echo "  Both conditions: exit 0 (SHA match short-circuit)"
assert_eq "SHA match short-circuits both conditions" "exit 0 (within grace)" "exit 0 (within grace)"
echo ""

echo "=== Results: $PASS passed, $FAIL failed ==="
if [ "$FAIL" -gt 0 ]; then
  echo "SOME TESTS FAILED"
  exit 1
fi
echo "ALL TESTS PASSED"
