# -*- coding: utf-8 -*-
"""连接编排：软件凭据同步 Windows，并通过 RAS/EAP 无界面拨号。"""
import re
import time

from . import ras_cred, vpn_connect

_ERR_HINTS = [
    ('621', '无法打开 VPN 电话簿 (621): 请检查 Windows VPN 配置和当前用户权限'),
    ('622', '无法加载 VPN 电话簿 (622): 请重新创建该 VPN 配置'),
    ('624', '无法写入 VPN 电话簿 (624): 请检查当前用户权限和文件占用'),
    ('625', 'VPN 电话簿内容无效 (625): 请重新创建该 VPN 配置'),
    ('633', 'VPN 端口已被占用 (633): 请断开残留连接；仍失败时使用“修复 VPN 服务”'),
    ('628', 'VPN 连接已断开 (628): 可能是网络波动或服务器主动终止'),
    ('629', 'VPN 被远程服务器断开 (629): 请稍后重试或检查服务器状态'),
    ('668', 'VPN 链路已中断 (668): 请检查当前网络稳定性'),
    ('691', '认证被拒 (691): 密码错误或授权到期。密码正确时工具会自动触发续期; '
            '若超星轮换过密码, 请在「VPN 配置」中点该行「凭据」更新为新密码'),
    ('692', 'VPN 端口或设备发生硬件故障 (692): 请使用“修复 VPN 服务”后重试'),
    ('619', '连接被终止 (619): PPTP 可能被当前网络拦截, 换网络或协议试试'),
    ('623', '系统未找到该 VPN 条目 (623): 请检查名称'),
    ('703', 'EAP 无界面凭据不可用 (703): 请在「VPN 配置」中重新保存账号密码'),
    ('711', 'Windows RAS 服务初始化失败 (711): 请使用“修复 VPN 服务”'),
    ('718', 'PPP 连接超时 (718): 请检查网络或稍后重试'),
    ('721', '远程 PPP 端无响应 (721): PPTP 请检查 TCP 1723 和 GRE 协议'),
    ('720', '无法协商 PPP 控制协议 (720): 协议/加密配置不匹配, 检查隧道类型'),
    ('734', 'PPP 链路控制协议已终止 (734): 请检查认证和协议配置'),
    ('756', 'VPN 正在拨号 (756): 请等待前一次连接结束后重试'),
    ('781', 'VPN 连接所需证书不存在 (781): 请安装有效证书'),
    ('786', 'L2TP 缺少有效计算机证书 (786): 请检查计算机证书'),
    ('787', 'L2TP 无法验证远程计算机 (787): 请检查预共享密钥或证书'),
    ('788', 'L2TP 安全参数不兼容 (788): 请检查客户端和服务器配置'),
    ('789', 'L2TP 初始安全协商失败 (789): 请检查 IPsec 服务、密钥和防火墙'),
    ('790', 'L2TP 远程证书验证失败 (790): 请检查证书有效期和信任链'),
    ('791', 'L2TP 安全策略不存在 (791): 请检查 IPsec 策略'),
    ('792', 'L2TP 安全协商超时 (792): 请检查网络、防火墙或稍后重试'),
    ('793', 'L2TP 安全协商出错 (793): 请检查 IPsec 配置'),
    ('798', '找不到 EAP 可用证书 (798): 请安装并选择有效证书'),
    ('800', '无法到达 VPN 服务器 (800): 请检查网络或服务器地址'),
    ('806', 'PPTP 的 GRE 流量被阻止 (806): 请检查路由器或防火墙的 GRE 协议'),
    ('807', 'VPN 网络连接中断 (807): 请检查网络稳定性或服务器负载'),
    ('809', '无法与服务器建立隧道 (809): 服务器无响应或被防火墙拦截'
            '(常见于 UDP 500/4500 被拦)'),
    ('812', '连接被策略拒绝 (812): NPS/网络策略不允许此连接'),
    ('829', 'VPN 被远程服务器断开 (829): 请检查服务器状态或稍后重试'),
    ('868', '无法解析服务器地址 (868): DNS 失败或服务器地址错误'),
    ('1223', 'VPN 连接已取消 (1223)'),
    ('1460', 'VPN 连接超时 (1460): Windows 未在限定时间内完成拨号，'
             '已强制终止本次连接'),
    ('13801', 'IKE 身份验证失败 (13801): 请检查证书或账号'),
    ('13806', '找不到有效计算机证书 (13806): IKEv2 所需证书缺失'),
    ('13868', 'IKE 认证方法不被服务器接受 (13868)'),
]

_TRANSIENT_CODES = {
    '619', '628', '629', '668', '718', '721', '792', '800', '807',
    '809', '829', '868',
    '1460',
}
_SERVICE_CODES = {'633', '692', '711'}
_PROFILE_CODES = {'621', '622', '623', '624', '625'}
_CONFIG_CODES = {
    '703', '720', '734', '781', '786', '787', '788', '789', '790',
    '791', '793', '798', '806', '812', '13801', '13806', '13868',
}
_BUSY_CODES = {'756'}


def error_code(out):
    """从 RAS 输出或已格式化提示中提取已知错误码。"""
    text = out or ''
    for code, _ in _ERR_HINTS:
        if re.search(r'\b' + code + r'\b', text):
            return code
    return ''


def failure_policy(out):
    """返回连接失败的分类与自动恢复策略。"""
    text = out or ''
    if ('NO_TOOL_CREDENTIALS' in text or 'NO_SAVED_CREDENTIALS' in text or
            text.startswith('Windows 未保存此 VPN') or
            '软件尚未保存此 VPN 的账号密码' in text):
        return {'code': '691', 'category': 'credentials',
                'retryable': False, 'suggest_repair': False,
                'trigger_renew': False}
    code = error_code(text)
    if code == '691':
        return {'code': code, 'category': 'authentication',
                'retryable': True, 'suggest_repair': False,
                'trigger_renew': True}
    if code in _BUSY_CODES:
        return {'code': code, 'category': 'busy',
                'retryable': True, 'retry_delay': 30,
                'suggest_repair': False, 'trigger_renew': False}
    if code in _TRANSIENT_CODES:
        return {'code': code, 'category': 'network',
                'retryable': True, 'suggest_repair': False,
                'trigger_renew': False}
    if code in _SERVICE_CODES:
        return {'code': code, 'category': 'service',
                'retryable': False, 'suggest_repair': True,
                'trigger_renew': False}
    if code in _PROFILE_CODES:
        return {'code': code, 'category': 'profile',
                'retryable': False, 'suggest_repair': False,
                'trigger_renew': False}
    if code in _CONFIG_CODES:
        return {'code': code, 'category': 'configuration',
                'retryable': False, 'suggest_repair': False,
                'trigger_renew': False}
    return {'code': code, 'category': 'unknown',
            'retryable': True, 'suggest_repair': False,
            'trigger_renew': False}


def friendly(out):
    """把 rasdial 输出提炼成一句可读提示"""
    if 'NO_TOOL_CREDENTIALS' in (out or ''):
        return ('软件尚未保存此 VPN 的账号密码，请在「VPN 配置」中点击该行'
                '「凭据」补录')
    if 'NO_SAVED_CREDENTIALS' in (out or ''):
        return ('Windows 未保存此 VPN 的可用登录密码，请在「VPN 配置」中点击'
                '该行「凭据」补录')
    lines = [l.strip() for l in (out or '').splitlines() if l.strip()]
    for ln in lines:
        for code, hint in _ERR_HINTS:
            if re.search(r'\b' + code + r'\b', ln):
                return hint
    return lines[0] if lines else ''


def sync_credentials(name, username, password):
    """同步普通 RAS 凭据及 EAP 用户数据，返回 (ok, error)。"""
    ok, error = ras_cred.set(name, username, password)
    if not ok:
        return False, error
    eap_ok, eap_message = vpn_connect.prepare_credentials(
        name, username, password)
    if not eap_ok:
        return False, eap_message
    return True, 0


def connect(name, creds=None, log=print, timeout=90, cancel_event=None):
    """连接 VPN, 返回 (ok, msg)

    creds: 工具侧保存的凭据 {'user','pass'} 或 None。
    - 有凭据: 同步普通 RAS/EAP 凭据，再把软件保存的账号密码交给 RAS。
    - 无工具凭据: 不调用会弹框的 RasDialDlg，直接要求在软件内补录。
    """
    if creds and creds.get('user') and creds.get('pass'):
        wok, err = sync_credentials(name, creds['user'], creds['pass'])
        if wok:
            log(f'[vpn] {name} 账号密码已同步 Windows，正在无界面连接')
        else:
            log(f'[vpn] {name} 同步 Windows 失败 (错误 {err})，'
                '仍尝试使用软件凭据连接')
        started = time.monotonic()
        log(f'[vpn] {name} 开始异步 RAS 拨号（超时 {timeout:g} 秒）')
        ok, out = vpn_connect.connect(
            name, creds['user'], creds['pass'], timeout=timeout, log=log,
            cancel_event=cancel_event)
        message = friendly(out) or ('连接成功' if ok else '连接失败')
        elapsed = time.monotonic() - started
        log(f'[vpn] {name} 异步 RAS 拨号结束 -> '
            f'{"成功" if ok else "失败"}（{elapsed:.1f} 秒）: {message}')
        return ok, message
    return False, friendly('NO_TOOL_CREDENTIALS')
