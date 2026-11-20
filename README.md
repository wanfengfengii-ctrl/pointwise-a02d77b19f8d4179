# Flat supplier archive

Python 3.11 标准库项目。把供应商的平面 XML 记录追加到 SQLite，并提供独立的通用二进制附件端口。

运行：

```sh
python3.11 -B verify.py
python3.11 -B demo.py
```

verify 发现并运行 tests 中全部原回归，包括文件数据库重开、整批回滚、附件生命周期和生成式大载荷检查。demo 在临时目录真实导入样例、存储附件、拒绝截断文档并关闭重开；退出后删除自建目录。不需要安装依赖或访问网络。

## XML 输入

`Archive(path).ingest(chunks, record_name="r") -> int` 返回新增记录数。chunks 是有限、单遍 bytes 流，每块至多 65536 字节，允许空块。源迭代异常原样传播；XML、属性、身份冲突及块格式违规抛 ValueError。失败整次不发布，既有记录和附件不变。

文档为 UTF-8 XML 1.0，根为无属性的 feed；直接子元素必须全部等于 record_name。record_name 是非 feed 的 ASCII NCName，字符范围为 [A-Za-z_][A-Za-z0-9_.-]*。记录恰含 id、kind、href 三个属性：

- id 为非空字符串，在整个仓库内唯一。
- kind 为同样字符范围的 ASCII NCName，保存为 ("", kind)。
- href 为绝对 URI：urllib.parse.urlsplit 可解析、scheme 非空且不含空白。不解析相对地址，不访问网络。

记录只允许文本，不允许子元素；文本被解析校验但不留存。根下记录之间只允许 XML 空白。允许注释、处理指令、CDATA、预定义实体和数字字符引用。拒绝 DTD、外部实体、命名空间声明及带冒号的名称。声明编码若存在须为 UTF-8 或 UTF8。任意 UTF-8 字符与 XML 标记都可以跨块。

`read_flat(chunks, record_name="r")` 是公开的低层转换器，惰性产生 (id, ("", kind), href)。它可以在源 EOF 前产生行，只有耗尽迭代器才验证整份文档。需要整批原子性的调用者使用 ingest。

## 查询和事务端口

```python
from archive import Archive

with Archive("supplier.sqlite") as archive:
    with archive.transaction() as batch:
        batch.add_row("manual-1", ("", "Sale"), "https://vendor.example/1")
        batch.put_attachment("operator-note", [b"checked", b"\n"])
    assert archive.row("manual-1") == (("", "Sale"), "https://vendor.example/1")
    with archive.attachment("operator-note") as reader:
        assert reader.read(7) == b"checked"
```

Archive 接收文本路径或产生文本路径的 PathLike，父目录须存在；不支持 :memory:。本接口面向单个写入调用方，不支持交错或嵌套写事务。多个已打开附件读取器可以与该 Archive 的后续追加交错使用。正常关闭重开保真，不提供进程崩溃恢复承诺。

- `row(id)` 返回 ((namespace_uri, local_name), href)，缺失抛 KeyError。
- `ids()` 返回按字符串排序的全部 ID 元组。
- `transaction()` 产生有效期限于该上下文的 Batch。正常退出提交，任何异常回滚。某次写入失败后，即使调用方在上下文内捕获异常，批次仍不可提交，退出抛 ValueError。已退出或已失败的 Batch 不可继续使用。
- `Batch.add_row(id, kind, href)` 追加一行。kind 必须为两个字符串的 tuple，URI 可以为空、local_name 非空；它存储调用者已给出的名字，不执行 XML 作用域解释。字符串须可编码为 UTF-8。id 不得冲突，href 遵循上述绝对 URI 校验。
- `Batch.put_attachment(name, chunks)` 追加独立附件。name 为非空字符串，与行 ID 是两个独立键空间。不得覆盖；空附件合法。块和异常规则与 XML 源相同。它与同一批次的行共同提交或共同回滚。
- `attachment_info(name)` 返回 (总字节数, SHA256十六进制字符串)，缺失抛 KeyError。
- `attachment(name)` 返回拥有独立数据库连接的可关闭二进制读取器，支持 read、readinto、上下文管理，不支持 seek。缺失抛 KeyError。读取器从头读取完整原始字节，可交错打开多个。
- `close()` 幂等，关闭仍存活的全部附件读取器和主连接。已关闭 Archive 不可查询。写事务内部查询 Archive、再开事务或关闭 Archive 均抛 RuntimeError。

附件不自动关联任何行，也不保证内容是 XML。平面导入不会自动创建附件。

## 作用域记录与独立 XML 证据

`Archive.ingest_scoped(chunks, record_name, source_base) -> int` 在保持上述平面能力
不变的前提下，按 XML 命名空间作用域导入结构化记录，并为每行保存一份可独立重放的
完整 XML 证据。契约细节见 SCOPED.md，交接不变量见 SCOPED_HANDOFF.md。

- `record_name` 是 `(namespace_uri, local_name)`，按展开名精确选择记录，允许嵌套，
  文档根也可被选中；`local_name` 须为合法 NCName，URI 允许空串。
- `source_base` 是绝对 http/https URI（不访问网络）。逐层 `xml:base` 用
  `urllib.parse.urljoin` 解释；记录 `kind` 在其命名空间作用域内解释成
  `(kind_uri, kind_local)`，`href` 按有效基准解析成绝对 URI 后存入既有 `records` 表。
- 选中元素必须带无命名空间的 `id`、`kind`、`href`；身份在仓库内唯一，冲突整次回滚。
  拒绝 DTD、外部实体、`xml:lang`、`xml:space`、未绑定 QName 前缀及非法 XML；
  仅接受 UTF-8/UTF8 声明。源迭代异常原样传播（保留对象身份）。
- `Archive.evidence(id)` 返回从头读取该记录证据的可关闭二进制读取器，支持
  `read`、`readinto` 与上下文管理，不要求 seek；持久化、可交错、关闭重开保真。
  缺少记录或该旧行没有证据时抛 `KeyError`。证据键空间与通用附件相互独立：同名附件
  不会被认作证据，也不会被覆盖；平面导入和手工 `add_row` 的旧行不补造证据。
- 每份证据以记录元素为文档根并包含完整后代，根上补齐闭合作用域所需的命名空间声明和
  绝对 `xml:base`。脱离原文祖先与原地址后独立解析，展开名、字符数据顺序、记录分类与
  绝对地址均与源文一致。CDATA 与字符引用可改写为等价字符数据，普通属性文本值保留。
- 导入工作内存只随输入块、深度及沿途开始标签/作用域增长，不缓存整条记录，长注释和
  处理指令同样满足；证据流式落到临时侧车后在同一事务内发布，失败整次回滚并清理临时
  内容。资源实测入口为 `python3.11 -B resource_probe.py`。

## 存储和资源

SQLite user_version=1。records 保存 id、kind_uri、kind_local、href；attachments 保存名称、长度、摘要；attachment_chunks 以 (name, part) 排序保存原始 bytes。作用域扩展另增 evidences 与 evidence_chunks 两张表（以 (id, part) 排序保存证据 bytes），与附件表互不影响，旧 v1 数据库可直接打开并继续追加。每个非空输入块对应一行，未做块合并或压缩。事务使用 SQLite，附件、证据与行存放于同一数据库；WAL 和 SHM 是 SQLite 管理的同目录文件。附件与证据读取连接只读且 query_only。

XML 转换不缓存记录正文；工作状态包括当前输入块、解析器开始标签信息及当前块产生的行。附件写入和有大小限制的读取按块处理；无参 read 和 ids 会按调用者请求分配完整结果。每个数据库连接的页面缓存设为约 512 KiB。

samples/feed.xml 与 samples/note.txt 是有限样例。samples/archive-v1.sql 是可直接由 sqlite3.executescript 装入的真实 v1 数据夹具，包含 historical-1 行及跨两个块的 supplier-note 附件，原回归会打开该夹具并继续追加。
