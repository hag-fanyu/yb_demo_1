#!/usr/bin/env python3
"""下载 frida-server 并推送到手机"""
import urllib.request
import subprocess
import sys
import os

FRIDA_VERSION = "17.15.5"
ABI = "arm64"
FILENAME_XZ = f"frida-server-{FRIDA_VERSION}-android-{ABI}.xz"
FILENAME = f"frida-server-{FRIDA_VERSION}-android-{ABI}"
# 国内镜像（优先），失败则回退 GitHub
MIRRORS = [
    f"https://ghfast.top/https://github.com/frida/frida/releases/download/{FRIDA_VERSION}/{FILENAME_XZ}",
    f"https://gh-proxy.com/https://github.com/frida/frida/releases/download/{FRIDA_VERSION}/{FILENAME_XZ}",
    f"https://mirror.ghproxy.com/https://github.com/frida/frida/releases/download/{FRIDA_VERSION}/{FILENAME_XZ}",
    f"https://github.com/frida/frida/releases/download/{FRIDA_VERSION}/{FILENAME_XZ}",
]
OUT_DIR = "D:\\"
OUT_XZ = os.path.join(OUT_DIR, FILENAME_XZ)
OUT_BIN = os.path.join(OUT_DIR, FILENAME)
ADB = r"D:\platform-tools\adb.exe"

# Step 1: 下载
print(f"正在下载 {FILENAME_XZ} ...")
import ssl
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

downloaded = False
for URL in MIRRORS:
    print(f"尝试下载: {URL[:80]}...")
    try:
        req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, context=ctx, timeout=30)
        with open(OUT_XZ, "wb") as f:
            while True:
                chunk = resp.read(65536)
                if not chunk:
                    break
                f.write(chunk)
        size = os.path.getsize(OUT_XZ)
        if size > 1000000:  # 至少 1MB
            print(f"下载完成: {OUT_XZ} ({size/1024/1024:.1f} MB)")
            downloaded = True
            break
        else:
            print(f"文件太小({size}B)，可能下载了错误页面，尝试下一个镜像...")
            os.remove(OUT_XZ)
    except Exception as e:
        print(f"此镜像失败: {e}")
        continue

if not downloaded:
    print("所有镜像均失败！请手动下载：")
    print(f"  https://github.com/frida/frida/releases/tag/{FRIDA_VERSION}")
    print(f"  下载 {FILENAME_XZ} 放到 {OUT_DIR}")
    sys.exit(1)

# Step 2: 解压 xz
print("正在解压 xz ...")
try:
    import lzma
    with open(OUT_XZ, "rb") as f_in:
        data = lzma.decompress(f_in.read())
    with open(OUT_BIN, "wb") as f_out:
        f_out.write(data)
    size = os.path.getsize(OUT_BIN)
    print(f"解压完成: {OUT_BIN} ({size/1024/1024:.1f} MB)")
except Exception as e:
    print(f"解压失败: {e}")
    print("尝试用 7z 或手动解压...")
    sys.exit(1)

# Step 3: 推送到手机
print("正在推送到手机 /data/local/tmp/ ...")
try:
    r = subprocess.run(
        [ADB, "push", OUT_BIN, "/data/local/tmp/frida-server"],
        capture_output=True, text=True, timeout=60
    )
    print(r.stdout)
    if r.returncode != 0:
        print(f"推送失败: {r.stderr}")
        sys.exit(1)
except Exception as e:
    print(f"推送失败: {e}")
    sys.exit(1)

# Step 4: 设置权限
print("正在设置权限 ...")
subprocess.run([ADB, "shell", "chmod 755 /data/local/tmp/frida-server"])

# Step 5: 验证
print("验证 frida-server 版本 ...")
r = subprocess.run(
    [ADB, "shell", "/data/local/tmp/frida-server --version"],
    capture_output=True, text=True, timeout=5
)
print(f"设备上 frida-server 版本: {r.stdout.strip()}")

print("\n✅ 全部完成！")
print("启动 frida-server: adb shell \"/data/local/tmp/frida-server &\"")
