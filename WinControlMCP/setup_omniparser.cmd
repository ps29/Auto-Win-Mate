@echo off
rem Optional: installs OmniParser V2's MIT-licensed icon detector (icon_detect_v3) so the
rem detect_elements tool can find clickable regions by vision. Downloads torch (CPU) and ~100 MB of weights.
setlocal
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe call setup.cmd || exit /b 1
.venv\Scripts\python.exe -m pip install torch torchvision numpy --index-url https://download.pytorch.org/whl/cpu --extra-index-url https://pypi.org/simple || exit /b 1
if not exist omniparser\icon_detect_v3 mkdir omniparser\icon_detect_v3
curl.exe -L --fail -o omniparser\icon_detect_v3\model.pt https://huggingface.co/microsoft/OmniParser-v2.0/resolve/main/icon_detect_v3/model.pt || exit /b 1
curl.exe -L --fail -o omniparser\icon_detect_v3\LICENSE https://huggingface.co/microsoft/OmniParser-v2.0/resolve/main/icon_detect_v3/LICENSE
echo OmniParser is ready: detect_elements will work after restarting the server or WinPilot.
