#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""从 NAS 上那台网页版 Chromium 的 profile 里导出 cookie。

用法：
  python3 cookie_export.py --list                 # 列出有哪些域名（按条数排序）
  python3 cookie_export.py --domain instagram.com # 输出该域的 Cookie 头
  python3 cookie_export.py --domain instagram.com --netscape   # 输出 Netscape cookies.txt
"""
import argparse
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

PROFILE = "/vol1/1000/Hermes/chromium-config/.config/chromium/Default"
COOKIES = os.path.join(PROFILE, "Cookies")


def _copy_db() -> str:
    """Chrome 会锁库，先复制一份再读（root 权限用 sudo 拷）。"""
    tmp = tempfile.mktemp(suffix=".db", dir="/tmp")
    try:
        shutil.copy2(COOKIES, tmp)
    except PermissionError:
        subprocess.run(["/usr/bin/sudo", "-n", "cp", COOKIES, tmp], check=True)
    return tmp


def _key() -> bytes:
    # Linux 上没有 keyring 时 Chrome 用固定密码 'peanuts'
    return hashlib.pbkdf2_hmac("sha1", b"peanuts", b"saltysalt", 1, 16)


def _decrypt_bytes(enc: bytes) -> bytes:
    if not enc:
        return b""
    if enc[:3] not in (b"v10", b"v11"):
        return enc
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        return b""
    iv = b" " * 16
    dec = Cipher(algorithms.AES(_key()), modes.CBC(iv)).decryptor()
    out = dec.update(enc[3:]) + dec.finalize()
    pad = out[-1] if out else 0
    if 0 < pad <= 16:
        out = out[:-pad]
    return out


def _decrypt(enc: bytes) -> str:
    return _decrypt_bytes(enc).decode("utf-8", "ignore")


def rows(db):
    con = sqlite3.connect(db)
    try:
        cur = con.execute(
            "SELECT host_key,name,encrypted_value,path,expires_utc,is_secure,is_httponly "
            "FROM cookies")
        return cur.fetchall()
    finally:
        con.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--domain")
    ap.add_argument("--netscape", action="store_true")
    a = ap.parse_args()
    db = _copy_db()
    try:
        rs = rows(db)
    finally:
        if "--keep" not in sys.argv:
            try:
                os.remove(db)
            except OSError:
                pass
    if a.list or not a.domain:
        from collections import Counter
        c = Counter(r[0] for r in rs)
        for host, n in c.most_common(40):
            print(f"{n:4d}  {host}")
        print(f"\n共 {len(rs)} 条 cookie，{len(c)} 个域名")
        return
    dom = a.domain.lower().lstrip(".")
    picked = [r for r in rs if r[0].lower().lstrip(".") == dom or r[0].lower().endswith("." + dom)]
    if not picked:
        print(f"没有 {dom} 的 cookie（这个站点在容器浏览器里未登录）")
        return

    def val_of(host, enc):
        raw = _decrypt_bytes(enc)
        # Chrome 130+ 在密文前加了 sha256(host_key) 的 32 字节前缀
        try:
            h = hashlib.sha256(host.encode()).digest()
            if raw[:32] == h:
                raw = raw[32:]
        except Exception:
            pass
        return raw.decode("utf-8", "ignore")

    vals = [(r[0], r[1], val_of(r[0], r[2]), r[3], r[4], r[5], r[6]) for r in picked]
    if a.netscape:
        print("# Netscape HTTP Cookie File")
        for host, name, val, path, exp, secure, http in vals:
            exp_s = int(exp / 1000000 - 11644473600) if exp else 0
            print("\t".join([host, "TRUE" if host.startswith(".") else "FALSE", path or "/",
                             "TRUE" if secure else "FALSE", str(exp_s), name, val]))
    else:
        print("; ".join(f"{n}={v}" for _, n, v, *_ in vals if v))
        print(f"\n# {len(vals)} 条", file=sys.stderr)


if __name__ == "__main__":
    main()
