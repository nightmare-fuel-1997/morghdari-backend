"""Probe support login + reject pending requests."""
import base64
import hashlib
import hmac
import os
import re
import struct
import subprocess
import sys
import time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from qa_lib import api

HERE = os.path.dirname(os.path.abspath(__file__))
PW = "password12345"
MFA_SECRET = os.environ.get("QA_MFA_SECRET", "")

def totp(secret, skew=0):
    k = base64.b32decode(secret)
    msg = struct.pack(">Q", int((time.time() + skew) // 30))
    h = hmac.new(k, msg, hashlib.sha1).digest()
    o = h[-1] & 0xF
    return str((struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % 10 ** 6).zfill(6)

for skew in (0, -30, 30):
    r = api("POST", "/auth/login/", body={"username": "support1", "password": PW, "mfa_code": totp(MFA_SECRET, skew)})
    print(f"skew {skew}: {r['status']} {str(r['json'])[:100]}", flush=True)
