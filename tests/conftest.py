"""
Test-run environment fixes.

SSLKEYLOGFILE gets inherited from whatever shell launched the run - it was set
at some point to capture TLS keys while the WARG protocol was being reverse
engineered, and it points at C:\\sslkeys.log. urllib3 reads it at *import* time
and assigns context.keylog_filename, so if anything still holds that file open
the import of `requests` dies with PermissionError and every test in the
process fails for a reason that has nothing to do with the tests.

Clearing it here rather than in each command means a plain `pytest` works.
"""

import os

os.environ.pop("SSLKEYLOGFILE", None)
