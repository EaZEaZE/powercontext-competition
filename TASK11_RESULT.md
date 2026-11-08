# 赛题 11：SeekDB 单 SQL 混合检索

实现位于分支 `codex/powercontext-complete-11`，基于统一提测提交 `27f3fa904f63a4536ebc47ec2bbe71f62b88310c`，包含赛题 1 至 11 的集成代码。本分支针对赛题 11 修改三个产品源码文件，并补充行为回归测试。

## 产品行为

- hybrid 模式使用一条候选 SQL，分别在数据库中筛选并截断全文、向量候选，再合并和读取对应 entry version 的精确正文。
- 全文分支指定已有全文索引，避免选择性 scope 使优化器改走标量索引。
- 无 tag 的向量分支保留冻结查询的结构，并在合并前记录原生候选顺序，避免等距结果被身份排序重新排列。融合与 admission 规则保持冻结契约。
- scope、Memory artifact、tag 和候选 limit 在数据库内生效；tag 的 any/all 和无匹配情况保持可用。
- 搜索引用请求中已校验的当前 head，允许未改动投影保留旧 head_revision。历史版本、缺失版本和被修改的 Memory 快照仍被拒绝。
- 当前 head 校验改为查询版本存在性与 head 指针，服务层保留快照内容校验，减少重复读取和解析完整 manifest。搜索前后均保留 head 校验。
- 继续使用产品传入的 AsyncConnection 和冻结 schema；未修改公开题面、验收文件、依赖锁文件和 HTTP 合约。

## 验证

Linux 功能与质量验证使用真实嵌入式 SeekDB，环境为 Linux x86_64、Python 3.12.13、pylibseekdb 1.3.0.post4。Linux 由 arm64 主机通过 QEMU 执行。

| 检查 | 结果 |
| --- | --- |
| 赛题 11 公开用例及 SeekDB 回归 | 5 个独立用例全部通过，覆盖单 SQL、双索引、scope、多 Memory、tag any/all/无匹配、当前引用、修订、停用、恢复、精确展开及等距向量顺序 |
| macOS Memory 相关回归 | 95 项通过，16 项按后端或平台条件跳过 |
| 赛题 5、8、9、10 公开用例 | 25 项通过 |
| macOS SeekDB 1.4.0.post1 补充回归 | 4 项通过，含同 scope 多 Memory 隔离、tag any/all/无匹配 |
| 冻结基线可达性、改动文件 Ruff/类型检查、git diff --check | 通过 |

主要复现命令，在仓库根目录运行；`python` 指向已安装对应依赖的解释器：

```bash
make -C competition check-baseline
make -C competition test-hybrid
PYTHONPATH=src PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -q \
  -c competition/pytest.ini --confcutdir=competition tests/e2e/test_seekdb_memory_search.py
make -C competition test-rerank test-embedding test-lifecycle test-evidence
PYTHONPATH=src python -m pytest -q tests/builtin/artifacts/memory \
  tests/builtin/persistence/test_memory.py tests/builtin/persistence/test_seekdb_profile.py \
  tests/builtin/persistence/test_sqlite_memory_vector_index.py \
  tests/e2e/test_memory_search_concurrency.py tests/e2e/test_memory_capacity.py
```

全仓 `prek run -a` 未通过：公开样例存在格式和类型诊断。与统一提测基线使用同一 Python 环境、同一 ty 比较，195 条类型诊断逐行一致；改动源码与测试的单独类型检查通过。格式工具产生的公开样例变动未包含在交付中。

## 逐查询质量

本地固定语料来自公开 BEIR SciFact train 抽样：173 篇文档、20 条 query、384 维单位向量，写入三个独立 scope。不是官方隐藏数据。两个路径共用同库、scope/head、query 和 embedding；对照使用冻结提交的双查询索引，各预热一次并交替测量。通过公开 Memory.search 运行，检查实际 SQL/EXPLAIN，并对照已入库 entry version 核验身份、正文、通道和重复查询稳定性。

Linux 1.3.0.post4 的 60 次查询结果：

- Recall@10：1.0；MRR@10：0.855。
- 逐查询 Recall@10 和 MRR@10 退化数：0。
- 命中文档顺序、全文候选顺序、向量候选及距离：60/60 与冻结路径一致。
- 每次候选检索一条 SQL；无缺失全文或向量索引的执行计划。
- 命中均具有当前有效引用、对应版本逐字正文及合法非空通道，无重复身份，重复检索稳定。
- 完整 Memory.search 的 p50：候选路径 651.50ms，冻结路径 656.15ms。跨架构模拟环境的结果不能证明原生比赛环境的 500ms 门槛。

macOS arm64、SeekDB 1.4.0.post1 的同语料补充对照为 Recall@10=1.0、MRR@10=0.88、p50=37.96ms，60 次查询无退化、无缺失索引，不能替代固定比赛环境验证。

本地验证脚本、固定向量输入、冻结索引源码及逐查询 JSON 保存在相邻的 `task11-validation` 目录，执行方式和输入摘要见该目录 README.md。产品源码不包含语料、qrels 或查询特例。

## 集成条件

待队长合入统一提测分支后，在原生 Linux x86_64、比赛资源配额下确认完整搜索 p50，并以完整 40 位提交 SHA 统一提测。官方隐藏语料、隐藏质量门槛和本提交的官方分数尚未验证；本地通过不等同于官方 20/20。
