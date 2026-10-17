"""Image feature worker (ONNX Runtime + OpenCV), run in the Flowbench add-on environment.

Request ``{"model": path, "images": [paths]}`` → ``{"results": [[1000 ImageNet class scores], ...]}``. Each
image is resized so its short side is 256, centre-cropped to 224 × 224, normalised with the ImageNet mean
and standard deviation, and scored by the backbone.
"""
import json
import os
import sys


def main():
    import cv2
    import numpy as np
    import onnxruntime as ort
    with open(sys.argv[1], encoding='utf-8') as f:
        req = json.load(f)
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = max(1, min(4, os.cpu_count() or 1))
    sess = ort.InferenceSession(req['model'], opts, providers=['CPUExecutionProvider'])
    name = sess.get_inputs()[0].name
    mean, std = np.array([0.485, 0.456, 0.406], np.float32), np.array([0.229, 0.224, 0.225], np.float32)
    results = []
    batch, owners = [], []

    def flush():
        if batch:
            out = sess.run(None, {name: np.stack(batch)})[0].reshape(len(batch), -1)
            for i, row in zip(owners, out):
                results[i] = [float(v) for v in row]
            batch.clear()
            owners.clear()
    for i, path in enumerate(req['images']):
        results.append(None)
        img = cv2.imread(path, cv2.IMREAD_COLOR)
        if img is None:
            continue
        img = img[:, :, ::-1]
        h, w = img.shape[:2]
        s = 256 / min(h, w)
        img = cv2.resize(img, (max(224, round(w * s)), max(224, round(h * s))), interpolation=cv2.INTER_AREA)
        h, w = img.shape[:2]
        top, left = (h - 224) // 2, (w - 224) // 2
        x = (img[top:top + 224, left:left + 224].astype(np.float32) / 255.0 - mean) / std
        batch.append(x.transpose(2, 0, 1))
        owners.append(i)
        if len(batch) == 16:
            flush()
    flush()
    with open(sys.argv[2], 'w', encoding='utf-8') as f:
        json.dump({'results': results}, f)


if __name__ == '__main__':
    main()
