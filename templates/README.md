# 图像模板匹配模板目录

在此目录存放用于 OpenCV 模板匹配的按钮/图标截图。

## 命名规范

- `btn_reserved.png` — 「已预约」按钮
- `btn_buy.png` — 「立即购买」按钮
- `btn_back.png` — 返回箭头
- `btn_close.png` — 关闭×按钮
- `tab_home.png` — 底部「首页」tab 图标
- `tab_mine.png` — 底部「我的」tab 图标
- `tab_search.png` — 底部「搜索」tab 图标

## 截图要求

1. **格式**：PNG（推荐）、JPG、BMP
2. **尺寸**：与目标设备分辨率接近即可，程序会自动缩放
3. **内容**：只包含按钮/图标本身，尽量裁切掉周围空白
4. **背景**：透明背景最佳，不透明背景也可以（matchTemplate 仍能匹配）

## 如何制作模板

1. 用 `adb shell screencap` 截取完整屏幕截图
2. 用图片编辑工具裁切出目标按钮/图标区域
3. 保存到本目录，按命名规范命名

## 示例

```bash
# 截取设备屏幕
adb shell screencap -p /sdcard/screen.png
adb pull /sdcard/screen.png

# 然后用图片编辑工具裁切出按钮区域，保存为 btn_reserved.png
```
