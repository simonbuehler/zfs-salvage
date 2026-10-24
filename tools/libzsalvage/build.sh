#!/bin/bash
# libzsalvage.so, built in a manylinux2014 container (glibc 2.17; see tools/build-portable.sh), loaded by zsread.py via ctypes
exec "$(dirname "$0")/../build-portable.sh" libzsalvage
