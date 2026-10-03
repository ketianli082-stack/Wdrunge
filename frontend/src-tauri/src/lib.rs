// Learn more about Tauri commands at https://tauri.app/develop/calling-rust/
use tauri::{Manager, PhysicalSize};

#[tauri::command]
fn greet(name: &str) -> String {
    format!("Hello, {}! You've been greeted from Rust!", name)
}

#[tauri::command]
fn execution_control_token(app: tauri::AppHandle) -> Result<String, String> {
    let base = app.path().local_data_dir().map_err(|error| error.to_string())?;
    let directory = if cfg!(target_os = "windows") {
        base.join("Wdrunge").join("Wdrunge")
    } else {
        base.join("Wdrunge")
    };
    std::fs::read_to_string(directory.join("execution-token"))
        .map(|token| token.trim().to_string())
        .map_err(|_| "后端执行授权尚未准备好".to_string())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![greet, execution_control_token])
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
