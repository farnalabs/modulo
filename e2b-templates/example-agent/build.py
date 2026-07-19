"""Build the modulo-agent E2B sandbox template."""
import os, sys
os.environ.setdefault("E2B_API_KEY", os.environ.get("MODULO_E2B_API_KEY", ""))
if not os.environ.get("E2B_API_KEY"):
    print("ERROR: Set E2B_API_KEY"); sys.exit(1)
import subprocess
r = subprocess.run([sys.executable, "template.py"], capture_output=True, text=True)
print(r.stdout[-500:] if len(r.stdout) > 500 else r.stdout)
if r.returncode != 0:
    print("STDERR:", r.stderr[-500:] if len(r.stderr) > 500 else r.stderr)
    sys.exit(r.returncode)
print("Build complete")
