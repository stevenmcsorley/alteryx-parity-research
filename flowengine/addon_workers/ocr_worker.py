"""OCR worker (PaddleOCR PP-OCR models in ONNX Runtime with OpenCV), run in the Flowbench add-on environment.

Request ``{"det": path, "rec": path, "dict": path, "images": [png paths], "rec_height": 48}`` →
``{"results": [[{"text", "score", "box": [[x, y] * 4]}, ...] per image]}``. Words come out in reading
order (top to bottom, then left to right).
Detection uses DB with a 0.3 binarisation threshold, a 0.6 box score threshold and 1.5 unclip. Recognition
is CTC with greedy decoding.
"""
import json
import sys


def _session(path):
    import os
    import onnxruntime as ort
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = max(1, min(4, os.cpu_count() or 1))
    return ort.InferenceSession(path, opts, providers=['CPUExecutionProvider'])


def detect(sess, img, limit=960):
    import cv2
    import numpy as np
    import pyclipper
    h, w = img.shape[:2]
    scale = min(1.0, limit / max(h, w))
    nh, nw = max(32, int(round(h * scale / 32)) * 32), max(32, int(round(w * scale / 32)) * 32)
    x = cv2.resize(img, (nw, nh)).astype(np.float32) / 255.0
    x = (x - np.array([0.485, 0.456, 0.406], np.float32)) / np.array([0.229, 0.224, 0.225], np.float32)
    prob = sess.run(None, {sess.get_inputs()[0].name: x.transpose(2, 0, 1)[None]})[0][0, 0]
    bitmap = (prob > 0.3).astype(np.uint8)
    contours, _ = cv2.findContours(bitmap, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    boxes = []
    for c in contours[:1000]:
        if len(c) < 4:
            continue
        rect = cv2.minAreaRect(c)
        if min(rect[1]) < 3:
            continue
        mask = np.zeros_like(bitmap)
        cv2.fillPoly(mask, [c.reshape(-1, 2)], 1)
        if mask.sum() == 0 or float(prob[mask.astype(bool)].mean()) < 0.6:
            continue
        pts = cv2.boxPoints(rect)
        area, length = cv2.contourArea(pts), cv2.arcLength(pts, True)
        off = pyclipper.PyclipperOffset()
        off.AddPath([tuple(map(int, p)) for p in pts], pyclipper.JT_ROUND, pyclipper.ET_CLOSEDPOLYGON)
        grown = off.Execute(area * 1.5 / max(length, 1e-6))
        if not grown:
            continue
        rect = cv2.minAreaRect(np.array(grown[0], np.float32))
        if min(rect[1]) < 5:
            continue
        box = cv2.boxPoints(rect)
        box[:, 0] = np.clip(box[:, 0] * w / nw, 0, w - 1)
        box[:, 1] = np.clip(box[:, 1] * h / nh, 0, h - 1)
        s = box.sum(axis=1)
        d = np.diff(box, axis=1).ravel()
        boxes.append(np.array([box[s.argmin()], box[d.argmin()], box[s.argmax()], box[d.argmax()]], np.float32))
    boxes.sort(key=lambda b: (round(float(b[:, 1].mean()) / 10), float(b[:, 0].min())))
    return boxes


def crop(img, box):
    import cv2
    import numpy as np
    w = int(max(np.linalg.norm(box[0] - box[1]), np.linalg.norm(box[2] - box[3])))
    h = int(max(np.linalg.norm(box[0] - box[3]), np.linalg.norm(box[1] - box[2])))
    dst = np.array([[0, 0], [w, 0], [w, h], [0, h]], np.float32)
    out = cv2.warpPerspective(img, cv2.getPerspectiveTransform(box, dst), (max(w, 1), max(h, 1)),
                              borderMode=cv2.BORDER_REPLICATE, flags=cv2.INTER_CUBIC)
    if out.shape[0] / max(out.shape[1], 1) >= 1.5:
        out = np.rot90(out)
    return out


def recognize(sess, crops, chars, height):
    import cv2
    import numpy as np
    out = []
    for i in range(0, len(crops), 16):
        batch = crops[i:i + 16]
        width = max(int(np.ceil(height * c.shape[1] / max(c.shape[0], 1))) for c in batch)
        width = max(16, min(width, 1600))
        x = np.zeros((len(batch), 3, height, width), np.float32)
        for k, c in enumerate(batch):
            rw = max(1, min(width, int(np.ceil(height * c.shape[1] / max(c.shape[0], 1)))))
            r = cv2.resize(c, (rw, height)).astype(np.float32) / 255.0
            x[k, :, :, :rw] = ((r - 0.5) / 0.5).transpose(2, 0, 1)
        probs = sess.run(None, {sess.get_inputs()[0].name: x})[0]
        for p in probs:
            idx, conf = p.argmax(axis=1), p.max(axis=1)
            text, scores, prev = [], [], 0
            for t, (j, s) in enumerate(zip(idx, conf)):
                if j != 0 and j != prev and j - 1 < len(chars):
                    text.append(chars[j - 1])
                    scores.append(float(s))
                prev = j
            out.append((''.join(text), float(np.mean(scores)) if scores else 0.0))
    return out


def main():
    import cv2
    with open(sys.argv[1], encoding='utf-8') as f:
        req = json.load(f)
    with open(req['dict'], encoding='utf-8') as f:
        chars = [line.rstrip('\n') for line in f] + [' ']
    det, rec = _session(req['det']), _session(req['rec'])
    results = []
    for path in req['images']:
        img = cv2.imread(path, cv2.IMREAD_COLOR)
        if img is None:
            results.append([])
            continue
        img = img[:, :, ::-1].copy()  # RGB
        boxes = detect(det, img)
        texts = recognize(rec, [crop(img, b) for b in boxes], chars, req.get('rec_height', 48)) if boxes else []
        results.append([{'text': t, 'score': s, 'box': b.round(1).tolist()} for b, (t, s) in zip(boxes, texts) if t.strip()])
    with open(sys.argv[2], 'w', encoding='utf-8') as f:
        json.dump({'results': results}, f, ensure_ascii=False)


if __name__ == '__main__':
    main()
