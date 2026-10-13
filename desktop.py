"""桌面端启动入口：python desktop.py

网页端与桌面端共用同一套核心代码（app.main:app），只有启动方式不同：
- 网页端：run.py 用 uvicorn 启动，浏览器访问；
- 桌面端：本文件在后台线程里跑同一个 ASGI 应用，再用 pywebview 打开原生窗口。

打包：pyinstaller --noconfirm mail_summary.spec → dist/MailSummary/MailSummary.exe
自检：python desktop.py --selftest（无窗口，仅验证服务/路径可用）
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
import time
from pathlib import Path

# 让根目录下的 desktop.py 也能直接找到 backend/app 包
BACKEND_DIR = Path(__file__).resolve().parent / "backend"
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


def _ensure_console_streams() -> None:
    """窗口模式（--noconsole）下 sys.stdout/stderr 为 None。

    loguru 的 ``logger.add(sys.stderr)`` 会因此抛 TypeError 导致启动失败，
    且异常在无控制台时只能弹窗、难以排查。这里在导入应用模块前把两个流
    指向 exe（或仓库根）下的日志文件，保证日志与 print 始终可用。
    """
    if sys.stdout is not None and sys.stderr is not None:
        return
    base = (
        Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent
    )
    try:
        log_dir = base / "data"
        log_dir.mkdir(parents=True, exist_ok=True)
        stream = open(log_dir / "desktop-console.log", "a", encoding="utf-8", buffering=1)
    except OSError:
        stream = open(os.devnull, "w", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


_ensure_console_streams()

import httpx  # noqa: E402
import uvicorn  # noqa: E402

from app.config import settings  # noqa: E402
from app.core.desktop_prefs import CLOSE_ACTION_TRAY, load_prefs  # noqa: E402
from app.main import app  # noqa: E402

WINDOW_TITLE = "多邮箱聚合摘要工具"

# 桌面端运行态：托盘图标与「关闭窗口」行为共享。
_exiting = False          # 用户在托盘菜单点了「退出」→ 之后允许窗口真正关闭
_tray_icon = None         # pystray.Icon 实例，退出时用它 stop()
_tray_lock = threading.Lock()



def _find_free_port(preferred: int) -> int:
    """从 preferred 起找到一个可用端口（被占用则依次 +1）。"""
    for port in range(preferred, preferred + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"从 {preferred} 起未找到可用端口")


def _wait_ready(url: str, timeout: float = 30.0) -> bool:
    """轮询健康检查，直到服务就绪或超时。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(url, timeout=1.0)
            if resp.status_code == 200:
                return True
        except Exception:  # noqa: BLE001 - 启动期连接失败属正常现象
            pass
        time.sleep(0.2)
    return False


class _ServerThread(threading.Thread):
    """在后台线程运行 uvicorn，窗口关闭后可优雅退出。"""

    def __init__(self, config: uvicorn.Config) -> None:
        super().__init__(daemon=True)
        self.server = uvicorn.Server(config)

    def run(self) -> None:
        self.server.run()

    def stop(self) -> None:
        self.server.should_exit = True


def _start_server(port: int) -> _ServerThread:
    """用同一份 app 对象启动 uvicorn（直接传对象，便于 PyInstaller 静态分析）。"""
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level=settings.log_level.lower(),
    )
    thread = _ServerThread(config)
    thread.start()
    return thread


def run_selftest(port: int) -> int:
    """无界面自检：启动服务 → 健康检查 → 打印结果后退出。用于验证开发/打包路径。"""
    settings.app_public_base_url = f"http://127.0.0.1:{port}"
    thread = _start_server(port)
    url = f"http://127.0.0.1:{port}/api/health"
    ok = _wait_ready(url)
    if ok:
        print(f"desktop selftest ok: {url}")
        print(f"静态目录: {settings.static_dir} (exists={settings.static_dir.exists()})")
        print(f"数据目录: {settings.data_dir}")
    else:
        print("desktop selftest failed: 健康检查超时", file=sys.stderr)
    thread.stop()
    time.sleep(0.5)
    return 0 if ok else 1


def _icon_path() -> Path | None:
    """托盘图标路径：打包后位于 _MEIPASS/assets，源码运行取仓库 assets/。"""
    base = Path(sys._MEIPASS) if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    candidate = base / "assets" / "icon.ico"
    return candidate if candidate.exists() else None


def _tray_image():
    """托盘图标：优先复用应用图标，缺失时用 PIL 画一个圆点兜底。"""
    from PIL import Image, ImageDraw

    path = _icon_path()
    if path is not None:
        try:
            return Image.open(path)
        except OSError:
            pass
    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    ImageDraw.Draw(image).ellipse((4, 4, 60, 60), fill=(15, 220, 120, 255))
    return image


def _show_window(window) -> None:
    """托盘菜单「显示主窗口」：把隐藏/最小化的窗口重新显示出来。"""
    window.show()
    try:
        window.restore()
    except Exception:  # noqa: BLE001 - 未最小化时 restore 会报错，忽略即可
        pass


def _stop_tray() -> None:
    with _tray_lock:
        icon = _tray_icon
    if icon is not None:
        try:
            icon.stop()
        except Exception:  # noqa: BLE001 - 托盘已退出时重复 stop 无副作用
            pass


def _quit_from_tray(window) -> None:
    """托盘菜单「退出」：解除关闭拦截 → 停托盘 → 销毁窗口，让 start() 返回。"""
    global _exiting
    _exiting = True
    _stop_tray()
    window.destroy()


def _start_tray(window) -> None:
    """在 GUI 循环启动后于独立线程运行系统托盘图标。

    托盘常驻（无论关闭行为是退出还是隐藏），提供「显示主窗口 / 退出」两个菜单项；
    pystray 缺失或创建失败时仅打印日志并继续，不影响主窗口正常使用。
    """
    global _tray_icon
    try:
        import pystray
        from pystray import Menu, MenuItem
    except Exception as exc:  # noqa: BLE001 - 托盘不可用不应导致程序无法启动
        print(f"[DESKTOP] 托盘不可用，已跳过：{exc}", file=sys.stderr)
        return

    menu = Menu(
        MenuItem("显示主窗口", lambda *_: _show_window(window), default=True),
        MenuItem("退出", lambda *_: _quit_from_tray(window)),
    )
    try:
        icon = pystray.Icon("MailSummary", _tray_image(), WINDOW_TITLE, menu)
    except Exception as exc:  # noqa: BLE001
        print(f"[DESKTOP] 创建托盘图标失败：{exc}", file=sys.stderr)
        return

    with _tray_lock:
        _tray_icon = icon
    try:
        icon.run()
    except Exception as exc:  # noqa: BLE001
        print(f"[DESKTOP] 托盘运行异常：{exc}", file=sys.stderr)


def _on_closing(window) -> bool:
    """窗口关闭事件处理：返回 False 表示拦截关闭。

    偏好为「隐藏到托盘」（tray）时隐藏窗口并在后台继续同步邮件；否则放行，
    让 ``webview.start`` 返回、进程正常退出。托盘菜单「退出」会把 _exiting
    置真，从而绕过这里的拦截。
    """
    if _exiting:
        return True
    if load_prefs().close_action == CLOSE_ACTION_TRAY:
        window.hide()
        print("[DESKTOP] 窗口已隐藏到托盘，程序在后台继续运行")
        return False
    return True


def run_desktop(port: int) -> None:
    """启动服务并打开原生窗口（阻塞直到窗口关闭）。"""
    import webview

    settings.app_public_base_url = f"http://127.0.0.1:{port}"
    settings.desktop_mode = True  # 让前端展示「桌面端」设置分区
    thread = _start_server(port)
    url = f"http://127.0.0.1:{port}"
    if not _wait_ready(f"{url}/api/health"):
        print("服务启动超时，仍尝试打开窗口…", file=sys.stderr)

    # text_select=True：pywebview 默认禁用页面文本选择，会导致邮件详情里的正文
    # 无法选中/复制；打开后正文可选中复制（页面无 contenteditable，故不可编辑）。
    window = webview.create_window(
        WINDOW_TITLE, url, width=1280, height=860, min_size=(900, 600), text_select=True
    )
    window.events.closing += _on_closing
    # private_mode=False + storage_path：否则 pywebview 会给 WebView2 一个临时
    # 用户数据目录，窗口一关 localStorage 就清空，主题/字号/侧栏折叠等偏好每次
    # 重启都回到默认值。这里固定到系统用户目录，与打包产物解耦。
    storage = settings.data_dir / "webview"
    storage.mkdir(parents=True, exist_ok=True)
    try:
        # 托盘在 GUI 循环启动后于独立线程运行：关闭窗口若命中「隐藏到托盘」，
        # 窗口会藏起来，靠托盘菜单重新显示或彻底退出。
        webview.start(_start_tray, (window,), private_mode=False, storage_path=str(storage))
    finally:
        _stop_tray()
        thread.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="多邮箱聚合摘要工具 · 桌面端")
    parser.add_argument("--port", type=int, default=settings.app_port, help="服务端口（默认取配置）")
    parser.add_argument("--selftest", action="store_true", help="启动服务并自检后退出（不打开窗口）")
    args = parser.parse_args()

    port = _find_free_port(args.port)
    if port != args.port:
        print(f"端口 {args.port} 被占用，改用 {port}")

    if args.selftest:
        raise SystemExit(run_selftest(port))
    run_desktop(port)


if __name__ == "__main__":
    main()