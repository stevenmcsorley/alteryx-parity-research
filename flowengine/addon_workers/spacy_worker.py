"""spaCy worker, run in the Flowbench add-on environment: ``python spacy_worker.py request.json response.json``.

The request is ``{"task": "preprocess" | "ner" | "pos", "language": ..., "model": name or null, "texts": [...],
...options}``. The response is ``{"results": [...]}``, one result per text, in the same order.
This file must only import the standard library and spaCy.
"""
import json
import sys

CODES = {'english': 'en', 'french': 'fr', 'german': 'de', 'italian': 'it', 'portuguese': 'pt', 'spanish': 'es'}


def load(req, disable=()):
    import spacy
    if req.get('model'):
        return spacy.load(req['model'], disable=list(disable))
    return spacy.blank(CODES[req['language']])


def preprocess(req):
    nlp = load(req, disable=('ner', 'parser'))
    stops = set()
    if req['filter_stopwords']:
        if req['default_stopwords']:
            stops |= {w.lower() for w in nlp.Defaults.stop_words}
        stops |= {w.lower() for w in req['custom_stopwords']}
    out = []
    for doc in nlp.pipe(req['texts'], batch_size=64):
        words = []
        for t in doc:
            if t.is_space:
                continue
            if req['filter_digits'] and (t.is_digit or t.like_num):
                continue
            if req['filter_punctuation'] and t.is_punct:
                continue
            if t.lower_ in stops:
                continue
            words.append(t.lemma_ if req['lemmatize'] and t.lemma_ else t.text)
        out.append(' '.join(words))
    return out


def ner(req):
    from spacy import displacy
    nlp = load(req)
    if req.get('patterns'):
        attr = 'ORTH' if req.get('case_sensitive') else 'LOWER'
        ruler = nlp.add_pipe('entity_ruler', before='ner', config={'overwrite_ents': True, 'phrase_matcher_attr': attr})
        ruler.add_patterns([{'label': p['label'], 'pattern': p['entity']} for p in req['patterns']])
    out = []
    for doc in nlp.pipe(req['texts'], batch_size=32):
        ents = [{'entity': e.text, 'label': e.label_, 'character_index': e.start_char, 'word_index': e.start,
                 'entity_length': len(e.text)} for e in doc.ents]
        out.append({'entities': ents, 'diagram': displacy.render(doc, style='ent', page=False, jupyter=False)})
    return out


def pos(req):
    import spacy
    from spacy import displacy
    nlp = load(req, disable=('ner',))
    out = []
    for doc in nlp.pipe(req['texts'], batch_size=32):
        tags = [{'text': t.text, 'part_of_speech': t.pos_, 'part_of_speech_description': spacy.explain(t.pos_) or '',
                 'fine_grained_tag': t.tag_, 'fine_grained_tag_description': spacy.explain(t.tag_) or '',
                 'dependency': t.dep_, 'dependency_description': spacy.explain(t.dep_) or '',
                 'character_index': t.idx, 'word_index': t.i, 'text_length': len(t.text)} for t in doc if not t.is_space]
        out.append({'tags': tags, 'diagram': displacy.render(doc, style='dep', page=False, jupyter=False)})
    return out


def main():
    with open(sys.argv[1], encoding='utf-8') as f:
        req = json.load(f)
    results = {'preprocess': preprocess, 'ner': ner, 'pos': pos}[req['task']](req)
    with open(sys.argv[2], 'w', encoding='utf-8') as f:
        json.dump({'results': results}, f, ensure_ascii=False)


if __name__ == '__main__':
    main()
