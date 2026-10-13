# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller 打包配置：pyinstaller --noconfirm --distpath . mail_summary.spec

产物：项目根目录下的单文件 MailSummary.exe（免安装、免 _internal 目录）。
网页端与桌面端共用同一套核心代码，本配置只负责：
- 把静态前端 backend/static 收进包内（运行时位于 sys._MEIPASS/backend/static）；
- 使用 assets/icon.ico 作为程序图标（快捷方式/任务栏/窗口）；
- 补齐 uvicorn / 应用内惰性导入 / pywebview Windows 后端的 hiddenimports。
"""
from pathlib import Path

ROOT = Path(SPECPATH)

hiddenimports = [
    # uvicorn 运行期按字符串动态导入
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "uvicorn.lifespan.off",
    # 应用内惰性导入（session.init_db / sync_service.provider 工厂）
    "app.db.migrate",
    "app.models",
    "app.services.providers.gmail",
    "app.services.providers.graph",
    # pywebview Windows 后端
    "webview.platforms.edgechromium",
    "webview.platforms.winforms",
    # APScheduler 定时同步
    "apscheduler.schedulers.background",
    "apscheduler.triggers.interval",
    # 系统托盘：pystray 按平台动态选择后端，Windows 用 _win32
    "pystray",
    "pystray._base",
    "pystray._util",
    "pystray._win32",
]

a = Analysis(
    ["desktop.py"],
    pathex=[str(ROOT), str(ROOT / "backend")],
    binaries=[],
    datas=[
        (str(ROOT / "backend" / "static"), "backend/static"),
        # 托盘图标运行时从 assets/ 读取
        (str(ROOT / "assets"), "assets"),
    ],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)


pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="MailSummary",
    icon=str(ROOT / "assets" / "icon.ico"),  # 应用图标（角色形象，已去白底）
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,  # 桌面应用不弹控制台窗口；排查时可临时改 True
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)