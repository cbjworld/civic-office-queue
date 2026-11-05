"""Local data directory and network interfaces shared by the applications."""
import os
from pathlib import Path
import socket


def data_dir():
    path = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'CivicOfficeQueue'
    path.mkdir(parents=True, exist_ok=True)
    return path


def local_ips():
    return {address[4][0] for address in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)}
