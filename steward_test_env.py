"""Import first in any test module that builds an AwakeSession or the app.

Points the Keep Awake session record at a throwaway folder so tests never read, adopt or stop
the developer's real background session in ~/.system-steward.
"""
import atexit
import os
import shutil
import tempfile

if "SYSTEM_STEWARD_STATE_DIR" not in os.environ:
    _folder = tempfile.mkdtemp(prefix="steward-state-")
    os.environ["SYSTEM_STEWARD_STATE_DIR"] = _folder
    atexit.register(shutil.rmtree, _folder, ignore_errors=True)
