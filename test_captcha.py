# -*- coding: utf-8 -*-
"""test_captcha.py - 图标验证码通过率自测 (假手机号, 不消耗真实号码配额)

每轮: 全新浏览器上下文 → 登录页 → 填假手机号 → 点"获取验证码" →
等图标验证码弹窗 → VLM 求解(最多 captcha_max_attempts 次) → 记录是否通过。
用法: python test_captcha.py [轮数, 默认3] [--config 配置文件路径]
                           [--attempts 每轮最多识别次数]
"""
import os
import argparse
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from playwright.sync_api import sync_playwright

from core import captcha_handler, config as cfgmod, web_flow

FAKE_PHONE = '13000000000'


def log(m):
    print(m, flush=True)


def _load_config(path=None):
    if not path:
        return cfgmod.load()
    original = cfgmod.CFG_PATH
    try:
        cfgmod.CFG_PATH = os.path.abspath(path)
        return cfgmod.load()
    finally:
        cfgmod.CFG_PATH = original


def one_round(pw, idx, config_path=None, max_attempts=None):
    cfg = _load_config(config_path)
    cfg['phone'] = FAKE_PHONE
    if max_attempts is not None:
        cfg['captcha_max_attempts'] = max_attempts
    data_dir = os.path.join(cfgmod.BASE, f'browser_data_test_{idx}')
    cfg['browser_data_dir'] = data_dir
    ctx = web_flow.start_browser(pw, cfg)
    try:
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(web_flow.URL, wait_until='domcontentloaded')
        time.sleep(2)
        inp = page.query_selector('input[placeholder*="手机"]') \
            or page.query_selector('input[type="tel"]')
        if inp is None:
            log(f'[round {idx}] 未找到手机号输入框, 页面结构可能变化')
            return None
        inp.fill(FAKE_PHONE)
        btn = page.query_selector('text=获取验证码') \
            or page.query_selector('button:has-text("登录")')
        btn.click()
        if not web_flow._wait_popup(page, timeout=10):
            log(f'[round {idx}] 弹窗未出现 (假号可能被前置校验拒绝), 本轮无效')
            return None
        log(f'[round {idx}] 弹窗出现, 开始 VLM 求解')
        ok = captcha_handler.handle_captcha(
            page, cfg, log=log,
            manual_provider=lambda *a, **k: None)  # 人工通道直接放弃
        log(f'[round {idx}] 结果: {"通过" if ok else "未通过"}')
        return ok
    finally:
        try:
            ctx.close()
        except Exception:
            pass
        shutil.rmtree(data_dir, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description='图标验证码真实站点自测')
    parser.add_argument('rounds', nargs='?', type=int, default=3)
    parser.add_argument('--config', help='测试配置路径（仅内存读取，不改写）')
    parser.add_argument('--attempts', type=int, help='每轮最多刷新识别次数')
    args = parser.parse_args()
    rounds = args.rounds
    results = []
    with sync_playwright() as pw:
        for i in range(1, rounds + 1):
            r = one_round(pw, i, args.config, args.attempts)
            if r is not None:
                results.append(r)
            time.sleep(2)
    n = len(results)
    log(f'=== 图标验证码通过率: {sum(results)}/{n} ===')


if __name__ == '__main__':
    main()
