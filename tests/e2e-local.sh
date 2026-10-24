#!/bin/bash
# End-to-end test on one machine: scan, rank, catalog, select, transfer and verify through the web API,
# without ssh, on a fixture image attached as a read-only file. Prints E2E PASS or E2E FAIL.
# Needs python3, gcc (builds zscan/libzsalvage.so) and the fixture images (tests/vm/run.sh tests/make-fixtures.sh).
cd "$(dirname "$0")/.." || exit 1
if python3 -I -m unittest discover -s tests -p test_e2e_local.py 2>&1 | tee /tmp/e2e-local.log | tail -3 | grep -q '^OK'; then
    echo "E2E PASS"
else
    tail -20 /tmp/e2e-local.log; echo "E2E FAIL"; exit 1
fi
