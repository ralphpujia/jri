#!/bin/bash
cd /home/nico/jri
python3 -m pytest tests/e2e_test.py -v --timeout=120
