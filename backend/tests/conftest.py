import os
import sys
import tempfile
from pathlib import Path

_tmp = tempfile.mkdtemp(prefix="medassist-test-")
os.environ["MEDASSIST_DATA_DIR"] = _tmp
os.environ["MEDASSIST_GATEWAY_SECRET"] = "test-secret"
os.environ["MEDASSIST_OLLAMA_URL"] = "http://127.0.0.1:9"  # unreachable: tests never call a model
os.environ["MEDASSIST_GATEWAY_URL"] = "http://127.0.0.1:9"
os.environ["MEDASSIST_NETWORK_GUARD"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
