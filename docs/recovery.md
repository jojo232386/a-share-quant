# v0.1-research 失败恢复

恢复原则：先保留现场，再判断错误层。不要把删除冻结快照、历史输出或整个虚拟环境当作第一步。

## 找不到 uv

症状：`verify_v01.sh` 返回 `uv_not_found`。

处理：按 uv 官方方式安装后重新运行 `./scripts/bootstrap_env.sh`。不要改写 `uv.lock` 来绕过安装
问题。

## 依赖锁不一致

症状：返回 `lock_check_failed` 或版本不匹配。

处理：

```bash
./scripts/bootstrap_env.sh
uv lock --check
```

仍失败时保存完整环境输出，确认 Python 为 3.11，再检查仓库是否有未提交的 `pyproject.toml` 或
`uv.lock` 改动。不要在发布验收时临时升级 AKShare、Backtrader 或 pandas。

## 冻结输入验真失败

症状：`input_hash_mismatch`、`input_file_set_mismatch`、`unsafe_input_link`。

处理：

```bash
git status --short
git diff -- release/v0.1-research
```

从同一可信 Git 提交恢复发布目录，重新验收。不要从网络重新下载一份“看起来相同”的数据覆盖
冻结文件，也不要直接修改 `release_manifest.json` 迁就新哈希。

## 重建身份不一致

症状：`baseline_identity_mismatch`、`candidate_identity_mismatch`、
`risk_report_identity_mismatch` 或 `experiment_identity_mismatch`。

处理顺序：

1. 确认运行的是当前仓库根目录；
2. 运行 `uv lock --check`；
3. 检查代码、依赖、冻结输入和清单是否来自同一 Git 提交；
4. 保留失败 JSON，先定位漂移来源。

禁止只更新预期 ID 让验收变绿；这会掩盖代码、依赖或非确定性变化。

## 普通导出冲突

历史研究命令遇到已有同名但内容冲突的输出包时会拒绝覆盖。保留冲突目录，改用新的空输出根进行
诊断，或先用产物清单核验旧包。`verify_v01.sh` 本身在临时目录生成输出，不会覆盖根目录现有
`outputs/`。

## 运行中断

直接重新运行 `./scripts/verify_v01.sh`。临时目录会自动清理，固定输入不会变化，也不会产生第二笔
真实订单。若重复中断，保存最后一个进度阶段和退出 JSON，再检查磁盘空间、内存和系统休眠。
