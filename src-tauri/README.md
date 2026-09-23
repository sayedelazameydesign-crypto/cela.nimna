# Tauri Wrapper — Nimna Desktop (Sprint 3)

Tغليف واجهة `Advanced Computer Use` ( `nimna/api/static/index.html` ) في تطبيق Tauri خفيف (Rust + Wry).

```bash
# متطلبات: Rust + Node
npm create tauri-app@latest   # أو cargo install tauri-cli
cargo tauri dev    # يفتح http://localhost:8001 داخل Webview
cargo tauri build  # ينتج .deb/.AppImage/.msi
```

- `tauri.conf.json` → `build.beforeDevCommand = "nimna serve --port 8001"` + `build.beforeBuildCommand`
- `src/main.rs` → يطلق الخادم المحلي (subprocess) ثم يفتح Webview على `http://localhost:8001`
- اختصارات نظام: `Ctrl+K` palette, `Ctrl+Shift+S` screenshot, `Ctrl+,` settings

الحالة: **Scaffold فقط** — البناء الكامل في Sprint 3 يتطلب Rust. الواجهة الحالية تعمل بدون Tauri عبر المتصفح.
