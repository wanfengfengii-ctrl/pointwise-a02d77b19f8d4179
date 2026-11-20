# 作用域记录与独立XML证据

## 现有能力与新增交付

现有 archive.py、README.md、verify.py、demo.py、tests/test_archive.py、样例及 .pointwise-runtime.json 均属于初始工程。原平面导入、查询、事务、通用附件和全部原回归须保持兼容。本扩展不放宽 read_flat 或 ingest 原有的平面格式。

新增两个关联能力：按XML作用域转换结构化行，以及为同一行保存可独立重放的完整XML证据。现有通用附件没有这层关联，不应把任意同名附件直接认作记录证据。不得覆盖或改变调用者已有的附件，即使其名称等于记录ID。

## 调用接口

```python
from archive import Archive

with Archive("supplier.sqlite") as archive:
    count = archive.ingest_scoped(chunks, ("urn:record", "item"), "https://e.test/root/")
    value = archive.row("outer")
    with archive.evidence("outer") as reader:
        block = reader.read(65536)
```

`Archive.ingest_scoped(chunks, record_name, source_base) -> int` 消费有限单遍输入，成功返回新增记录数。chunks由bytes组成，每块至多65536字节，允许空块，任意字符、标记、字符引用及UTF-8编码都可以跨块。

record_name是(namespace_uri, local_name)二元tuple，成员均为字符串，URI允许为空字符串，local_name须是XML 1.0的合法NCName。以展开名精确选择记录，不按源前缀选择；文档根也可以被选中。

source_base为绝对HTTP或HTTPS URI，不访问网络。以urllib.parse.urlsplit解析，scheme忽略大小写后须为http或https，netloc非空且字符串不含空白；解析错误或非法参数抛ValueError。

`row(id)`保持既有格式，返回((kind_uri, kind_local), absolute_href)。`ids()`仍列出全部行，包含旧行及本次新增行。

`Archive.evidence(id)`返回从头读取该记录证据的可关闭二进制读取器，支持read、readinto和上下文管理，不要求seek。证据须持久化，关闭重开后仍可读取；多个读取器可交错使用。Archive.close关闭其仍存活的证据读取器。缺少记录或该旧行没有证据时抛KeyError；非法id沿用原查询参数规则。原平面导入和手工add_row产生的旧行不会被自动补造证据。

## XML与记录解释

输入为UTF-8 XML 1.0。允许命名空间、注释、处理指令、CDATA、预定义实体和数字字符引用；拒绝DTD及外部实体。XML声明若指定编码，只接受UTF-8或UTF8。本范围不接收xml:lang或xml:space；在任何元素上遇到它们均抛ValueError。不实现Schema、XPath、网络资源获取或签名。

所有展开名等于record_name的元素都是记录，允许记录嵌套。每个选中元素自身必须具有无命名空间的id、kind、href三个属性；允许其他属性。id是经XML属性规范化后的非空字符串，不另行strip，在本次输入及既有records中均须唯一。其他元素不因缺少这三个属性而报错。

kind须是合法QName，不做额外空白裁剪。在该元素的命名空间作用域内解释：有前缀时必须存在对应绑定，无前缀时使用当前默认命名空间，默认命名空间不存在或被取消时URI为空字符串。xml前缀使用XML规定的固定绑定。不得因同一个前缀在不同位置拼写相同而合并其含义。

文档根的初始基准为source_base。逐层用urllib.parse.urljoin解释xml:base，包含记录自身的xml:base；缺少该属性时继承父基准。记录的href按其有效基准用urljoin解析。URI引用不得含空白，解析错误抛ValueError；最终href须符合原Archive绝对URI要求，即urlsplit可解析、scheme非空且不含空白。空href表示当前基准，不能被当成缺属性。只解释选中记录的kind与href，不根据其他属性值里的冒号猜测QName或URI。

例如source_base为https://e.test/root/，输入为：

```xml
<feed xmlns:p="urn:a" xml:base="daily/"><r id="x" kind="p:T" href="a"/></feed>
```

选择("", "r")后，返回1，row("x")为(("urn:a", "T"), "https://e.test/root/daily/a")。

## 独立证据的正确结果

每份证据以被选中的记录元素为文档根，包含其完整后代，且自身是良构的UTF-8 XML文档。内层记录单独归档后，外层证据仍须保留该内层记录的全部内容。

脱离原文祖先及原文地址后独立解析，证据中的元素和属性展开名、字符数据顺序、每个选中记录的kind解释与href解析结果须与源文相同。不得依赖调用者再提供原source_base才能恢复行。CDATA与字符引用可以改写为等价字符数据，属性顺序、前缀拼写和字节格式不限。

命名空间声明及证据根上的xml:base允许为闭合作用域而补齐或调整；这种调整不得改变上述语义。它们是上下文载体，不将为闭合环境而补入的声明或根xml:base计作业务属性增删。kind可同步改写QName前缀，href可改写为语义相同的URI表示；其余原有属性的XML规范化文本值须保留，不能把普通属性中的p:T一起重编码。后代中的局部绑定和基准必须继续产生原有解释。这里只要求语义保真，不要求XML规范化或逐字节复制。

行转换与证据归档必须使用同一记录身份及作用域语义。只有正确行而证据不能重放，或只有正确证据而行仍存字面QName和相对地址，均未完成本扩展。

## 原子性、错误与生命周期

必须消费到源EOF并确认整份文档有效后才能发布本次结果，包括没有选中记录的输入。非法参数、块格式、XML、记录属性、未绑定QName前缀、身份冲突及不支持的输入抛ValueError。源迭代器抛出的异常须原样传播，包括异常对象身份，不得因为其类型也是ValueError或SQLite异常而改写。

任何失败均不发布本次行、证据或其关联，既有行、通用附件和证据不变；源流先产生完整记录、随后报错也不能留下部分结果。成功结果关闭重开保真。允许复用现有事务，不要求并发导入或进程崩溃恢复。调用方原有文件不得被清理；自行建立的临时内容须在本次正常结束或异常退出时清理。

## 资源边界

允许暂存整份源文件、全量重扫及使用已有SQLite事务，不限制重扫次数，也不要求新增性能索引。

导入工作内存只能随当前输入块、元素深度及沿途开始标签与作用域信息增长，不能随完整记录正文、所有记录数量或整份原文长度增长。不得把整条记录先放入字符串、bytes或解析树再交给附件端口。固定深度及开始标签规模时，增长正文、CDATA、合法注释或处理指令长度仍须满足此边界；解析器内部缓存同样属于工作内存。

临时磁盘允许随输入字节数与最终证据总字节数之和线性增长。嵌套记录会自然产生重叠证据，不把这种最终输出重复计算成违规。使用有界读取核验输出；调用者主动无参read或ids导致的完整返回值分配不属于导入资源测量。

## 提供的嵌套样例

samples/scoped.xml以("urn:record", "item")选择记录，source_base为https://e.test/root/，应新增2行：

- outer：(("urn:outer", "Batch"), "https://e.test/root/daily/a.xml")
- inner：(("urn:inner", "Part"), "https://e.test/root/detail/b.xml")

outer证据须包含inner及其CDATA、字符数据和plain后代；inner证据的plain仍无命名空间，note仍为字面字符串k:literal。分别在任意不同于原文地址的绝对HTTP基准下重放两份证据，得到的记录分类和绝对地址均须与上述结果一致。

## 联动核验与演示

交付简短交接不变量说明，说明记录身份、作用域、完整内容和发布之间的联系。小规模核验使用独立于生产转换逻辑的全量参照，分别解释源文和每份独立证据，并与持久行核账；覆盖嵌套、默认命名空间取消、局部重绑定、QName字符引用、空元素、混合文本及跨块UTF-8。核对晚截断、重复ID和源异常的整次回滚，以及关闭重开和读取器生命周期。

以生成式大文本、注释、处理指令及嵌套记录检查资源边界，分别增长内容长度与元素深度，记录输入规模、峰值内存及结果核账。测量时不预存完整输入或输出。不设额外吞吐、扫描次数或固定测试数量门槛。

保留全部原测试字节。沿现有.pointwise-runtime.json接入原回归与新增真实核验；其verify和demo仍使用命令参数数组，setup保持命令参数数组列表，version与kind沿用现有值。demo须真实导入嵌套样例，读取持久行及独立证据，展示原始截取丢失祖先环境而新证据可重放的差别，并关闭重开核账，不能只打印成功信息。
