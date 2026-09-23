// Minimal Tauri entry — launches Nimna server then opens http://localhost:8001
// #![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]
fn main() {
  tauri::Builder::default()
    .setup(|_app| {
      // TODO: spawn `nimna serve --port 8001` as sidecar
      // std::process::Command::new("nimna").args(["serve","--port","8001"]).spawn().ok();
      Ok(())
    })
    .run(tauri::generate_context!())
    .expect("error while running tauri app");
}
