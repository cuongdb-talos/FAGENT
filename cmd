#!/bin/bash
rm -r output
exec python3 -m FAGENT --setup env_setup.yaml  --verbose "$@"
