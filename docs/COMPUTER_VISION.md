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

## OCR (next batch)

These need an OCR engine, which will download on first use like the text-mining models:

- Image to Text
- scanned pages in PDF to Text (`text_and_ocr`)
- Key-Value Pair Extraction
- Image Recognition
