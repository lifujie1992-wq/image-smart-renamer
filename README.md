# Image Smart Renamer

本地审核优先的图片智能命名工具。仅扫描所选目录当前层，按 SHA-256 识别精确重复；Claude 只提出建议，用户审核并预演后才执行原地改名。每次提交先在 `~/Library/Application Support/ImageSmartRenamer/history/` 写 manifest，并支持撤销最近一次操作。

编号规则支持本机持久化：草稿自动保存，并可保存为多套命名模板。数据写在 `~/Library/Application Support/ImageSmartRenamer/rules.json`，不依赖浏览器 `localStorage`，清理缓存或换浏览器也不会丢。若该文件损坏，工具会 fail closed 并提示，不会静默覆盖你的模板。

## 环境与启动

要求 Python 3.11、Anthropic 官方认证，以及 `pyproject.toml` 中声明的依赖。项目不保存 API Key。

```bash
./start-mac.sh
```

打开 <http://127.0.0.1:8765>。不要使用多 worker 或开发自动重载，因为 V1 的任务与文件夹会话保存在单进程内存中。

局域网访问时，请用 Chrome / Edge 打开服务地址。选择文件夹会弹出**当前浏览电脑**的系统对话框（Win 上就是 Win 的），识别走服务端，确认改名时在本机原地重命名。仅在本机用 Safari 等不支持 File System Access API 的浏览器时，才会回退到服务器本机的文件夹选择器。

## 测试

```bash
python3.11 -m pytest --cov=app --cov-report=term-missing
ruff check .
black --check .
isort --check-only .
```

真实 Claude 测试应显式标记 `anthropic_live`，常规测试使用 FakeClassifier，不产生 API 费用。

## 安全边界

- 仅绑定 `127.0.0.1`。
- 浏览器只持有不透明 folder/job/item ID，不提交绝对路径。
- 支持 JPG/JPEG/PNG/WebP；不递归、不跟随软链接。
- 外部占用、源文件变化、过期计划均会阻止提交。
- 改名和撤销均使用两阶段临时名；失败时尝试回滚，无法完整恢复会留下 `needs_recovery` manifest。
