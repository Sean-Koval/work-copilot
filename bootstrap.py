"""Install only when this checkout's dependency manifest changed or imports fail."""
import hashlib
import importlib
from pathlib import Path
import subprocess
import sys

root=Path(__file__).parent
fingerprint=hashlib.sha256((root/'pyproject.toml').read_bytes()+(root/'constraints.txt').read_bytes()).hexdigest()
marker=Path(sys.prefix)/'.work-copilot-installed'
ready=marker.exists() and marker.read_text()==fingerprint
if ready:
    try:
        for name in ('work_copilot','fastapi','uvicorn','mcp','filelock','tzdata'):
            importlib.import_module(name)
    except ImportError:
        ready=False
if not ready:
    subprocess.run([sys.executable,'-m','pip','install','-q','-c',str(root/'constraints.txt'),'-e',str(root)],check=True)
    marker.write_text(fingerprint)
