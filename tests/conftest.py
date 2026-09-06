"""Suite-wide environment, set before any test module imports the server.

`lookup.MODE` is read once at import time and defaults to `off` — an
unconfigured launch of this app makes no outbound request, which is the
pre-Phase-6 contract and the right default for a dev run, a clone, or a test
process. The suite exercises the enabled paths, so it opts in the same way a
deployment does.

This has to live in conftest rather than at the top of test_lookup.py: pytest
imports every test module, and whichever one imports the server first fixes
MODE for the whole process. Setting it there worked when that file happened to
be imported first and failed when it did not.

Nothing here reaches the network. The tests drive parsing and provenance from
recorded responses; MODE only decides whether the endpoints refuse to run at
all.
"""

import os

os.environ.setdefault("THINCART_LOOKUP", "all")
