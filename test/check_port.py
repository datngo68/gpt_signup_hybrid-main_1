# test/check_port.py
import socket

s = socket.socket(socket.AF_SOCKET if hasattr(socket, "AF_SOCKET") else socket.AF_INET, socket.SOCK_STREAM)
try:
    s.bind(("127.0.0.1", 8083))
    print("PORT_8083_FREE")
    s.close()
except Exception as e:
    print("PORT_8083_IN_USE:", e)
