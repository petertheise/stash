#!/bin/bash
# Stash launcher: double-click to start, then open http://localhost:5189
cd "$(dirname "$0")"
[ -x venv/bin/python ] || { python3 -m venv venv && ./venv/bin/pip install -q -r requirements.txt; }
./venv/bin/python app.py
