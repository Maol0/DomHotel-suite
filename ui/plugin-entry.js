// domhotel-suite / ui / plugin-entry.js (v2.1.2-test1: pluginId auto-resolved)
//
// QwenPaw console 框架要求:
//   - window.QwenPaw.host.React 提供 createElement / useState / memo
//   - registerRoutes(pluginId, [...]) 第一个参数必须是 plugin 的 id (会用作 Sp.add 的 pluginId)
//   - registerRoutes 内部 Sp.add(pluginId, {id, path, component}) —— component 是 React 组件函数
//   - AppCenter 通过 Sp.snapshot() 找 entry_page 对应路由, Component 渲染
//
// 修复(2026-08-05 QA Agent): pluginId 不能硬编码 "domhotel-suite"
//   - pluginId 必须跟 plugin.json 一致,
//     否则 Sp.add 找 entry_page 路由时 pluginId 不匹配, Component=undefined → 404
//
// 此 wrapper:
//   1) 从 entry 脚本的 script 标签上读 data-plugin-id (由 QwenPaw loader 注入)
//   2) 读 <base href> 解析 /api/{pluginId}/ui/ 作为 iframe src
//   3) fallback: 拿 <script src> 路径里的 /api/{id}/ 段

(function () {
  "use strict";

  var QP = (typeof window !== "undefined" && window.QwenPaw) || null;
  if (!QP) {
    if (typeof console !== "undefined" && console.error) {
      console.error("[domhotel-suite] window.QwenPaw not found; plugin loader may have failed");
    }
    return;
  }

  var React = QP.host && QP.host.React;
  if (!React || typeof React.createElement !== "function") {
    if (typeof console !== "undefined" && console.error) {
      console.error("[domhotel-suite] window.QwenPaw.host.React not available");
    }
    return;
  }

  // [v2.1.9] 企微 WebView 强制跳转: 手机端企微应用打开根 URL / /apps 时
  //   跳到 /apps/<pluginId>, 让用户直接看到酒店工作台而不是 QwenPaw 工作台
  //   - 检测 UA (企微内置 WebView): wxwork / wxworkcorp / MicroMessenger (企微也用)
  //   - 检测路径: 只在根 / /apps 跳, 不在其他路径跳 (避免无限重定向)
  //   - 用 replace 而不是 href, 不留历史记录
  //   - 失败兜底: try/catch 包裹, 出错静默继续 (不阻断 plugin 加载)
  //   - 必须在 pluginId 解析之前先临时用一个占位 pluginId (因为 pluginId 还没解析)
  try {
    var ua = (typeof navigator !== "undefined" && navigator.userAgent) || "";
    var isWecomUA = /wxwork|wxworkcorp|MicroMessenger/i.test(ua);
    var curPath = (typeof window !== "undefined" && window.location && window.location.pathname) || "";
    // 根路径 / /apps / /apps/ 都视为"在 QwenPaw 工作台首页"
    var isRootOrApps = curPath === "/" || curPath === "/apps" || curPath === "/apps/" || curPath === "";
    if (isWecomUA && isRootOrApps) {
      // 临时用占位 pluginId, 后面的 pluginId 解析会覆盖它
      // 真正跳转的目标路径 = /apps/domhotel-suite (与 plugin.json meta.pawapp.entry_page 一致)
      var targetPath = "/apps/domhotel-suite";
      if (curPath !== targetPath) {
        if (typeof console !== "undefined" && console.log) {
          console.log("[domhotel-suite] wecom WebView detected, redirect " + curPath + " -> " + targetPath);
        }
        // 用 replace 不留历史; 同步跳转, 不需要 await
        window.location.replace(targetPath);
        // replace 之后页面会卸载, return 防止后面代码继续跑
        return;
      }
    }
  } catch (e) {
    if (typeof console !== "undefined" && console.warn) {
      console.warn("[domhotel-suite] wecom redirect check failed:", e);
    }
  }

  // 1) 解析 pluginId — 优先从当前执行的 <script> 标签读 data-plugin-id
  var pluginId = "";
  try {
    var scripts = document.getElementsByTagName("script");
    for (var i = 0; i < scripts.length; i++) {
      var s = scripts[i];
      if (s.src && s.src.indexOf("/ui/plugin-entry.js") !== -1) {
        // 优先 data-plugin-id
        if (s.getAttribute("data-plugin-id")) {
          pluginId = s.getAttribute("data-plugin-id");
          break;
        }
        // fallback: 从 src 里抠 /api/{pluginId}/ui/plugin-entry.js
        var m = s.src.match(/\/api\/([^\/]+)\/ui\/plugin-entry\.js/);
        if (m) { pluginId = m[1]; break; }
        // fallback: src 里 /pawapps/{pluginId}/ui/plugin-entry.js
        var m2 = s.src.match(/\/pawapps\/([^\/]+)\/ui\/plugin-entry\.js/);
        if (m2) { pluginId = m2[1]; break; }
      }
    }
  } catch (e) { /* 忽略, 走下一 fallback */ }

  // 2) fallback: QwenPaw.host 可能注入 pluginMeta
  if (!pluginId && QP.host && QP.host.pluginMeta && QP.host.pluginMeta.id) {
    pluginId = QP.host.pluginMeta.id;
  }

  // 3) 终极 fallback: 用 manifest 推断 (不行就放弃, 至少报错明显)
  if (!pluginId) {
    pluginId = "domhotel-suite";  // 与 plugin.json id 一致 (降级 fallback)
    if (typeof console !== "undefined" && console.warn) {
      console.warn("[domhotel-suite] 无法自动解析 pluginId, fallback 到:", pluginId);
    }
  }

  // [v2.1.8] 隐藏 QwenPaw 控制台浮动胶囊 (右下角"X 关闭浮动气泡")
  // plugin.json 的 meta.hide_floating_capsule 字段当前 QwenPaw 2.0.1 控制台不读取,
  // 所以直接注入 CSS 强制隐藏 (覆盖控制台的 CSS module class 名 hash)
  try {
    if (typeof document !== "undefined" && document.head) {
      var hideCss = document.getElementById("domhotel-hide-capsule");
      if (!hideCss) {
        hideCss = document.createElement("style");
        hideCss.id = "domhotel-hide-capsule";
        // 覆盖 3 种可能的选择器:
        // 1) CSS module hash 类名 (从 console/assets/index-CfpiNiXc.js 看到的 hash 是 bCgHK)
        // 2) 通用 [class*="floatingCapsule"] 子串匹配
        // 3) capsuleBtn/capsuleDivider/capsuleDots/capsuleCloseIcon 等子元素
        hideCss.textContent =
          "[class*=\"floatingCapsule\"]," +
          ".index-module__floatingCapsule__bCgHK," +
          "[class*=\"capsuleBtn\"]," +
          "[class*=\"capsuleDivider\"]," +
          "[class*=\"capsuleDots\"]," +
          "[class*=\"capsuleCloseIcon\"]," +
          "[class*=\"embedPage\"] [class*=\"floating\"] {" +
          "  display: none !important;" +
          "  visibility: hidden !important;" +
          "  opacity: 0 !important;" +
          "  pointer-events: none !important;" +
          "}";
        document.head.appendChild(hideCss);
      }
    }
  } catch (e) {
    if (typeof console !== "undefined" && console.warn) {
      console.warn("[domhotel-suite] hide floating capsule failed:", e);
    }
  }

  // iframe src: /api/{pluginId}/ui/ (framework 会代理到 /pawapps/{id}/static/ui/index.html)
  var host = QP.host;
  var apiBase = "/api/" + pluginId + "/";
  var htmlSrc = (host && typeof host.getApiUrl === "function")
    ? host.getApiUrl("/" + pluginId + "/ui/")
    : apiBase + "ui/";

  // React 组件: 渲染 iframe (同源, HTML 内 fetch('/api/<id>/...') 正常工作)
  //
  // 关键: **不要**用 position:fixed + 100vw/100vh — 那会让 iframe 撑破父容器,
  // 在 QwenPaw 2.1.0 桌面 OS 模式 (Tauri 包装 + DesktopOS window manager) 下
  // 不能跟随 OSWindow 缩放、最大化、最小化。
  //
  // 正确做法 (与 qwenpaw-workflow-studio 保持一致):
  //   - 外层 div 100% 父容器,无 fixed 定位
  //   - iframe 100% 父容器
  //   - 父容器是 QwenPaw Router 提供的 <Route element> wrapper,自带 100% 高度
  //     (DesktopOS 的 <os> 包装器用 MemoryRouter + Routes 给 plugin 路由上下文)
  function DomHotelSuiteApp() {
    return React.createElement("div", {
      style: {
        width: "100%",
        height: "100%",
        background: "#f0f2f5",
        display: "flex",
        flexDirection: "column",
        overflow: "hidden"
      }
    }, React.createElement("iframe", {
      src: htmlSrc,
      style: {
        width: "100%",
        height: "100%",
        flex: 1,
        border: "none",
        display: "block",
        background: "#f0f2f5"
      },
      allow: "clipboard-read; clipboard-write",
      referrerPolicy: "no-referrer",
      title: "DomHotel 智能酒店套件"
    }));
  }

  // 路由注册: path 必须与 plugin.json meta.entry_page 一致 (默认 /apps/<id>)
  var entryPath = "/apps/" + pluginId;
  var registered = false;
  try {
    if (typeof QP.registerRoutes === "function") {
      QP.registerRoutes(pluginId, [{
        path: entryPath,
        component: DomHotelSuiteApp
      }]);
      registered = true;
    }
  } catch (e) {
    if (typeof console !== "undefined" && console.error) {
      console.error("[domhotel-suite] registerRoutes failed:", e);
    }
  }

  // 菜单注册 (可选; 全屏模式不影响)
  try {
    if (QP.host && QP.host.menu && typeof QP.host.menu.add === "function") {
      QP.host.menu.add({
        id: pluginId,
        title: "🏨 DomHotel 智能酒店套件",
        icon: "🏨",
        group: "智能酒店",
        path: entryPath
      });
    }
  } catch (e) {
    // 菜单注册失败不致命
  }

  if (typeof console !== "undefined" && console.log) {
    console.log(
      "[domhotel-suite] v2.1.2-test1 plugin-entry loaded" +
      " (pluginId=" + pluginId +
      ", path=" + entryPath +
      ", registered=" + registered +
      ", htmlSrc=" + htmlSrc + ")"
    );
  }
})();
