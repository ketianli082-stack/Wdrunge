// Learn more about Tauri commands at https://tauri.app/develop/calling-rust/
use tauri::{Manager, PhysicalSize};

#[tauri::command]
fn greet(name: &str) -> String {
    format!("Hello, {}! You've been greeted from Rust!", name)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![greet])
        .setup(|app| {
            let window = app
                .get_webview_window("main")
                .expect("main window is missing");
            let size =
                if let Some(monitor) = window.current_monitor()?.or(window.primary_monitor()?) {
                    let screen = monitor.size();
                    PhysicalSize::new(
                        1920_u32.max((f64::from(screen.width) * 0.75).round() as u32),
                        1080_u32.max((f64::from(screen.height) * 0.75).round() as u32),
                    )
                } else {
                    PhysicalSize::new(1920, 1080)
                };
            window.set_size(size)?;
            window.center()?;
            window.show()?;
            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
