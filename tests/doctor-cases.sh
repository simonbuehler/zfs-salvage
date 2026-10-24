#!/bin/bash
# Simulates every failure the doctor knows (fake system facts, missing tools, a failing ssh) and checks the
# expected line for each; also that a correct setup is all OK.
cd "$(dirname "$0")/.." || exit 1
exec python3 -I -m unittest discover -s tests -p test_doctor.py -v
