/* 品牌匹配只看平台名称或已知官方域名；资源离线打包，不请求远程 favicon。 */
(() => {
  'use strict';
  const brands = [
    { file: 'github.svg', aliases: ['github', 'github.com'] },
    { file: 'google.png', aliases: ['google', 'google.com', 'accounts.google.com', 'gmail', '谷歌'] },
    { file: 'gitlab.svg', aliases: ['gitlab', 'gitlab.com'] },
    { file: 'openai.svg', aliases: ['openai', 'openai.com', 'platform.openai.com', 'chatgpt', 'chatgpt.com'] },
  ];
  function image(issuer) {
    const value = String(issuer).trim().toLowerCase().replace(/^https?:\/\//, '').replace(/\/$/, '');
    const brand = brands.find(item => item.aliases.includes(value));
    return brand ? `icons/brands/${brand.file}` : null;
  }
  window.TwoFactorBrands = { image };
})();
