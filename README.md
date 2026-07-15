# yb_demo

大麦网余票查询工具集

## 功能

- **纯 API 模式**：通过 mtop 网关直接查询（`damai_h5.py`）
- **APP 自动化模式**：通过 uiautomator2 驱动大麦 APP 内 WebView（`damai_h5_u2.py`）
- **余票监控**：持续轮询余票状态（`damai_monitor.py`）

## 安装依赖

### 基础依赖（纯 API 模式）
```bash
pip install requests
```

### APP 自动化模式额外依赖
```bash
pip install uiautomator2
```

## 设备准备（APP 自动化模式）

1. **开启 USB 调试**：手机 → 设置 → 开发者选项 → USB 调试
2. **连接设备**：USB 连接电脑，运行 `adb devices` 确认设备可见
3. **安装大麦 APP**：确保手机上已安装大麦 APP（包名：`cn.damai`）
4. **开启 WebView 调试**（可选）：开发者选项 → WebView 调试

## 使用方法

### APP 自动化模式（推荐，更稳定）
```bash
# 交互式登录 + 搜索 + 查询余票
python damai_h5_u2.py

# 指定手机号
python damai_h5_u2.py --phone 15757176315

# 直接搜索指定演出
python damai_h5_u2.py --search "周杰伦演唱会"

# 指定设备
python damai_h5_u2.py --device abc123

# 跳过登录（使用已保存的 cookies）
python damai_h5_u2.py --skip-login --search "周杰伦演唱会"

# 详细日志
python damai_h5_u2.py --verbose
```

### 纯 API 模式
```bash
# 交互式登录 + 搜索 + 查询余票
python damai_h5.py

# 指定手机号
python damai_h5.py --phone 15757176315

# 直接搜索指定演出
python damai_h5.py --search "周杰伦演唱会"
```

### 余票监控
```bash
# 使用配置文件监控
python damai_monitor.py

# 指定演出 ID
python damai_monitor.py 825173765577

# 指定 cookie
python damai_monitor.py --cookie "cookie2=xxx; sgcookie=yyy"
```

## 配置文件

`damai_config.json`：
```json
{
    "item_id": "1061170881710",
    "cookie": "",
    "phone": "15757176315",
    "cookie_file": "damai_cookies.json",
    "interval": 3,
    "notify": false,
    "verbose": false
}
```

## 验证码说明

登录时需要短信验证码，程序会在发送验证码后暂停，提示你输入收到的验证码。

## 注意事项

- 本工具仅用于查看余票/开售状态，不做任何自动下单、抢票操作
- 大麦网对自动化访问有风控（滑块、行为校验等），频繁请求可能被限流
- 建议轮询间隔 >= 3 秒
- APP 自动化模式比纯 API 模式更不易被风控拦截
