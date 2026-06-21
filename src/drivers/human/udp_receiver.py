#!/usr/bin/env python3
import socket
s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
s.bind(("0.0.0.0",3101))
print("Listening on udp://0.0.0.0:3101")
while True:
    data,peer=s.recvfrom(4096)
    print(peer,data.decode("utf-8",errors="replace").rstrip())
