"""Generates build assets for packaging\\build.cmd: winpilot.ico (from WinPilot's tray icon)
and version_info.txt (the Windows version resource shown in the exe's Properties)."""
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
version = sys.argv[1] if len(sys.argv) > 1 else "2.0.0"
parts = (version.split(".") + ["0", "0", "0"])[:4]
nums = ", ".join(str(int(p)) for p in parts)

# Same drawing as pilot.tray_image(), at a larger size so every icon resolution stays sharp.
img = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((8, 8, 248, 248), radius=56, fill=(37, 99, 235))
d.polygon([(80, 48), (80, 200), (120, 164), (148, 220), (176, 208), (148, 152), (200, 152)], fill="white")
img.save(HERE / "winpilot.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])

(HERE / "version_info.txt").write_text(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({nums}), prodvers=({nums})),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Auto-Win-Mate'),
      StringStruct('FileDescription', 'WinPilot - tell an AI what to do on your desktop'),
      StringStruct('FileVersion', '{version}'),
      StringStruct('ProductName', 'WinPilot'),
      StringStruct('ProductVersion', '{version}'),
      StringStruct('LegalCopyright', 'Auto-Win-Mate contributors')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""", encoding="utf-8")
print(f"Assets ready for version {version}")
