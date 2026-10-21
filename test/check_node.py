# test/check_node.py
import subprocess
import shutil

node_path = shutil.which("node")
print("Node binary found:", node_path)
if node_path:
    try:
        ver = subprocess.check_output([node_path, "--version"], text=True).strip()
        print("Node version:", ver)
    except Exception as e:
        print("Error checking node version:", e)
