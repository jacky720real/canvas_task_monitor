"""启动脚本：双击即可跑起 Web UI（存在性 + 关键内容 + UTF-8 BOM 编码）。

【为什么要钉编码】cmd 的 .bat 若存成"无 BOM 的 UTF-8"，中文提示会乱码；
UTF-8 with BOM（Windows 10 1903+ 支持）才稳。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = ("启动.bat", "start.bat")


def test_start_bat_exists_and_invokes_web_main() -> None:
    """启动脚本存在，且确实调用 web_main.py。"""
    existing = [ROOT / name for name in CANDIDATES if (ROOT / name).is_file()]
    assert existing, "至少应存在一个启动脚本（启动.bat 或 start.bat）"

    content = existing[0].read_text(encoding="utf-8", errors="replace")
    assert "web_main.py" in content
    assert ".venv" in content


def test_start_bat_is_utf8_with_bom() -> None:
    """必须是 UTF-8 with BOM，否则 cmd 里中文乱码。"""
    for name in CANDIDATES:
        path = ROOT / name
        if path.is_file():
            assert path.read_bytes()[:3] == b"\xef\xbb\xbf", f"{name} 应为 UTF-8 with BOM"
