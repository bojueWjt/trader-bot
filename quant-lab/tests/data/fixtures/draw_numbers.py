"""Dependency-free 5x7 bitmap font. Numeric glyph boxes are exact pixel evidence."""
import struct
import zlib

FONT = {
    '0': ('01110','10001','10011','10101','11001','10001','01110'),
    '1': ('00100','01100','00100','00100','00100','00100','01110'),
    '2': ('01110','10001','00001','00010','00100','01000','11111'),
    '3': ('11110','00001','00001','01110','00001','00001','11110'),
    '4': ('00010','00110','01010','10010','11111','00010','00010'),
    '5': ('11111','10000','10000','11110','00001','00001','11110'),
    '6': ('01110','10000','10000','11110','10001','10001','01110'),
    '7': ('11111','00001','00010','00100','01000','01000','01000'),
    '8': ('01110','10001','10001','01110','10001','10001','01110'),
    '9': ('01110','10001','10001','01111','00001','00001','01110'),
    '.': ('00000','00000','00000','00000','00000','00110','00110'),
}
SIZE = [256, 80]


def draw(values, *, decoration=0, unreadable=False, labels=()):
    w, h = SIZE
    pixels = bytearray([255] * (w * h * 3))
    numbers = []
    for i, value in enumerate(values):
        token = str(value)
        x, y = 8, 8 + i * 22
        for j, char in enumerate(token):
            for row, bits in enumerate(FONT[char]):
                for col, bit in enumerate(bits):
                    if bit != '1':
                        continue
                    for dy in range(2):
                        for dx in range(2):
                            xx, yy = x + j * 12 + col * 2 + dx, y + row * 2 + dy
                            if unreadable and (xx + yy) % 3:
                                continue
                            at = (yy * w + xx) * 3
                            pixels[at:at + 3] = bytes([0, 0, 0])
        numbers.append({'value': float(value), 'bbox': [x, y, x + len(token) * 12 - 2, y + 14]})
    if labels:
        from PIL import Image, ImageDraw, ImageFont
        image = Image.frombytes("RGB", (w, h), bytes(pixels))
        painter = ImageDraw.Draw(image)
        font = ImageFont.load_default(size=10)
        for i, label in enumerate(labels):
            painter.text((100, 8 + i * 22), label, font=font, fill=(0, 0, 0))
        pixels = bytearray(image.tobytes())
    pixels[-3:] = bytes([decoration % 256, 0, 0])
    raw = b''.join(b'\x00' + pixels[y * w * 3:(y + 1) * w * 3] for y in range(h))

    def chunk(kind, payload):
        return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind + payload) & 0xffffffff)

    png = b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(raw)) + chunk(b'IEND', b'')
    return png, numbers
