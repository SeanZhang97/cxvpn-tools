# -*- coding: utf-8 -*-
"""probe_disabled_click.py - 验证 disabled 按钮上各种点击方式的事件行为

背景: 超星登录页 #vpnSmsCodeBtn 冷启动时疑似 disabled, 原生 el.click()
静默无效, 而 $('#vpnSmsCodeBtnText').trigger('click') 可触发。用同内核
(Chromium/Edge) 实测确认: click() / dispatchEvent / 子元素冒泡 / jQuery
trigger 在 disabled 按钮上的监听器行为, 以决定 PageShim 点击实现。
"""
import sys

from playwright.sync_api import sync_playwright

HTML = """
<!doctype html><html><body>
<button id="btn" disabled><span id="txt">获取验证码</span></button>
<button id="btn2"><span id="txt2">正常</span></button>
<script>
window.log = [];
const on = (id) => document.getElementById(id)
  .addEventListener('click', () => window.log.push(id));
on('btn'); on('txt'); on('btn2'); on('txt2');
</script>
</body></html>
"""


def main():
    with sync_playwright() as pw:
        b = pw.chromium.launch(channel='msedge', headless=True)
        pg = b.new_page()
        pg.set_content(HTML)
        r = pg.evaluate("""() => {
            const out = {};
            // 1. disabled 按钮原生 click()
            window.log = []; document.getElementById('btn').click();
            out.disabled_native_click = window.log.slice();
            // 2. disabled 按钮 dispatchEvent
            window.log = [];
            document.getElementById('btn').dispatchEvent(
                new MouseEvent('click', {bubbles: true, cancelable: true,
                                         view: window}));
            out.disabled_dispatch = window.log.slice();
            // 3. disabled 按钮子元素 dispatchEvent 冒泡
            window.log = [];
            document.getElementById('txt').dispatchEvent(
                new MouseEvent('click', {bubbles: true, cancelable: true,
                                         view: window}));
            out.disabled_child_dispatch = window.log.slice();
            // 4. 正常按钮原生 click()
            window.log = []; document.getElementById('btn2').click();
            out.enabled_native_click = window.log.slice();
            // 5. 正常按钮 dispatchEvent
            window.log = [];
            document.getElementById('btn2').dispatchEvent(
                new MouseEvent('click', {bubbles: true, cancelable: true,
                                         view: window}));
            out.enabled_dispatch = window.log.slice();
            return out;
        }""")
        for k, v in r.items():
            print(f'{k}: {v}')
        b.close()


if __name__ == '__main__':
    sys.stdout.reconfigure(encoding='utf-8')
    main()
