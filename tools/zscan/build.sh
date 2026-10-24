#!/bin/bash
# zscan, static, built in a manylinux2014 container (see tools/build-portable.sh)
exec "$(dirname "$0")/../build-portable.sh" zscan
