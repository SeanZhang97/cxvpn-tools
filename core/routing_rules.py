# -*- coding: utf-8 -*-
"""离线内置分流规则包；只包含随程序版本发布、可审计的低风险规则。"""

SCHEMA_VERSION = 2
BUILTIN_PACKS = {'off', 'local-direct-v1', 'cn-direct-v1'}

LOCAL_DIRECT_V1 = (
    ('DOMAIN-SUFFIX', 'lan', ''),
    ('DOMAIN-SUFFIX', 'local', ''),
    ('IP-CIDR', '10.0.0.0/8', 'no-resolve'),
    ('IP-CIDR', '100.64.0.0/10', 'no-resolve'),
    ('IP-CIDR', '127.0.0.0/8', 'no-resolve'),
    ('IP-CIDR', '169.254.0.0/16', 'no-resolve'),
    ('IP-CIDR', '172.16.0.0/12', 'no-resolve'),
    ('IP-CIDR', '192.168.0.0/16', 'no-resolve'),
    ('IP-CIDR6', '::1/128', 'no-resolve'),
    ('IP-CIDR6', 'fc00::/7', 'no-resolve'),
    ('IP-CIDR6', 'fe80::/10', 'no-resolve'),
)

# 不依赖在线 rule-provider/GeoSite。除 .cn 外只收录长期稳定且归属明确的
# 国内基础服务域名；版本升级通过新的 pack id 交付，避免后台静默漂移。
CN_DIRECT_V1_SUFFIXES = (
    'cn', 'baidu.com', 'bdstatic.com', 'bcebos.com', 'qq.com', 'qpic.cn',
    'gtimg.com', 'weixin.qq.com', 'aliyun.com', 'alicdn.com', 'alipay.com',
    'taobao.com', 'tmall.com', 'jd.com', '360.cn', '360.com', '163.com',
    '126.com', 'sina.com.cn', 'weibo.com', 'bilibili.com', 'douyin.com',
    'bytedance.com', 'byteimg.com', 'meituan.com', 'dianping.com',
    'xiaomi.com', 'mi.com', 'huawei.com', 'huaweicloud.com', 'chaoxing.com',
)


def rules_for(pack_id):
    if pack_id == 'off':
        return []
    rules = list(LOCAL_DIRECT_V1)
    if pack_id == 'cn-direct-v1':
        rules.extend(('DOMAIN-SUFFIX', suffix, '')
                     for suffix in CN_DIRECT_V1_SUFFIXES)
    return [f'{kind},{value},PHYSICAL{"," + option if option else ""}'
            for kind, value, option in rules]


def summary(pack_id):
    rules = rules_for(pack_id)
    return {
        'id': pack_id,
        'version': 1 if pack_id != 'off' else 0,
        'readonly': True,
        'rule_count': len(rules),
        'description': {
            'off': '不加载内置规则',
            'local-direct-v1': '局域网、回环和私有地址直连',
            'cn-direct-v1': '本地网络与稳定国内域名离线直连',
        }[pack_id],
    }
