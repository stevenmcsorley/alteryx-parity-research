//! Saving and opening chart images produced by engine chart tools.
//!
//! The webview cannot download files, so the chart viewer hands the image to these commands. Only the
//! engine's standalone SVG documents and PNG images (rasterised from them in the viewer) are accepted.

use base64::Engine as _;
use std::path::{Path, PathBuf};

const PNG_SIGNATURE: &[u8] = b"\x89PNG\r\n\x1a\n";
const MAX_BYTES: usize = 64 * 1024 * 1024;

fn is_chart_svg(text: &str) -> bool {
    text.starts_with("<svg xmlns=\"http://www.w3.org/2000/svg\"") && text.trim_end().ends_with("</svg>")
}

fn image_bytes(kind: &str, data: &str) -> Result<Vec<u8>, String> {
    if data.len() > MAX_BYTES * 2 {
        return Err("The chart image is too large".into());
    }
    let bytes = match kind {
        "svg" if is_chart_svg(data) => data.as_bytes().to_vec(),
        "svg" => return Err("Only Flowbench chart SVG documents can be saved".into()),
        "png" => {
            let bytes = base64::engine::general_purpose::STANDARD
                .decode(data)
                .map_err(|_| "The PNG image data is not valid base64".to_string())?;
            if !bytes.starts_with(PNG_SIGNATURE) {
                return Err("The image data is not a PNG".into());
            }
            bytes
        }
        _ => return Err("Charts can be saved as svg or png".into()),
    };
    if bytes.len() > MAX_BYTES {
        return Err("The chart image is too large".into());
    }
    Ok(bytes)
}

fn has_extension(path: &Path, kind: &str) -> bool {
    path.extension()
        .and_then(|e| e.to_str())
        .is_some_and(|e| e.eq_ignore_ascii_case(kind))
}

fn file_stem(name: &str) -> String {
    let cleaned: String = name
        .chars()
        .filter(|c| c.is_ascii_alphanumeric() || matches!(c, ' ' | '-' | '_'))
        .take(80)
        .collect();
    let trimmed = cleaned.trim();
    if trimmed.is_empty() {
        "chart".into()
    } else {
        trimmed.into()
    }
}

/// Write a chart to a path the user chose in the native save dialog.
#[tauri::command]
pub fn chart_save(path: String, kind: String, data: String) -> Result<String, String> {
    let bytes = image_bytes(&kind, &data)?;
    let target = PathBuf::from(&path);
    if !has_extension(&target, &kind) {
        return Err(format!("Choose a file name ending in .{kind}"));
    }
    std::fs::write(&target, bytes).map_err(|e| format!("Could not save the chart: {e}"))?;
    Ok(target.display().to_string())
}

/// Write a chart to the temporary folder and open it in the system's default viewer.
#[tauri::command]
pub fn chart_open(name: String, kind: String, data: String) -> Result<String, String> {
    let bytes = image_bytes(&kind, &data)?;
    let dir = std::env::temp_dir().join("flowbench-charts");
    std::fs::create_dir_all(&dir).map_err(|e| format!("Could not prepare the chart file: {e}"))?;
    let target = dir.join(format!("{}.{kind}", file_stem(&name)));
    std::fs::write(&target, bytes).map_err(|e| format!("Could not prepare the chart file: {e}"))?;
    open::that_detached(&target).map_err(|e| format!("Could not open the chart: {e}"))?;
    Ok(target.display().to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    const SVG: &str = "<svg xmlns=\"http://www.w3.org/2000/svg\" width=\"10\" height=\"10\"><rect/></svg>";

    #[test]
    fn accepts_only_engine_svg_and_real_png() {
        assert_eq!(image_bytes("svg", SVG).unwrap(), SVG.as_bytes());
        assert!(image_bytes("svg", "<svg onload=\"x\"></svg>").is_err());
        let png = base64::engine::general_purpose::STANDARD.encode(b"\x89PNG\r\n\x1a\nrest");
        assert!(image_bytes("png", &png).is_ok());
        let fake = base64::engine::general_purpose::STANDARD.encode(b"GIF89a");
        assert!(image_bytes("png", &fake).is_err());
        assert!(image_bytes("exe", SVG).is_err());
    }

    #[test]
    fn save_checks_the_extension_and_writes_the_file() {
        let dir = std::env::temp_dir().join(format!("flowbench-chart-test-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let wrong = dir.join("chart.txt");
        assert!(chart_save(wrong.display().to_string(), "svg".into(), SVG.into()).is_err());
        let right = dir.join("chart.SVG");
        chart_save(right.display().to_string(), "svg".into(), SVG.into()).unwrap();
        assert_eq!(std::fs::read_to_string(&right).unwrap(), SVG);
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn temporary_names_are_sanitised() {
        assert_eq!(file_stem("Mean & CI / 2024"), "Mean  CI  2024");
        assert_eq!(file_stem("../../"), "chart");
    }
}
