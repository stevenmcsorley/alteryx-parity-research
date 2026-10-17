# Text Mining tools

All nine Text Mining tools are implemented, using the methods the vendor documents. These five need no downloads:

| Tool | Method |
|---|---|
| Sentiment Analysis | VADER (`vaderSentiment`): `negative_sentiment`, `neutral_sentiment`, `positive_sentiment` and `compound_sentiment_score`. Sentence-level mode averages the score over sentences. `sentiment_category` uses the `max_negative` / `min_positive` cut-offs, VADER's usual −0.05 / 0.05 by default. English only. |
| Text Summary | Extractive TextRank (`summa`, vendored in `flowengine/_vendor` because it ships only as a source archive). Returns the top *n* sentences unchanged, most important first. |
| Topic Modeling | Latent Dirichlet allocation (scikit-learn, batch variational Bayes, seeded), with stop words and min/max document frequency. Adds `topic_1 … topic_k` shares to each record. **R** is either a chart (top terms by relevance at λ = 0.6, plus an intertopic distance map from Jensen–Shannon distances and classical MDS) or the word relevance summary (relevance, probability, lift, saliency, frequency). **M** is a model for Predict. |
| Word Cloud | The most frequent words, sized by frequency and placed largest first along a spiral without overlaps, about 10% of them vertical. Supports stop words, two-word phrases, an optional mask image (words fill its dark area), themes and a background colour. Output is an SVG chart that opens in the chart viewer and works with Render. |
| Text Classification | TF-IDF features with multinomial naive Bayes (alpha grid) and/or a linear SVM (C log grid, sigmoid-calibrated probabilities). The search is a stratified k-fold grid search on macro F1. **E** reports per-class precision, recall and F1 on the validation data. **M** feeds Predict. |

Predict (Machine Learning) applies Text Classification models, which give `Probability_<class>` and
`Prediction`, and Topic Modeling models, which give `topic_1 … topic_k`.

**Stop words** come from `summa`'s lists for English, German, Spanish, Portuguese and Italian. French currently uses
none.

## Tools that download components on first use

| Tool | Method |
|---|---|
| Text Pre-processing | spaCy tokenization. Optional lemmatization uses the language's model. Removes digits, punctuation and stop words (spaCy defaults, a comma-separated list and/or a stop-word input). Output is `<field>_processed`. |
| Named Entity Recognition | spaCy small models (18 English types such as PERSON, ORG, GPE, DATE and MONEY). `entities` is JSON with `entity`, `label`, `character_index`, `word_index` and `entity_length`; `entity_diagram` is a displaCy HTML view. Custom entities from **E** are matched by an entity ruler ahead of the statistical model, case-sensitively or not; the network is not retrained. **M** feeds Predict. |
| Part-of-Speech Tagger | spaCy: `part_of_speech_tags` is JSON per token (universal POS, fine-grained tag and dependency, each with a description, plus indexes) and `dependency_diagram` is a displaCy SVG. |
| Zero-shot Text Classification | The standard NLI method: "This example is {label}." scored for entailment by cross-encoder/nli-deberta-v3-xsmall (Apache-2.0, quantized ONNX, about 92 MB) in ONNX Runtime. Gives one score per label (softmax across labels, or independent with multi-label) and `top_label`. English only. |

spaCy models are available for English, French, German, Italian, Portuguese and Spanish (`*_core_*_sm` 3.8.0).

### First-use downloads

These tools need components that are **not in the installer**:

| Component | Download | Source |
|---|---|---|
| `language-pack` (spaCy 3.8) | about 75 MB | pypi.org |
| `spacy-model:<language>` | 13–16 MB each | github.com/explosion/spacy-models |
| `neural-runtime` (ONNX Runtime 1.31 and tokenizers) | about 45 MB | pypi.org |
| `zero-shot-model` | about 92 MB | huggingface.co, pinned revision |

When a workflow needs one, the desktop app lists what is missing, with sizes and sources, before the run starts.
It downloads only after you agree, then continues the run. From the command line:

```
python -m flowengine addons list
python -m flowengine addons install spacy-model:english zero-shot-model
python -m flowengine addons remove language-pack
```

How the components are installed and run:

- **Libraries** go into a Flowbench-managed environment, `<Flowbench folder>/envs/flowbench-nlp`. Installed, it takes
  about 440 MB of disk. pip installs binary wheels only, with every version fixed.
- **Models** are verified against pinned SHA-256 checksums before use.
- **During a run**, nothing is downloaded. A tool whose component is missing stops with `addon-missing` and names
  the install command.
- **Workers:** tools run spaCy and ONNX in a separate process (`flowengine/addon_workers`), so the engine never
  imports them.

**Evidence:**
- `tests/test_batch11b.py` covers the installer: pinned pip arguments, checksum verification and rejection, the
  preflight download list, and table plumbing.
- `tests/integration/test_text_addons_integration.py` runs the real models: entities and their indexes, custom
  terms, English and German POS, lemmatization and zero-shot. It runs in the `text-addons` CI job.
- The Windows CI job installs the language pack from the bundled runtime and runs NER.
