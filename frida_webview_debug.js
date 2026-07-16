/**
 * Frida Hook：强制开启大麦 APP WebView 调试模式
 *
 * 原理：
 *   Android WebView 默认不暴露 DevTools 端口，
 *   需要应用代码调用 WebView.setWebContentsDebuggingEnabled(true)。
 *   大麦 APP 未开启此开关，导致 Chrome DevTools 无法连接（_wd is None）。
 *
 *   本脚本通过 Frida 在运行时强制调用该方法，
 *   使 WebView 在 /proc/net/unix 中暴露 webview_devtools_remote_<pid> 套接字，
 *   从而允许 adb forward localabstract: → 本地 TCP 端口 → CDP 连接。
 *
 * 使用：
 *   方式 1（推荐，spawn 模式，APP 启动前注入）：
 *     frida -U -f cn.damai -l frida_webview_debug.js --no-pause
 *
 *   方式 2（attach 模式，APP 已运行后注入）：
 *     frida -U cn.damai -l frida_webview_debug.js
 *
 *   方式 3（Python 集成，在自动化脚本中调用）：
 *     见下方 frida_webview_debug.py
 *
 * 依赖：
 *   pip install frida frida-tools
 *   手机需运行 frida-server（版本与 frida 一致）
 */

"use strict";

// ─── 颜色输出 ──────────────────────────────────────────────────────────────
var COLOR_RESET = "\x1b[0m";
var COLOR_GREEN = "\x1b[32m";
var COLOR_YELLOW = "\x1b[33m";
var COLOR_CYAN = "\x1b[36m";
var COLOR_RED = "\x1b[31m";

function logInfo(msg) {
    console.log(COLOR_CYAN + "[*] " + msg + COLOR_RESET);
}

function logSuccess(msg) {
    console.log(COLOR_GREEN + "[+] " + msg + COLOR_RESET);
}

function logWarn(msg) {
    console.log(COLOR_YELLOW + "[!] " + msg + COLOR_RESET);
}

function logError(msg) {
    console.log(COLOR_RED + "[-] " + msg + COLOR_RESET);
}

// ─── 核心：强制开启 WebView 调试 ────────────────────────────────────────────

function enableWebViewDebugging() {
    var enabled = false;

    // ── 1. android.webkit.WebView（标准 WebView） ──
    try {
        var WebView = Java.use("android.webkit.WebView");
        WebView.setWebContentsDebuggingEnabled(true);
        logSuccess("android.webkit.WebView.setWebContentsDebuggingEnabled(true) 已调用");
        enabled = true;
    } catch (e) {
        logWarn("android.webkit.WebView hook 失败：" + e);
    }

    // ── 2. com.tencent.smtt.sdk.WebView（腾讯 X5 WebView） ──
    try {
        var X5WebView = Java.use("com.tencent.smtt.sdk.WebView");
        X5WebView.setWebContentsDebuggingEnabled(true);
        logSuccess("com.tencent.smtt.sdk.WebView (X5) 调试已开启");
        enabled = true;
    } catch (e) {
        // 大麦可能不用 X5，忽略
    }

    // ── 3. com.uc.webview.SdkWebView（UC WebView） ──
    try {
        var UCWebView = Java.use("com.uc.webview.SdkWebView");
        UCWebView.setWebContentsDebuggingEnabled(true);
        logSuccess("com.uc.webview.SdkWebView (UC) 调试已开启");
        enabled = true;
    } catch (e) {
        // 忽略
    }

    // ── 4. chromium.webview.ChromeWebView（Chrome Custom Tab） ──
    try {
        var ChromeWebView = Java.use("org.chromium.webview.ChromeWebView");
        ChromeWebView.setWebContentsDebuggingEnabled(true);
        logSuccess("ChromeWebView 调试已开启");
        enabled = true;
    } catch (e) {
        // 忽略
    }

    return enabled;
}

// ─── Hook WebView 构造函数，确保每个实例都开启调试 ──────────────────────────

function hookWebViewConstructors() {
    var hookCount = 0;

    // 标准 WebView 构造函数
    try {
        var WebView = Java.use("android.webkit.WebView");

        // Hook 所有构造函数重载
        var overloads = WebView.$init.overloads;
        for (var i = 0; i < overloads.length; i++) {
            overloads[i].implementation = function () {
                // 先确保全局调试已开启
                Java.use("android.webkit.WebView").setWebContentsDebuggingEnabled(true);
                // 调用原构造函数
                return this.$init.apply(this, arguments);
            };
            hookCount++;
        }
        logSuccess("已 hook android.webkit.WebView 构造函数（" + hookCount + " 个重载）");
    } catch (e) {
        logWarn("WebView 构造函数 hook 失败：" + e);
    }

    // X5 WebView 构造函数
    try {
        var X5WebView = Java.use("com.tencent.smtt.sdk.WebView");
        var x5Overloads = X5WebView.$init.overloads;
        var x5Count = 0;
        for (var j = 0; j < x5Overloads.length; j++) {
            x5Overloads[j].implementation = function () {
                try {
                    Java.use("com.tencent.smtt.sdk.WebView").setWebContentsDebuggingEnabled(true);
                } catch (e) { }
                return this.$init.apply(this, arguments);
            };
            x5Count++;
        }
        if (x5Count > 0) {
            logSuccess("已 hook X5 WebView 构造函数（" + x5Count + " 个重载）");
        }
    } catch (e) {
        // 忽略
    }
}

// ─── Hook setWebContentsDebuggingEnabled 本身，拦截并强制为 true ──────────

function hookSetDebuggingEnabled() {
    try {
        var WebView = Java.use("android.webkit.WebView");

        WebView.setWebContentsDebuggingEnabled.implementation = function (enabled) {
            // 无论传入什么参数，都强制为 true
            logInfo("拦截 setWebContentsDebuggingEnabled(" + enabled + ") → 强制设为 true");
            this.setWebContentsDebuggingEnabled(true);
        };
        logSuccess("已 hook setWebContentsDebuggingEnabled，强制返回 true");
    } catch (e) {
        logWarn("hook setWebContentsDebuggingEnabled 失败：" + e);
    }
}

// ─── 监控 WebView 创建，输出调试信息 ────────────────────────────────────────

function hookWebViewForDiagnostics() {
    try {
        var WebView = Java.use("android.webkit.WebView");

        // Hook loadUrl，记录 WebView 加载的 URL
        WebView.loadUrl.overloads.forEach(function (overload) {
            overload.implementation = function () {
                var url = arguments[0] || "(unknown)";
                logInfo("WebView.loadUrl: " + url);
                return this.loadUrl.apply(this, arguments);
            };
        });
        logSuccess("已 hook WebView.loadUrl 用于诊断");
    } catch (e) {
        logWarn("WebView.loadUrl hook 失败：" + e);
    }
}

// ─── 检查当前已有的 WebView 实例 ────────────────────────────────────────────

function scanExistingWebViews() {
    logInfo("扫描已有的 WebView 实例…");

    try {
        Java.choose("android.webkit.WebView", {
            onMatch: function (instance) {
                var url = "(unknown)";
                try {
                    url = instance.getUrl();
                } catch (e) {
                    try {
                        url = instance.getOriginalUrl();
                    } catch (e2) { }
                }
                logSuccess("发现 WebView 实例：url=" + url);
            },
            onComplete: function () {
                logInfo("WebView 实例扫描完成");
            }
        });
    } catch (e) {
        logWarn("扫描 WebView 实例失败：" + e);
    }
}

// ─── Native 层：Hook DevToolsAgent 启动 ─────────────────────────────────────

function hookNativeDevTools() {
    // 某些 ROM 的 WebView 实现可能在 native 层控制调试开关
    // 尝试 hook chromium 的 DevTools 相关函数

    try {
        // 查找 libwebviewchromium.so 中的调试相关符号
        var modules = Process.enumerateModules();
        var webviewModules = modules.filter(function (m) {
            return m.name.toLowerCase().indexOf("webview") >= 0 ||
                   m.name.toLowerCase().indexOf("chromium") >= 0;
        });

        if (webviewModules.length > 0) {
            logInfo("发现 WebView 相关 native 模块：");
            webviewModules.forEach(function (m) {
                logInfo("  " + m.name + " @ " + m.base + " (size: " + m.size + ")");
            });
        } else {
            logInfo("未发现 WebView native 模块（可能尚未加载）");
        }
    } catch (e) {
        logWarn("Native 模块枚举失败：" + e);
    }
}

// ─── 主入口 ────────────────────────────────────────────────────────────────

logInfo("══════════════════════════════════════════════════");
logInfo("  Frida WebView Debug Hook for cn.damai");
logInfo("  强制开启 WebView 调试模式");
logInfo("══════════════════════════════════════════════════");

Java.perform(function () {
    logInfo("Java VM 已就绪，开始注入…");

    // Step 1: 立即开启全局调试开关
    var ok = enableWebViewDebugging();
    if (ok) {
        logSuccess("WebView 调试模式已全局开启！");
    } else {
        logError("所有 WebView 类均 hook 失败，请确认 APP 已加载 WebView");
    }

    // Step 2: Hook setWebContentsDebuggingEnabled，防止 APP 关闭调试
    hookSetDebuggingEnabled();

    // Step 3: Hook WebView 构造函数，确保新创建的 WebView 也开启调试
    hookWebViewConstructors();

    // Step 4: 诊断 hook（可选，记录 WebView 加载的 URL）
    hookWebViewForDiagnostics();

    // Step 5: 扫描已有的 WebView 实例
    scanExistingWebViews();

    // Step 6: Native 层诊断
    hookNativeDevTools();

    logSuccess("所有 hook 注入完成！WebView DevTools 端口已暴露。");
    logInfo("现在可以在另一个终端运行：");
    logInfo("  adb shell cat /proc/net/unix | grep devtools");
    logInfo("  adb forward tcp:9222 localabstract:webview_devtools_remote_<pid>");
    logInfo("或直接运行 damai_reserve_u2.py 脚本");
});
