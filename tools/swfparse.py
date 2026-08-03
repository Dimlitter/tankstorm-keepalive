#!/usr/bin/env python3
"""SWF 容器解析：解压 + 遍历 tag。abcparse.py / disasm.py 缺的就是这个。"""
import struct
import zlib


def uncompress(raw: bytes):
    """返回 (未压缩的 body, 签名, 版本, 声明长度)。

    body 从 RECT(帧尺寸) 开始，也就是原始文件第 8 字节之后的内容。
    """
    sig, ver = raw[:3], raw[3]
    declared = struct.unpack_from('<I', raw, 4)[0]
    payload = raw[8:]
    if sig == b'CWS':
        # 截断流也能解，用 decompressobj 而不是 decompress
        body = zlib.decompressobj().decompress(payload)
    elif sig == b'ZWS':
        import lzma
        # SWF 的 LZMA: 4字节压缩后长度 + 5字节 props + 数据（无 end marker）
        props = payload[4:9]
        d = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[
            lzma._decode_filter_properties(lzma.FILTER_LZMA1, props)])
        body = d.decompress(payload[9:])
    else:
        body = payload
    return body, sig, ver, declared


def _rect_len(body: bytes) -> int:
    nbits = body[0] >> 3
    total = 5 + nbits * 4
    return (total + 7) // 8


def iter_tags(body: bytes):
    """产出 (tag_code, payload, 该 tag 在 body 中的偏移)。"""
    p = _rect_len(body) + 4          # RECT + frameRate(2) + frameCount(2)
    n = len(body)
    while p + 2 <= n:
        rh = struct.unpack_from('<H', body, p)[0]
        p += 2
        code, ln = rh >> 6, rh & 0x3F
        if ln == 0x3F:
            if p + 4 > n:
                break
            ln = struct.unpack_from('<I', body, p)[0]
            p += 4
        if p + ln > n:
            break
        yield code, body[p:p + ln], p
        p += ln
        if code == 0:                # End
            break


if __name__ == '__main__':
    import sys
    raw = open(sys.argv[1], 'rb').read()
    body, sig, ver, declared = uncompress(raw)
    print(f'{sig.decode()} v{ver}  声明 {declared:,}  实际文件 {len(raw):,}  '
          f'解压后 body {len(body):,}')
    from collections import Counter
    c = Counter()
    for code, payload, _ in iter_tags(body):
        c[code] += 1
    print('tag 分布:', dict(sorted(c.items())))
