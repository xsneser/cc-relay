#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Root entry-point shim forwarding to relay.lifecycle."""
import sys
from relay import lifecycle as _impl

sys.modules[__name__] = _impl

for _k, _v in list(_impl.__dict__.items()):
    if not _k.startswith("__"):
        globals()[_k] = _v

if __name__ == "__main__":
    _impl.main() if hasattr(_impl, "main") else None
