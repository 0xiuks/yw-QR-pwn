#!/usr/bin/env python3
import os
import struct

import qrcode
import qrcode.base
import qrcode.util
from PIL import Image
from qrcode.constants import ERROR_CORRECT_L
from qrcode.util import BitBuffer, create_bytes

# QR result buffer and adjacent heap blocks.
B = 0x1657AFF4
P = B + 0x40
N = B + 0x997B4
H0 = B - 0x1C0628
N_SIZE = 0x28
NEXT_BLOCK_ADDR = N + 0x28 + N_SIZE
N_OWNER_ADDR = 0x08C53AB8

# Game objects
D = 0x166351C4
DEV = 0x00857CC8
WF = 0x08D87378
FIELD = 0x08A0B310
BK = 0x08D87360
RPG_T = 0x08D87380
FIELD_REF_SLOT_ADDR = 0x008594AC

# Functions and ROP gadgets for this game version.
PIVOT = 0x0011E678  # Context load and stack pivot
POP_R0_TO_R3_R10_PC = 0x0019A7C0  # POP {R0-R3,R10,PC}
STR_R1_R2 = 0x00361290  # STR R1,[R2]; BX LR
MEMMOVE_FUNC = 0x0018A2E0  # PUSH {R0,LR}; BL memmove; POP {R0,PC}
STORE_RESULT_RET = 0x00135608  # STR R0,[R4]; MOV R0,R5; POP {R4-R6,PC}
RET_TRUE = 0x00151E54  # MOV R0,#1; BX LR
POP_R4_TO_R8_PC = 0x001145A0  # POP {R4-R8,PC}
HEAP_ALLOC_FUNC = 0x0014AA4C  # Heap allocation wrapper.
CREATE_YOKAI_FUNC = 0x003AC730  # Create a yokai; incoming R0 is unused.
YOKAI_PARAM_ID = 0xA3A68AA8  # Jibanyan parameter ID
VIEW_POP_FUNC = 0x00252398  # Pop allocation checkpoints for the view entries.
POP_PC = 0x001047D0  # POP {PC}
POP_LR_PC = 0x0011E690  # POP {LR,PC}
POP_R0_PC = 0x0018A2E8  # POP {R0,PC}
POP_R1_PC = 0x002FFFD0  # POP {R1,PC}
POP_R2_TO_R4_PC = 0x00106700  # POP {R2-R4,PC}
LDR_R0_R1 = 0x0063D63C  # LDR R0,[R1]; BX LR
STR_R0_R1 = 0x0010B940  # STR R0,[R1]; BX LR
ADD8 = 0x0033FE5C  # ADD R0,R0,#8; BX LR

# Return stack and scratch area.
EXIT_SP = 0x0FFFFEE8
# Facial landmark detection workspace [0x00903838, 0x00906868), unused during QR scanning.
SCRATCH_ADDR = 0x00906868
YOKAI_RESULT_OFF = 0x10

# Payload object layout.
INITIAL_STACK_OFF = 0x060
ALLOC_CTX_OFF = 0x0D8
VIEW_CTX_OFF = 0x118
ROP_CHAIN_OFF = 0x158
DEV_SLOT_OFF = 0x1C8
WF_SLOT_OFF = 0x1CC
RESTORE_TEMPLATE_OFF = 0x1D0
EXIT_CTX_OFF = 0x204
YOKAI_CTX_OFF = EXIT_CTX_OFF - 0x0C
OBJ_LEN = 0x244
PREFIX_LEN = 27
FIRST_SEGMENT_LEN = PREFIX_LEN + OBJ_LEN
TARGET_HEADER_DATA_OFF = N - (B + 37)

# QR image configuration.
QR_VERSION = 18
QR_ECC = ERROR_CORRECT_L
QR_MASK = 1

# Data restored before returning to the field.
MATERIAL = (
    0x11123000, 0x11123000, 0xFFFFFFFF, 0x00000000,
    0x11121000, 0x11121000, 0xFFFFFFFF, 0x00000000,
    0xB8DE009C, 0x21D75126,
)


def p32(buf, off, val):
    struct.pack_into("<I", buf, off, val)


def build_fake_object():
    b = bytearray(OBJ_LEN)

    # Initial fake screen object / PIVOT context.
    p32(b, 0x00, P + 0x40)
    p32(b, 0x04, FIELD)
    p32(b, 0x08, RPG_T)
    p32(b, 0x0C, 0)
    p32(b, 0x14, 1)
    p32(b, 0x34, P + INITIAL_STACK_OFF)
    p32(b, 0x38, POP_R0_TO_R3_R10_PC)
    p32(b, 0x3C, STR_R1_R2)
    for off in range(0x40, 0x58, 4):
        p32(b, off, PIVOT)

    # Stage frames, executed by POP_R0_TO_R3_R10_PC from P+INITIAL_STACK_OFF.
    def frame(off, r0, r1, r2, r3, pc):
        p32(b, off + 0x00, r0)
        p32(b, off + 0x04, r1)
        p32(b, off + 0x08, r2)
        p32(b, off + 0x0C, r3)
        p32(b, off + 0x10, 0)
        p32(b, off + 0x14, pc)

    frame(INITIAL_STACK_OFF + 0x00, 0, 0, D + 4, 0, STR_R1_R2)  # D.ref = 0
    frame(INITIAL_STACK_OFF + 0x18, 0, FIELD, FIELD_REF_SLOT_ADDR, RET_TRUE, STR_R1_R2)  # RET_TRUE also occupies fake-vtable slot P+0x84.
    frame(INITIAL_STACK_OFF + 0x30, 0, P + VIEW_CTX_OFF, SCRATCH_ADDR + 4, 0, STR_R1_R2)  # [SCRATCH_ADDR+4] = P+VIEW_CTX_OFF
    frame(INITIAL_STACK_OFF + 0x48, 0, PIVOT, SCRATCH_ADDR + 0xC, 0, STR_R1_R2)  # [SCRATCH_ADDR+0xC] = PIVOT
    frame(INITIAL_STACK_OFF + 0x60, P + YOKAI_CTX_OFF, 0, 0, 0, PIVOT)  # call CREATE_YOKAI_FUNC, then ALLOC_CTX_OFF

    # Allocation context.
    p32(b, ALLOC_CTX_OFF + 0x00, P + DEV_SLOT_OFF)  # r0 = pointer to word containing DEV
    p32(b, ALLOC_CTX_OFF + 0x04, D)  # r1 = owner
    p32(b, ALLOC_CTX_OFF + 0x08, 40)  # r2 = size
    p32(b, ALLOC_CTX_OFF + 0x0C, 2)  # r3 = align shift
    p32(b, ALLOC_CTX_OFF + 0x10, D + 0x14C)  # r4 = result slot for STORE_RESULT_RET
    p32(b, ALLOC_CTX_OFF + 0x14, P + VIEW_CTX_OFF)  # r5 -> r0 after STORE_RESULT_RET
    for off in range(0x18, 0x34, 4):
        p32(b, ALLOC_CTX_OFF + off, 0)
    p32(b, ALLOC_CTX_OFF + 0x34, SCRATCH_ADDR)  # SP for the allocator
    p32(b, ALLOC_CTX_OFF + 0x38, STORE_RESULT_RET)  # LR
    p32(b, ALLOC_CTX_OFF + 0x3C, HEAP_ALLOC_FUNC)  # PC

    # View-pop context. The same memory backs view+0x40/+0x48:
    #   view = P+ALLOC_CTX_OFF, view+0x40 = P+VIEW_CTX_OFF+0, view+0x48 = P+VIEW_CTX_OFF+8.
    p32(b, VIEW_CTX_OFF + 0x00, P + WF_SLOT_OFF)  # entries pointer
    p32(b, VIEW_CTX_OFF + 0x04, 0)
    p32(b, VIEW_CTX_OFF + 0x08, 0x00010001)  # entry count = 1
    for off in range(0x0C, 0x34, 4):
        p32(b, VIEW_CTX_OFF + off, 0)
    p32(b, VIEW_CTX_OFF + 0x34, P + ROP_CHAIN_OFF)
    p32(b, VIEW_CTX_OFF + 0x38, POP_PC)
    p32(b, VIEW_CTX_OFF + 0x3C, POP_R1_PC)

    # ROP chain.
    chain = [
        D + 0x14C,  # POP R1; LDR R0 -> r0 = p
        LDR_R0_R1,
        POP_R2_TO_R4_PC,  # r2 = 40
        40,
        0,
        0,
        POP_R1_PC,  # r1 = template
        P + RESTORE_TEMPLATE_OFF,
        MEMMOVE_FUNC,  # memmove(p, template, 40); r0=p, LR=0x18A2E8
        POP_LR_PC,  # restore LR=POP_PC (MEMMOVE_FUNC's internal BL clobbers LR)
        POP_PC,
        POP_R1_PC,  # r1 = D+0x80
        D + 0x80,
        ADD8,  # p+8
        ADD8,  # p+16
        STR_R0_R1,  # [D+0x80] = p+16
        POP_R1_PC,  # r1 = D+0x218
        D + 0x218,
        ADD8,  # p+24
        ADD8,  # p+32
        STR_R0_R1,  # [D+0x218] = p+32
        POP_R0_PC,  # r0 = view; PC = POP_LR_PC
        P + ALLOC_CTX_OFF,
        POP_LR_PC,  # LR = POP_R0_PC; PC = VIEW_POP_FUNC
        POP_R0_PC,
        VIEW_POP_FUNC,
        P + EXIT_CTX_OFF,  # POP_R0_PC return record
        PIVOT,
    ]
    for i, word in enumerate(chain):
        p32(b, ROP_CHAIN_OFF + 4 * i, word)

    p32(b, DEV_SLOT_OFF, DEV)
    p32(b, WF_SLOT_OFF, WF)
    for i, word in enumerate(MATERIAL):
        p32(b, RESTORE_TEMPLATE_OFF + 4 * i, word)

    # Yokai-creation context prefix at EXIT_CTX_OFF-0x0C.
    p32(b, YOKAI_CTX_OFF + 0x00, 0)  # r0 is unused
    p32(b, YOKAI_CTX_OFF + 0x04, 0)  # r1 = 0
    p32(b, YOKAI_CTX_OFF + 0x08, YOKAI_PARAM_ID)  # r2 = YOKAI_PARAM_ID

    # Exit and yokai-creation contexts share these words:
    #   exit R0-R12 = creation R3-R12 plus creation SP/LR/PC
    #   exit SP/LR/PC follow at EXIT_CTX_OFF+0x34
    for off in range(0x00, 0x34, 4):
        p32(b, EXIT_CTX_OFF + off, 0)
    p32(b, EXIT_CTX_OFF + 0x04, SCRATCH_ADDR + YOKAI_RESULT_OFF)  # creation R4
    p32(b, EXIT_CTX_OFF + 0x08, P + ALLOC_CTX_OFF)  # creation R5
    # Exit R9 points to the fake object.
    # This shared word is not an input to CREATE_YOKAI_FUNC.
    p32(b, EXIT_CTX_OFF + 0x24, P)
    p32(b, EXIT_CTX_OFF + 0x28, SCRATCH_ADDR)  # creation SP
    p32(b, EXIT_CTX_OFF + 0x2C, STORE_RESULT_RET)  # creation LR
    p32(b, EXIT_CTX_OFF + 0x30, CREATE_YOKAI_FUNC)  # creation PC
    p32(b, EXIT_CTX_OFF + 0x34, EXIT_SP)  # exit SP
    p32(b, EXIT_CTX_OFF + 0x38, 0)
    p32(b, EXIT_CTX_OFF + 0x3C, POP_R4_TO_R8_PC)
    return b


def build_fake_heap_header():
    b = bytearray(40)
    p32(b, 0x00, DEV)
    p32(b, 0x04, H0)
    p32(b, 0x08, NEXT_BLOCK_ADDR)
    p32(b, 0x0C, 0)
    p32(b, 0x10, N_OWNER_ADDR)
    p32(b, 0x14, N_SIZE)
    p32(b, 0x18, N_SIZE)
    p32(b, 0x1C, 0x00020000)
    p32(b, 0x20, P)
    p32(b, 0x24, BK)
    return b


def build_first_segment():
    b = bytearray(FIRST_SEGMENT_LEN)
    b[PREFIX_LEN:] = build_fake_object()
    return b


def build_declared_lengths():
    remaining = TARGET_HEADER_DATA_OFF - FIRST_SEGMENT_LEN
    out = []
    while remaining > 65535:
        out.append(65535)
        remaining -= 65535
    out.append(remaining)
    return out


DECLARED_LENGTHS = build_declared_lengths()


def build_bitstream():
    bits = []

    def put(val, n):
        for i in range(n - 1, -1, -1):
            bits.append((val >> i) & 1)

    put(0b0100, 4)
    put(FIRST_SEGMENT_LEN, 16)
    for by in build_first_segment():
        put(by, 8)
    for count in DECLARED_LENGTHS:
        put(0b0011, 4)
        put(0, 4)
        put(0, 4)
        put(0, 8)
        put(0b0100, 4)
        put(count, 16)
    put(0b0100, 4)
    put(40, 16)
    for by in build_fake_heap_header():
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


def build_image():
    scale = 8
    border = 4
    rs = qrcode.base.rs_blocks(QR_VERSION, QR_ECC)
    cap = sum(block.data_count for block in rs)
    data = build_bitstream()
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
    img.save(os.path.join(here, "yw2_qr_poc.png"))


if __name__ == "__main__":
    build_image()
