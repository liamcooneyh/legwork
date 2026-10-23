import os
import tempfile

# Keep tests away from the real database.
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="autoapply-test-"))
