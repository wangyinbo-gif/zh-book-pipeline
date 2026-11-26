/* 筑学书库 · 前端认证/CSRF 客户端（认证契约 v1 / 任务 S-C）
 *
 * 加载顺序：必须在 shelf.js / reader.js / practice.js 之前引入（各页 script 标签已就位）。
 *
 * 职责
 *   1. 启动时 GET /api/auth/status，把 {authed, mode, csrf} 缓存在内存；
 *   2. 导出 window.zhuxueFetch(url, opts)：
 *        - mode === 'session' 且方法非 GET/HEAD 的写请求 → 自动加 X-CSRF-Token 头；
 *        - 401 → location = /login.html?next=<encodeURIComponent(pathname+search)>；
 *        - 403 → 一次性提示条（本页内只出现一次）；
 *        - mode === 'open'（以及 status 探测失败/离线 file:// 打包）→ 直接透传 window.fetch，
 *          行为与原生 fetch 完全一致（零变化底线）。
 *   3. session 模式下在书架顶栏注入「退出」按钮（open 模式不出现任何 UI 变化）。
 *
 * 安全约束（不得放松）
 *   - csrf 只存在本闭包变量里：绝不写 localStorage / sessionStorage / cookie / URL / console。
 *     持久化的只有服务端下发的 HttpOnly SameSite=Strict 会话 Cookie，浏览器自动携带，脚本读不到。
 *   - 任何 token / cookie / csrf 的值都不打印日志。
 *   - 离线包（file:// 协议 + offline-api.js 接管 window.fetch）不探测、不改写、不跳转，
 *     保证导出的离线书库零影响。
 */
(function () {
  'use strict';

  var LOGIN_PATH = '/login.html';
  var STATUS_URL = '/api/auth/status';
  var LOGOUT_URL = '/api/auth/logout';
  var STATUS_TIMEOUT_MS = 2500;      // 探测超时后按“未知”处理，退回原生 fetch 行为
  var CSRF_RE = /^[0-9a-f]{8,64}$/;  // 只接受 hex 形态的 csrf，异常值不入内存
  // auth 三入口不做 CSRF、不触发 401 跳转（避免登录/登出流程自我打断）
  var AUTH_EXEMPT = { '/api/auth/login': 1, '/api/auth/logout': 1, '/api/auth/status': 1 };

  var state = { started: false, ready: false, mode: 'unknown', authed: false, csrf: '', offline: false };
  var statusPromise = null;
  var redirected = false;   // 401 只跳一次，避免并发请求重复赋值 location
  var noticeShown = false;  // 403 提示条一次性

  function isOffline() {
    try { return location.protocol === 'file:'; } catch (e) { return true; }
  }

  /* ---------- 动态取 window.fetch：离线包的 offline-api.js 会接管它，不能缓存旧引用 ---------- */
  function nativeFetch(input, init) {
    return window.fetch(input, init);
  }

  function pathOf(input, init) {
    var url = '';
    try {
      if (typeof input === 'string') url = input;
      else if (input && typeof input.url === 'string') url = input.url;
      else url = String(input || '');
    } catch (e) { url = ''; }
    if (!url) return '';
    var qi = url.indexOf('?');
    var s = qi < 0 ? url : url.slice(0, qi);
    var hi = s.indexOf('#');
    if (hi >= 0) s = s.slice(0, hi);
    if (s.charAt(0) !== '/') {                 // 绝对 URL 只留 path 部分再判断
      var m = /^[a-zA-Z][a-zA-Z0-9+.-]*:\/\/[^/?#]*(.*)$/.exec(s);
      s = m ? m[1] : s;
    }
    return s;
  }

  function methodOf(input, init) {
    var m = (init && init.method) || (input && typeof input.method === 'string' ? input.method : '') || 'GET';
    return String(m).toUpperCase();
  }

  function isAuthExempt(path) { return !!AUTH_EXEMPT[path]; }

  /* ---------- 状态探测（内存缓存，单次） ---------- */
  function applyStatus(j) {
    if (!j || typeof j !== 'object') return false;
    if (j.mode !== 'session' && j.mode !== 'open') return false;
    state.mode = j.mode;
    state.authed = !!j.authed;
    var c = j.csrf;
    state.csrf = (typeof c === 'string' && CSRF_RE.test(c)) ? c : '';
    return true;
  }

  function fetchStatus() {
    if (statusPromise) return statusPromise;
    statusPromise = new Promise(function (resolve) {
      if (isOffline()) {                 // 离线书库：不发探测请求，一切照旧
        state.offline = true;
        state.mode = 'unknown';
        state.ready = true;
        resolve(state);
        return;
      }
      var settled = false;
      var finish = function () {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        state.ready = true;
        resolve(state);
      };
      var timer = setTimeout(finish, STATUS_TIMEOUT_MS);
      state.started = true;
      var p;
      try {
        p = Promise.resolve(nativeFetch(STATUS_URL, { method: 'GET', credentials: 'same-origin', cache: 'no-store' }));
      } catch (e) { finish(); return; }
      p.then(function (r) {
        if (!r || !r.ok) return null;
        return r.json().catch(function () { return null; });
      }).then(function (j) {
        applyStatus(j);   // 拿不到合法 mode 就保持 unknown → 零改写
        finish();
      }, function () { finish(); });
    });
    return statusPromise;
  }

  /* ---------- 401：跳登录页，带同源 next ---------- */
  function loginHref() {
    var next = '';
    try { next = location.pathname + (location.search || ''); } catch (e) { next = ''; }
    if (!next || next === LOGIN_PATH || next.indexOf(LOGIN_PATH) === 0) return LOGIN_PATH;
    return LOGIN_PATH + '?next=' + encodeURIComponent(next);
  }

  function gotoLogin() {
    if (redirected) return;
    redirected = true;
    try { location.href = loginHref(); } catch (e) { /* ignore */ }
  }

  /* ---------- 403：一次性提示条（内联样式，不依赖 style.css） ---------- */
  function csrfNotice() {
    if (noticeShown) return;
    noticeShown = true;
    try {
      var bar = document.createElement('div');
      bar.setAttribute('role', 'alert');
      bar.style.cssText = 'position:fixed;left:0;right:0;top:0;z-index:9999;' +
        'background:#8f2f1f;color:#fff;font:13px/1.6 "PingFang SC","Microsoft YaHei",system-ui,sans-serif;' +
        'padding:9px 44px 9px 14px;text-align:center;box-shadow:0 2px 10px rgba(0,0,0,.18);';
      var txt = document.createElement('span');
      txt.textContent = '这个页面的会话校验没通过（可能已登录超时或口令已更换）。请刷新页面，或重新登录一次。';
      var close = document.createElement('button');
      close.textContent = '知道了';
      close.type = 'button';
      close.style.cssText = 'position:absolute;right:10px;top:50%;transform:translateY(-50%);' +
        'border:1px solid rgba(255,255,255,.6);background:transparent;color:#fff;border-radius:6px;' +
        'padding:2px 8px;font-size:12px;cursor:pointer;';
      close.addEventListener('click', function () { if (bar.parentNode) bar.parentNode.removeChild(bar); });
      bar.appendChild(txt);
      bar.appendChild(close);
      (document.body || document.documentElement).appendChild(bar);
      setTimeout(function () { if (bar.parentNode) bar.parentNode.removeChild(bar); }, 12000);
    } catch (e) { /* 提示条失败不影响请求本身 */ }
  }

  /* ---------- 给写请求补 X-CSRF-Token ---------- */
  function withCsrf(input, init) {
    var out = {};
    var src = init || {};
    var k;
    for (k in src) if (Object.prototype.hasOwnProperty.call(src, k)) out[k] = src[k];
    var h = src.headers;
    var merged;
    if (window.Headers && h instanceof window.Headers) {
      merged = new window.Headers(h);
    } else if (Array.isArray(h)) {
      merged = new window.Headers(h);
    } else if (h && typeof h === 'object') {
      merged = new window.Headers();
      for (k in h) if (Object.prototype.hasOwnProperty.call(h, k)) merged.append(k, h[k]);
    } else {
      merged = new window.Headers();
    }
    // input 是 Request 对象时（当前业务代码未用到，留作兼容）：先并入它自带的 header
    if (!h && window.Request && input instanceof window.Request) {
      try { input.headers.forEach(function (v, k) { merged.append(k, v); }); } catch (e) { /* ignore */ }
    }
    // 已有同名头就以已有值为准（尊重调用方），否则补内存里的 csrf
    if (!merged.has('X-CSRF-Token') && state.csrf) merged.set('X-CSRF-Token', state.csrf);
    out.headers = merged;
    if (!out.credentials) out.credentials = 'same-origin';
    return out;
  }

  /* ---------- 对外主入口 ---------- */
  function zhuxueFetch(input, init) {
    return fetchStatus().then(function () {
      // open / unknown / 离线：原样透传，零改写、零拦截
      if (state.mode !== 'session') return nativeFetch(input, init);

      var path = pathOf(input, init);
      if (isAuthExempt(path)) return nativeFetch(input, init);

      var method = methodOf(input, init);
      var write = method !== 'GET' && method !== 'HEAD';
      var opts = init;
      if (write && state.csrf) opts = withCsrf(input, init);
      else if (write && !state.csrf) {
        // csrf 还没拿到（例如刚登录就跳转过来）：先补一次探测再发，避免白吃 403
        return restatus().then(function () {
          if (state.csrf) opts = withCsrf(input, init);
          return send(input, opts, method);
        });
      }
      return send(input, opts, method);
    });
  }

  function restatus() {
    statusPromise = null;
    return fetchStatus();
  }

  function send(input, opts, method) {
    return Promise.resolve(nativeFetch(input, opts)).then(function (res) {
      try {
        if (res && res.status === 401) gotoLogin();
        else if (res && res.status === 403) csrfNotice();
      } catch (e) { /* ignore */ }
      return res;
    });
  }

  /* ---------- 退出登录（仅 session 模式挂到书架顶栏） ---------- */
  function logout() {
    var back = LOGIN_PATH;
    try {
      var here = location.pathname + (location.search || '');
      if (here && here !== LOGIN_PATH) back = LOGIN_PATH + '?next=' + encodeURIComponent(here);
    } catch (e) { /* ignore */ }
    var go = function () { try { location.href = back; } catch (e) { /* ignore */ } };
    try { Promise.resolve(nativeFetch(LOGOUT_URL, { method: 'POST', credentials: 'same-origin' })).then(go, go); }
    catch (e) { go(); }
  }

  /* ---------- 书架顶栏「退出」按钮 ---------- */
  function mountLogout() {
    if (state.mode !== 'session') return;      // open / unknown 模式：不产生任何 DOM 变化
    try {
      var head = document.querySelector('.shelf-head .head-actions') ||
                 document.querySelector('.head-actions');
      if (!head || document.getElementById('authLogoutBtn')) return;
      // 仅书架页注入（阅读器/刷题页顶栏按钮已排满）
      if (!document.getElementById('shelf')) return;
      var b = document.createElement('button');
      b.id = 'authLogoutBtn';
      b.type = 'button';
      b.className = 'btn';
      b.textContent = '退出';
      b.title = '退出登录，返回口令页';
      b.addEventListener('click', logout);
      head.appendChild(b);
    } catch (e) { /* ignore */ }
  }

  /* ---------- 导出（只给只读快照，不外泄 csrf 值） ---------- */
  window.zhuxueFetch = zhuxueFetch;
  window.zhuxueAuth = {
    ready: function () { return fetchStatus(); },
    info: function () {
      return { mode: state.mode, authed: state.authed, ready: state.ready, offline: state.offline, hasCsrf: !!state.csrf };
    },
    loginHref: loginHref,
    logout: logout
  };

  // 启动：script 位于 body 末尾，DOM 已可用
  fetchStatus().then(mountLogout, function () { /* ignore */ });
})();
