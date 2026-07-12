#!/usr/bin/env python3
"""mem0_hook_daemon.py への軽量ソケットクライアント。
起動コストほぼゼロ(重いimportなし)で、常駐デーモンにクエリを投げるだけ。
"""
import socket
import sys

SOCK_PATH = "/tmp/mem0_hook.sock"


def main(query: str) -> None:
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(5)
        s.connect(SOCK_PATH)
        s.sendall((query + "\n").encode("utf-8"))
        s.shutdown(socket.SHUT_WR)
        chunks = []
        while True:
            data = s.recv(4096)
            if not data:
                break
            chunks.append(data)
        s.close()
        out = b"".join(chunks).decode("utf-8", "replace")
        if out.strip():
            print(out, end="")
    except Exception:
        return


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else ""
    if q.strip():
        main(q)
