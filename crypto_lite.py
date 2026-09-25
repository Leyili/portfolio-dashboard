# -*- coding: utf-8 -*-
"""零依赖 AES-256-GCM + PBKDF2，用于看板数据包加密。

为什么不用 cryptography / pycryptodome：本项目要在 GitHub Actions 上跑，
本地双击 update.bat 也要能跑，装包多一层环境依赖就多一处断链。
这里用纯标准库实现，任何 python3 都能直接执行，代价是慢一点（几 KB 数据约 1 秒）。

浏览器端用内置的 WebCrypto 解密，两边算法参数必须一致：
    PBKDF2-HMAC-SHA256 → 32 字节密钥 → AES-256-GCM(12 字节 IV, 16 字节 tag)
"""

import base64
import hashlib
import os

ITERATIONS = 200000

# ---------------------------------------------------------------------------
# GF(2^8) 与 S 盒
# ---------------------------------------------------------------------------

_RCON = [0x01, 0x02, 0x04, 0x08, 0x10, 0x20, 0x40, 0x80, 0x1B, 0x36, 0x6C, 0xD8, 0xAB, 0x4D]


def _build_sbox():
    exp = [0] * 512
    log = [0] * 256
    x = 1
    for i in range(255):
        exp[i] = x
        log[x] = i
        # 乘 3（0x03 是本原元，用它生成整个乘法群）
        x = (x ^ ((x << 1) ^ (0x1B if (x & 0x80) else 0))) & 0xFF
    for i in range(255, 512):
        exp[i] = exp[i - 255]

    sbox = [0] * 256
    for a in range(256):
        b = 0 if a == 0 else exp[255 - log[a]]
        s = b
        for r in range(1, 5):
            s ^= ((b << r) | (b >> (8 - r))) & 0xFF
        sbox[a] = s ^ 0x63

    inv = [0] * 256
    for i, v in enumerate(sbox):
        inv[v] = i
    return sbox, inv


SBOX, INV_SBOX = _build_sbox()


def _xtime(a):
    return ((a << 1) ^ 0x1B) & 0xFF if (a & 0x80) else (a << 1) & 0xFF


def _mul8(a, b):
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = _xtime(a)
        b >>= 1
    return r


# ---------------------------------------------------------------------------
# AES
# ---------------------------------------------------------------------------

def _expand_key(key):
    nk = len(key) // 4
    nr = nk + 6
    w = list(key)
    for i in range(nk, 4 * (nr + 1)):
        t = w[(i - 1) * 4:(i - 1) * 4 + 4]
        if i % nk == 0:
            t = [SBOX[b] for b in (t[1:] + t[:1])]
            t[0] ^= _RCON[i // nk - 1]
        elif nk > 6 and i % nk == 4:
            t = [SBOX[b] for b in t]
        w += [w[(i - nk) * 4 + j] ^ t[j] for j in range(4)]
    return w, nr


def _shift_rows(s):
    return [s[((i // 4 + (i % 4)) % 4) * 4 + (i % 4)] for i in range(16)]


def _inv_shift_rows(s):
    return [s[((i // 4 - (i % 4)) % 4) * 4 + (i % 4)] for i in range(16)]


def _mix_columns(s):
    out = [0] * 16
    for c in range(4):
        a0, a1, a2, a3 = s[c * 4], s[c * 4 + 1], s[c * 4 + 2], s[c * 4 + 3]
        out[c * 4] = _xtime(a0) ^ _xtime(a1) ^ a1 ^ a2 ^ a3
        out[c * 4 + 1] = a0 ^ _xtime(a1) ^ _xtime(a2) ^ a2 ^ a3
        out[c * 4 + 2] = a0 ^ a1 ^ _xtime(a2) ^ _xtime(a3) ^ a3
        out[c * 4 + 3] = _xtime(a0) ^ a0 ^ a1 ^ a2 ^ _xtime(a3)
    return out


def _inv_mix_columns(s):
    out = [0] * 16
    for c in range(4):
        a0, a1, a2, a3 = s[c * 4], s[c * 4 + 1], s[c * 4 + 2], s[c * 4 + 3]
        out[c * 4] = _mul8(a0, 14) ^ _mul8(a1, 11) ^ _mul8(a2, 13) ^ _mul8(a3, 9)
        out[c * 4 + 1] = _mul8(a0, 9) ^ _mul8(a1, 14) ^ _mul8(a2, 11) ^ _mul8(a3, 13)
        out[c * 4 + 2] = _mul8(a0, 13) ^ _mul8(a1, 9) ^ _mul8(a2, 14) ^ _mul8(a3, 11)
        out[c * 4 + 3] = _mul8(a0, 11) ^ _mul8(a1, 13) ^ _mul8(a2, 9) ^ _mul8(a3, 14)
    return out


def _encrypt_block(rk, nr, block):
    s = [block[i] ^ rk[i] for i in range(16)]
    for rnd in range(1, nr + 1):
        s = [SBOX[b] for b in s]
        s = _shift_rows(s)
        if rnd != nr:
            s = _mix_columns(s)
        base = rnd * 16
        s = [s[i] ^ rk[base + i] for i in range(16)]
    return bytes(s)


def _decrypt_block(rk, nr, block):
    s = [block[i] ^ rk[nr * 16 + i] for i in range(16)]
    for rnd in range(nr - 1, -1, -1):
        s = _inv_shift_rows(s)
        s = [INV_SBOX[b] for b in s]
        base = rnd * 16
        s = [s[i] ^ rk[base + i] for i in range(16)]
        if rnd != 0:
            s = _inv_mix_columns(s)
    return bytes(s)


# ---------------------------------------------------------------------------
# GCM
# ---------------------------------------------------------------------------

_R = 0xE1000000000000000000000000000000


def _gmul(a, b):
    z = 0
    v = a
    for i in range(128):
        if (b >> (127 - i)) & 1:
            z ^= v
        v = (v >> 1) ^ _R if (v & 1) else (v >> 1)
    return z


def _ghash(h, data):
    y = 0
    for i in range(0, len(data), 16):
        y = _gmul(y ^ int.from_bytes(data[i:i + 16], "big"), h)
    return y


def _pad16(b):
    r = len(b) % 16
    return b + b"\x00" * (16 - r) if r else b


def _inc32(block):
    n = int.from_bytes(block[12:], "big")
    return block[:12] + ((n + 1) & 0xFFFFFFFF).to_bytes(4, "big")


def _ctr(rk, nr, iv, data):
    out = bytearray()
    cb = _inc32(iv)
    for i in range(0, len(data), 16):
        ks = _encrypt_block(rk, nr, cb)
        out += bytes(a ^ b for a, b in zip(data[i:i + 16], ks))
        cb = _inc32(cb)
    return bytes(out)


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------

def _b64(b):
    return base64.b64encode(b).decode("ascii")


def _unb64(s):
    return base64.b64decode(s.encode("ascii"))


def _derive(password, salt, iterations):
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations, 32)


def encrypt(plaintext, password, iterations=ITERATIONS):
    """plaintext: bytes → 可直接内嵌进 HTML 的 dict"""
    salt = os.urandom(16)
    iv = os.urandom(12)
    key = _derive(password, salt, iterations)
    rk, nr = _expand_key(key)
    h = int.from_bytes(_encrypt_block(rk, nr, b"\x00" * 16), "big")
    j0 = iv + b"\x00\x00\x00\x01"

    ct = _ctr(rk, nr, j0, plaintext)
    s = _ghash(h, _pad16(ct) + (0).to_bytes(8, "big") + (len(plaintext) * 8).to_bytes(8, "big"))
    tag = (s ^ int.from_bytes(_encrypt_block(rk, nr, j0), "big")).to_bytes(16, "big")

    return {"v": 1, "iter": iterations, "salt": _b64(salt), "iv": _b64(iv),
            "data": _b64(ct + tag)}


def decrypt(payload, password):
    """encrypt 的逆操作，密码错或数据损坏时抛 ValueError"""
    salt = _unb64(payload["salt"])
    iv = _unb64(payload["iv"])
    raw = _unb64(payload["data"])
    ct, tag = raw[:-16], raw[-16:]

    key = _derive(password, salt, payload.get("iter", ITERATIONS))
    rk, nr = _expand_key(key)
    h = int.from_bytes(_encrypt_block(rk, nr, b"\x00" * 16), "big")
    j0 = iv + b"\x00\x00\x00\x01"

    s = _ghash(h, _pad16(ct) + (0).to_bytes(8, "big") + (len(ct) * 8).to_bytes(8, "big"))
    if (s ^ int.from_bytes(_encrypt_block(rk, nr, j0), "big")).to_bytes(16, "big") != tag:
        raise ValueError("密码错误或数据已损坏")
    return _ctr(rk, nr, j0, ct)


# ---------------------------------------------------------------------------
# 自检：python crypto_lite.py
# ---------------------------------------------------------------------------

def selftest():
    assert SBOX[0] == 0x63 and SBOX[1] == 0x7C and SBOX[0x53] == 0xED, "S 盒生成错误"

    # FIPS-197 C.3 AES-256 示例
    key = bytes(range(32))
    pt = bytes.fromhex("00112233445566778899aabbccddeeff")
    want = bytes.fromhex("8ea2b7ca516745bfeafc49904b496089")
    rk, nr = _expand_key(key)
    got = _encrypt_block(rk, nr, pt)
    assert got == want, "AES-256 加密与标准向量不符：%s" % got.hex()
    assert _decrypt_block(rk, nr, want) == pt, "AES-256 解密不还原"

    plain = "持仓测试 净资产 2,892,480.61 元\n第二行 UTF-8 ✓".encode("utf-8")
    p = encrypt(plain, "abc123")
    assert decrypt(p, "abc123") == plain, "GCM 往返失败"
    try:
        decrypt(p, "wrong")
        raise AssertionError("错误密码竟然解密成功")
    except ValueError:
        pass

    print("[自检] S 盒、AES-256(FIPS-197 C.3)、GCM 往返、错误密码拒绝 —— 全部通过")


if __name__ == "__main__":
    import time
    t = time.time()
    selftest()
    print("[自检] 用时 %.2f 秒" % (time.time() - t))
