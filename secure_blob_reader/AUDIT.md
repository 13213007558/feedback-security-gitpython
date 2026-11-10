# 审计结论：GitPython「禁用 unsafe protocols + checkout」能否安全读取陌生仓库

## 结论先行

团队的判断不成立，存在两处事实错误：

1. **`allow_unsafe_protocols=False`（默认）不是执行沙箱。** 它只在 `clone`/`fetch`/
   remote URL 这几个入口对 URL 字符串做一次正则匹配，拦截 `xxx::yyy` 形式的 remote
   helper（如 `ext::`），以及 `--upload-pack`、`-c` 等少数网络命令选项。它不约束任何
   本地对象读取、索引或检出路径，也不拦截仓库/环境配置里定义的 filter、hook、alternates。
2. **checkout 不是纯读取。** `IndexFile.checkout()` 实际执行 `git checkout-index` 子进程，
   `Head.checkout()` 执行完整 `git checkout`。二者都会让 git 按仓库的
   `.gitattributes` + 配置运行 smudge filter；完整 checkout 还会执行 `post-checkout`
   hook。审计后端读取源文件不应把检出作为必经步骤。

实证（下文有完整输出）：同一个只 `touch` 标记文件的无害 filter，纯 blob 读取产生 0 个
标记，而 `index.checkout()` 产生 1 个、`git checkout` 产生 smudge+hook 共多个标记。

## 三条路径逐一追踪

### 1. 对象数据库读取（blob/commit/tree）

入口：`Blob.data_stream` → `repo.odb.stream(binsha)`（`git/objects/base.py:193`）。
具体行为取决于 `Repo(..., odbt=...)` 选择的后端，默认值见
`git/repo/base.py:175`（`odbt=GitCmdObjectDB`）。

- **默认后端 `git.db.GitCmdObjectDB`（`git/db.py`）——会调用 git。**
  `info()` 走 `git cat-file --batch-check`（`git/cmd.py:1695`），`stream()` 走持久化的
  `git cat-file --batch`（`git/cmd.py:1722`）。每个对象读取都是一个真实 git 子进程，
  继承 `os.environ`（`git/cmd.py:1240`），因此受仓库/全局/系统配置与 `GIT_CONFIG_*`
  等环境变量影响。注意：`cat-file` 输出的是仓库里的原始对象内容，**不会**跑 smudge
  filter（filter 是工作树层的概念），实验中默认后端读 blob 同样产生 0 个 smudge 标记。
- **纯 Python 后端 `gitdb.db.GitDB`（`odbt=GitDB`）——不调用 git。**
  loose 对象用 zlib + mmap 解析，pack 对象用纯 Python 解包；`info/stream` 全程无
  子进程。实验中把一个假 `git` 放到 PATH 最前，钉死可信 git 路径后，open + 固定 SHA
  读取全程假 git 零调用。
- 两种后端都尊重 `objects/info/alternates`：实验里向 alternates 写入另一个对象目录后，
  两个后端都能读到原本不存在的 blob。这是仓库布局可改变读取来源的位置，打开陌生仓库前
  应校验。

### 2. 索引操作（index）

- 索引文件本身的解析/序列化是纯 Python（`git/index/fun.py`、`git/index/util.py` 的
  `pack/unpack`），`iter_blobs` 只是遍历内存条目（`BlobFilter` 仅是路径谓词，与 git 的
  smudge/clean filter 驱动无关）。
- 但凡是「让索引与树/工作树同步」的高层操作都调用 git 子进程：
  `IndexFile.from_tree()` → `git read-tree`（`git/index/base.py:288,385`），
  `IndexFile.reset()` 内部依赖 `from_tree`（`git/index/base.py:1396,1449`）。

### 3. 工作树检出（checkout）

- `IndexFile.checkout()` → `repo.git.checkout_index(...)`（`git/index/base.py:1328,1346`）：
  真实 `git checkout-index` 子进程，git 自身执行 smudge filter（实验：+1 标记），
  但不触发 `post-checkout` hook。
- `Head.checkout()` → `repo.git.checkout(...)`（`git/refs/head.py:272`）：完整
  `git checkout`，smudge filter 与 `post-checkout` hook 都执行（实验：smudge + hook
  标记同时出现）。
- 触发 filter 需要两个条件同时成立，二者都可由仓库侧提供：提交进仓库的
  `.gitattributes`（如 `payload.txt filter=auditmarker`）+ 配置中的
  `filter.<name>.smudge/clean` 命令。配置命令可来自仓库 `.git/config`（克隆/复制仓库即
  携带）、`~/.gitconfig`、系统级配置，或 `GIT_CONFIG_COUNT/KEY/VALUE` 环境变量注入
  （已实证：注入的别名命令被 git 子进程接受并执行）。

## 仓库或环境仍能改变行为的位置清单

- `git/cmd.py:1240`：每次 `execute()` 都 `os.environ.copy()` 并叠加 `self._environment`。
  `PATH` 决定 git 与 filter 命令解析；`HOME`/`XDG_CONFIG_HOME` 决定全局配置；
  `GIT_CONFIG_COUNT/KEY/VALUE`、`GIT_CONFIG_SYSTEM/GLOBAL` 可直接注入配置；
  `GIT_DIR`/`GIT_WORK_TREE`/`GIT_INDEX_FILE` 改变仓库定位。
- git 可执行文件本身：默认按 PATH 查找（`git/cmd.py:775`），且
  **import 期无条件执行一次 `git version` 探测**（`git/__init__.py:296` →
  `git/cmd.py:788`）。`GIT_PYTHON_REFRESH=quiet` 只改变探测失败后的反应，并不跳过探测
  （已实证：quiet 下假 git 仍被调用并导致 import 失败）。必须用
  `GIT_PYTHON_GIT_EXECUTABLE=<绝对路径>` 或 `Git.refresh(abs_path)` 钉死。
- 仓库内：`.git/config`（filter/hook/别名等）、`.gitattributes`（filter/text/eol 绑定）、
  `.git/hooks/*`（完整 checkout/其它命令触发）、`objects/info/alternates`（对象来源重定向）。
- 所有权：git 2.35.4+ 的 `safe.directory` 机制会让非属主仓库上的 git 命令直接报错；
  任何「往全局配置加 `safe.directory=*`」的修法都会扩大信任面，应避免。

## 两种 object database 后端区别

| 维度 | `GitCmdObjectDB`（默认，`git/db.py`） | `gitdb.db.GitDB`（纯 Python） |
| --- | --- | --- |
| 对象读取实现 | 持久化 `git cat-file --batch[-check]` 子进程 | zlib/mmap + 纯 Python pack 解析 |
| 是否调用 git | 是，每库常驻 cat-file 进程 | 否（对象读取零子进程） |
| 受仓库/环境配置影响 | 是（子进程继承环境、读各级配置） | 对象解析本身否；但 alternates 仍生效 |
| 会跑 smudge/clean filter | 否（cat-file 给原始对象），但子进程攻击面仍在 | 否 |
| 已知工程缺陷 | 子进程生命周期、线程不安全（cmd.py 注明每线程一个 Git 实例） | 官方注释警告文件句柄释放不积极，长进程/大仓库需留意 |

## 最小接入示例

见 `secure_blob_reader/read_blob.py`，要点：

- `Repo(path, odbt=GitDB)` 使用纯 Python 对象库；
- 只接受完整 40 位 SHA，拒绝缩写/符号引用，避免任何 revision 解析走 git；
- 校验对象类型为 blob，并对读出内容重算 SHA-1 与入参比对；
- 不做 checkout/reset/read-tree，不接触工作树，因此 filter/hook 无入口；
- 部署时设置 `GIT_PYTHON_GIT_EXECUTABLE` 为受信绝对路径，并清洗 `GIT_CONFIG_*`、
  `HOME`、`PATH` 等环境变量；打开仓库前检查 `objects/info/alternates`。

信任边界（必须在调用方/部署层承担，库本身不提供）：

- 信任的 git 可执行文件：仅 `GIT_PYTHON_GIT_EXECUTABLE` 钉死的那一个（本机实验为
  `/usr/bin/git`）。import 期版本探测仍会执行它一次；对象读取本身不再需要它。
- 外部配置边界：进程环境（尤其 `GIT_CONFIG_*`、`HOME`/`XDG_CONFIG_HOME`、`PATH`）与
  仓库内 `.git/config`、`.gitattributes`、hooks、alternates。示例不解析这些配置来变换
  内容，但无法阻止同进程内其它代码调用 git 时它们生效——所以要配合环境清洗与不检出策略。

## 测试证据

命令（临时仓库 `$TMPDIR/sec3-demo/audit-target`，filter 只 `touch` 标记文件）：

```
REPO_PATH=... SEC3_MARKER_DIR=... .venv/bin/python -m pytest \
    secure_blob_reader/test_read_blob.py -q -o addopts=""
# 6 passed
```

关键实测结果：

1. 默认 `Repo()` 的 `odb` 类型为 `git.db.GitCmdObjectDB`；`odbt=GitDB` 时为
   `gitdb.db.git.GitDB`。两者读出同一 blob 字节完全相同。
2. 纯 blob 读取（两个后端）前后 smudge 标记数均为 0；`index.checkout()` 后 +1；
   完整 `git checkout` 后 smudge 与 `post-checkout-hook-ran` 均出现。
3. PATH 最前放假 `git`：不钉路径时 import 期 `git version` 即失败；设置
   `GIT_PYTHON_GIT_EXECUTABLE=/usr/bin/git` 后读取成功且假 git 零调用。
4. `GIT_CONFIG_COUNT/KEY/VALUE` 注入的配置被 git 子进程接受（标记文件出现），证明
   环境变量是真实配置注入面。
5. alternates 指向外部对象目录后，两个后端都读到外部 blob。

## 对团队提议的直接回应

- 「关掉 unsafe protocols」只防 `ext::` 这类**传输层** remote helper 和少量网络命令选项，
  对本地 filter/hook/alternates/环境注入完全无效——不要把它描述成通用执行沙箱。
- 「再 checkout 就可以了」恰恰是把只读需求升级成会执行仓库提供命令的操作。读取源文件
  应固定 commit、用纯 Python 后端按完整 SHA 直读 blob，检出仅在确有工作树需求时、在隔离
  环境（容器/专用用户、清洗过的环境变量、`-x` 关闭可执行位等）下单独进行。
