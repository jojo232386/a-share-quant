# 本地环境

项目固定使用 Python 3.11、AKShare 1.18.64 和 Backtrader 1.9.78.123。依赖版本由 `uv.lock` 保存。

在当前 macOS 机器上，点目录 `.venv` 会被标记为 `UF_HIDDEN`，导致 Python 3.11 跳过其中的 `.pth` 文件。项目因此把真实环境放在非点目录 `venv`，同时保留 `.venv -> venv` 链接供编辑器和 `uv run` 识别。这是本机兼容处理，不是对 uv 默认行为的通用要求。uv 官方也允许通过 `UV_PROJECT_ENVIRONMENT` 指定项目环境路径：<https://docs.astral.sh/uv/concepts/projects/config/#project-environment-path>。

首次或重建环境时运行：

```bash
./scripts/bootstrap_env.sh
```

验收命令：

```bash
uv run python -c 'import aquant, akshare, backtrader; print(akshare.__version__, backtrader.__version__)'
uv run pytest -q
```

不要对整个虚拟环境递归执行 `chflags nohidden`；这会改动 NumPy 等二进制扩展的文件元数据。
