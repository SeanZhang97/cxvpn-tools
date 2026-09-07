# 用户数据迁移至 LocalAppData

- 日期：2026-09-06
- 功能名：用户数据目录统一
- 分支：main

## 问题根因

应用此前以安装目录或 PyInstaller 的 `_internal` 目录作为配置、日志、浏览器缓存、路由缓存和
规则包写入位置。重新打包会清理 `dist`，需要构建脚本额外回填数据；将程序复制到另一台电脑或
安装到受保护目录时，也容易出现数据丢失或无写权限的问题。

## 涉及文件

- `core/app_paths.py`
- `core/config.py`
- `core/config_maintenance.py`
- `core/routing_rules.py`
- `core/subscription_store.py`
- `main.py`
- `build.py`
- `build_protected.py`
- `tests/test_app_paths.py`
- `tests/test_build_branding.py`
- `tests/test_routing_selection.py`
- `doc/repository/knowledge/common/打包与分发.md`
- `doc/repository/knowledge/modules/CXVPNTools.md`
- `doc/repository/knowledge/modules/Windows统一域名分流.md`

## 关键设计

- 用户态配置、日志、WebView 数据、订阅缓存、路由历史和可编辑规则包统一存放到
  `%LOCALAPPDATA%\CXVPNTools`，通常为 `C:\Users\<用户名>\AppData\Local\CXVPNTools`。
- 应用首次启动和构建清理发布目录前，自动从旧版发布目录、`_internal` 目录及旧产品名目录迁移
  已知数据。普通文件只补齐目标中不存在的内容；`config.json` 内容冲突时先校验 JSON，
  再保留修改时间较新的有效版本，另一份保存到用户目录的 `migration-backups`，避免构建清理
  旧发布目录时静默丢失配置。
- 旧发布目录中的 `routing/` 整体参与合并，覆盖订阅缓存与路由历史；构建和启动日志记录
  配置替换、冲突及备份数量，便于确认迁移结果。
- 规则包作为只读资源随程序分发，首次使用时补齐到用户数据目录；后续修改只发生在用户目录。
- Windows 服务仍使用 `C:\ProgramData` 保存系统级状态，避免改变服务账户访问边界。
- 构建产物不再保存或恢复运行时用户数据，防止用户数据混入分发包。

## 影响范围

- Windows 桌面应用的用户态数据路径和旧数据迁移流程。
- 标准构建与保护构建的资源打包、构建前迁移流程。
- 配置字段、业务数据格式、Windows 服务数据路径和外部接口保持不变。

## 验证情况

- 变更文件通过 `py_compile` 语法检查。
- 路径、迁移、构建约束、路由和订阅缓存相关离线测试共 146 项通过。
- 使用 `uv run --with pyinstaller python build.py` 完成标准打包，产物
  `dist/CXVPNTools/CXVPNTools.exe` 时间戳为 2026-09-06 21:21:23。
- 构建前从旧发布目录迁移 337 个文件；迁移后 `%LOCALAPPDATA%\CXVPNTools` 包含原日志、
  WebView 数据、规则包和代理保护状态。
- 新发布目录根目录和 `_internal` 中未发现配置、日志、缓存等运行数据；`_internal/rule-packs`
  仅保留只读内置规则资源。
- 新版进程 PID 35976 已启动并保持响应，窗口标题为 `CXVPNTools`；启动后日志仅写入用户目录，
  未新增启动异常。

### 2026-09-07 冲突迁移修复

- 复现并确认：当 `%LOCALAPPDATA%\CXVPNTools\config.json` 已存在旧版本，而旧发布目录中保留
  更新配置时，原迁移逻辑会跳过同名文件；PyInstaller 随后重建 `dist`，导致更新配置失去
  唯一主副本。不同文件名的路由历史和 provider 缓存仍会留下，因此表现为部分数据丢失。
- 新增“旧配置较新”“当前配置较新”“旧配置损坏”“冲突备份失败”和旧发布目录 `routing/`
  合并回归；`tests.test_app_paths` 7 项、`tests.test_build_branding` 5 项通过，相关 Python 文件
  语法检查通过。冲突副本无法保存时，构建会在 PyInstaller 清理 `dist` 前中止。
- 使用 `uv run --with pyinstaller python build.py` 完成标准打包，产物时间为
  2026-09-07 14:02:35；打包前后及新版启动后主配置哈希一致，发布目录未混入用户数据文件。
  新版进程 PID 28068 已启动并保持响应，启动日志未发现异常。

## 注意事项

- 迁移函数本身不删除源文件；但标准构建会在迁移完成后重建 `dist`，因此配置冲突必须确认
  已选择有效新版本且另一版本已进入 `migration-backups`。
- 本次未执行真实代理、VPN、DNS 或外部订阅服务的端到端验证。
