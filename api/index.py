# Vercel runs this file as the app's single Serverless Function. CLI 62 and the
# current Python runtime only discover functions inside api/, not the root
# app.py the older runtime auto-detected. The Flask app itself stays at the repo
# root (app.py) so its templates/ and static/ paths - which are relative to
# app.py's module - keep resolving. This shim just re-exports it.
#
# The sys.path insert is belt and braces: depending on how the runtime lays
# out the bundle, the repo root may not be on the module path by default, and
# `from app import app` would fail on local import order.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import app