"""Zero-shot classification worker (ONNX Runtime + tokenizers), run in the Flowbench add-on environment.

Request ``{"model_dir", "texts", "labels", "template", "multi_label"}`` → ``{"results": [[score per label], ...]}``.
Each label becomes the hypothesis ``template.format(label)``, which is scored for entailment against the text,
as in the standard NLI zero-shot method. Single-label scores are a softmax of the entailment logits across labels.
Multi-label scores are each label's softmax over entailment and contradiction.
"""
import json
import os
import sys


def main():
    import numpy as np
    import onnxruntime as ort
    from tokenizers import Tokenizer
    with open(sys.argv[1], encoding='utf-8') as f:
        req = json.load(f)
    d = req['model_dir']
    with open(os.path.join(d, 'config.json'), encoding='utf-8') as f:
        label_ids = {v.lower(): int(k) for k, v in json.load(f)['id2label'].items()}
    entail, contra = label_ids['entailment'], label_ids['contradiction']
    tok = Tokenizer.from_file(os.path.join(d, 'tokenizer.json'))
    tok.enable_truncation(max_length=512)
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = max(1, min(4, os.cpu_count() or 1))
    sess = ort.InferenceSession(os.path.join(d, 'model.onnx'), opts, providers=['CPUExecutionProvider'])
    names = {i.name for i in sess.get_inputs()}
    hypotheses = [req['template'].format(label) for label in req['labels']]
    results = []
    for text in req['texts']:
        enc = tok.encode_batch([(text, h) for h in hypotheses])
        width = max(len(e.ids) for e in enc)
        pad = lambda rows: np.array([r + [0] * (width - len(r)) for r in rows], dtype=np.int64)
        feeds = {'input_ids': pad([e.ids for e in enc]), 'attention_mask': pad([e.attention_mask for e in enc])}
        if 'token_type_ids' in names:
            feeds['token_type_ids'] = pad([e.type_ids for e in enc])
        logits = sess.run(None, feeds)[0].astype(np.float64)
        if req['multi_label']:
            pair = logits[:, [contra, entail]]
            pair = np.exp(pair - pair.max(axis=1, keepdims=True))
            scores = pair[:, 1] / pair.sum(axis=1)
        else:
            e = logits[:, entail]
            e = np.exp(e - e.max())
            scores = e / e.sum()
        results.append([float(s) for s in scores])
    with open(sys.argv[2], 'w', encoding='utf-8') as f:
        json.dump({'results': results}, f)


if __name__ == '__main__':
    main()
