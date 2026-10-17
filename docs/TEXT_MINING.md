# Text Mining tools

Five of the nine Text Mining tools are implemented. They use the methods the vendor documents:

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

**Still to come (Batch 11b):** Named Entity Recognition, Part-of-Speech Tagger and Text Pre-processing (spaCy), and
Zero-shot Text Classification (ONNX). Their language models download on first use, after the user confirms; they
are not bundled in the installers.
