"""Isolated native barcode generation for the Windows zxing-cpp binding."""
from __future__ import annotations

import base64
import io
import json
import sys


def _png(payload: dict) -> dict:
    import numpy as np
    import zxingcpp
    from PIL import Image

    formats = {'qr': zxingcpp.BarcodeFormat.QRCode, 'code128': zxingcpp.BarcodeFormat.Code128}
    scale = int(payload['scale'])
    output = []
    for text in payload['texts']:
        row = []
        for kind in payload['types']:
            if text is None or text == '':
                row.append(None)
                continue
            image = zxingcpp.write_barcode_to_image(
                zxingcpp.create_barcode(text, formats[kind]), scale=scale)
            stream = io.BytesIO()
            Image.fromarray(np.array(image, dtype=np.uint8, copy=True)).save(stream, format='PNG')
            row.append(base64.b64encode(stream.getvalue()).decode('ascii'))
        output.append(row)
    return {'codes': output}


def main() -> int:
    try:
        result = _png(json.load(sys.stdin))
        json.dump({'ok': True, **result}, sys.stdout, separators=(',', ':'))
        return 0
    except Exception as exc:
        # Avoid printing data values or stack traces from this bounded worker.
        json.dump({'ok': False, 'error': type(exc).__name__}, sys.stdout, separators=(',', ':'))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
