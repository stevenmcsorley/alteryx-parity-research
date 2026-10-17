# Computer Vision tools

Images travel as **blob** fields (PNG, JPEG, BMP or TIFF bytes). These seven tools use only bundled libraries:
Pillow, NumPy and SciPy for images, **PDFium** (`pypdfium2`) for PDFs, and **zxing-cpp** for barcodes.

| Tool | What it does |
|---|---|
| Image Input | Reads images and PDF pages from a folder, a file or a path field, with an optional file-type filter. Each PDF page is rendered to PNG at the chosen dpi, with its page number. |
| Image Output | Saves images as PNG, JPEG, BMP or TIFF to a folder, named from a field (made safe for the file system and de-duplicated). Needs the *file writes* permission. |
| Image Profile | **Base:** mode, size, channels, min/max, most frequent colour, and dark/bright pixel counts. **Exif:** the tags as JSON, plus GPS latitude, longitude, altitude, speed, track and time. **Summary:** pixel count and per-channel sum, squared sum, mean, median, RMS, variance and standard deviation. These are the vendor's column names. |
| Image Processing | Ordered steps: Alignment (auto deskew by projection-profile variance, or custom degrees), Brightness Balance, Crop (nine anchors, pixels or %), Enhance, Grayscale, OCR Optimization, Scale, Shift and Thresholding (adaptive Gaussian or binary). Output is `image_processed` as PNG. |
| Image Template | Labelled regions (string, table or image) on a template image or PDF page. Coordinates use the same JSON as an export. Each record carries the template crop, which Shift uses as an anchor. |
| Barcode | **Generate:** QR and Code 128 as PNG blobs. **Read:** QR, Code 128, Code 39, UPC-A, EAN-8/13 (reported as ISBN-13, ISSN or JAN by prefix), ITF (I25 or ITF-14), Data Matrix and PDF417. Gives one record per barcode, with an optional cropped image and an outlined copy of the original. |
| PDF to Text | PDF text in four formats: String (per page), Lines, Pipe-delimited Table, and Table (`Column_1…n` split by horizontal overlap). Supports a page range, a **risk score** for text that may be stored as images, a rendered page image, and Image Template regions scaled to each page. |

## Methods that differ from the vendor

- **Enhance** is 4× Lanczos upscaling with unsharp masking, a classical method. The vendor uses a learned
  super-resolution model. As with the vendor, images larger than 1000 × 1000 are left unchanged.
- **OCR Optimization** flattens the background (grey closing), then denoises (heavy mode) and applies an adaptive
  Gaussian threshold. Auto mode chooses heavy or light from a noise estimate. *Some text has a non-white
  background* first equalises local contrast.
- **Shift** finds the anchor by normalised cross-correlation (FFT) and translates the image back into place.
- **Image Template** annotations are entered as coordinates, since there is no drawing tool yet. Automatic table
  detection is not implemented.

## Tools that download models on first use

| Tool | Method |
|---|---|
| Image to Text | OCR with PaddleOCR in ONNX Runtime: PP-OCRv3 DB text detection and PP-OCRv5 **Latin-script** recognition, one model for English, French, German, Italian, Portuguese and Spanish, with accents. Adds `text` (lines top to bottom) and `confidence`. With Image Template regions it gives per-region text, table lines or cropped images. |
| PDF to Text (`text_and_ocr`) | Scanned pages, with little or no stored text, are OCR'd entirely. On other pages the text of pictures is added. |
| Key-Value Pair Extraction | OCR, then fuzzy key matching (similarity ≥ 0.8, so small OCR errors still match). The value is the rest of the line, or the next line. Repeats become `key2`, `key3` and so on. Empty keys raise the vendor's error. |
| Image Recognition | Transfer learning: the 1000 ImageNet class scores of **MobileNetV2** (14 MB) or **ResNet50-v2** (102 MB) feed a one-hidden-layer network trained with Adam for the chosen epochs, batch size and learning rate. Outputs **M** (for Predict), **E** (per-label precision, recall and accuracy) and **R** (accuracy and loss per epoch). The backbone is not fine-tuned. |

### First-use downloads

| Component | Download | Source |
|---|---|---|
| `vision-runtime` (OpenCV 4.13, pyclipper) | about 60 MB | pypi.org |
| `ocr-models` | about 10 MB | huggingface.co/monkt/paddleocr-onnx (Apache-2.0), pinned |
| `image-model:mobilenet_v2` / `image-model:resnet50_v2` | 14 / 102 MB | huggingface.co/onnxmodelzoo (Apache-2.0), pinned |

These need the `neural-runtime` (about 45 MB) from the Text Mining tools. All are installed once, after you confirm,
and the models are verified against their SHA-256 checksums (see `docs/TEXT_MINING.md`). Arabic, Japanese and
Chinese OCR are not included.

**Evidence:**
- `tests/test_batch12b.py` covers reading order, template regions scaled from the template, key matching with an
  OCR typo, values on the next line and repeats, image-recognition training and Predict, and the download
  preflight.
- `tests/integration/test_vision_addons_integration.py` runs the real models, in the `text-addons` CI job:
  - OCR of English and accented French text at confidence > 0.9
  - key-value extraction
  - a scanned PDF read by OCR
  - MobileNetV2 transfer learning that separates two kinds of picture
